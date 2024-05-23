import sys
import requests
import json
import time
import sqlite3
import os
import random
from youtube_transcript_api import YouTubeTranscriptApi
from datetime import datetime, timedelta
from pydub import AudioSegment
from sqlite3 import Error


def trim_text(text, max_length=500):
    if len(text) <= max_length:
        return text

    # Find the last occurrence of a full stop within the maximum length
    last_full_stop_index = text.rfind('.', 0, max_length)

    if last_full_stop_index == -1:
        # If no full stop found within the limit, simply truncate at max_length
        return text[:max_length]

    # Return text trimmed at the last full stop within the limit
    return text[:last_full_stop_index + 1]


def generate_filename():
    """Generates a random filename of the form 'part1.X.mp3'."""
    random_number = random.randint(0, 20)
    return f"{random_number}.mp3"


def trim_string(s, max_length):
    if len(s) <= max_length:
        return s  # No need to trim if string length is already within the limit
    else:
        return s[:max_length]  # Trim string to the specified max length


def db_create_connection():
    try:
        conn = None
        dir_path = os.path.dirname(os.path.realpath(__file__))
        file_location = f'{dir_path}/podcast.db'
        conn = sqlite3.connect(file_location)
        return conn
    except Error as e:
        print(e)
        sys.exit()


def db_return_next_episode():
    conn = db_create_connection()
    cur = conn.cursor()
    cur.execute("SELECT number FROM episode ORDER BY number DESC LIMIT 1")

    rows = cur.fetchall()

    if len(rows) == 0:
        value = 0  # Or raise an exception like: raise Exception("No matching row found")

    elif len(rows) == 1:
        # One row returned, extract the value
        value = rows[0][0]  # Access the first (and only) row, then access the 'value' column

    else:
        # More than one row returned (unexpected), handle this case accordingly
        raise Exception("More than one row returned, which is unexpected")

    # Close the cursor and connection when done with the query
    cur.close()
    conn.close()

    return value + 1


def db_insert_episode(description):
    conn = db_create_connection()
    if conn:
        try:
            cur = conn.cursor()
            date = int(time.time())

            cur.execute("INSERT INTO episode (date, description) VALUES (?, ?);",
                        (date, description,))
            conn.commit()  # Don't forget to commit changes

            cur.close()
            conn.close()
        except sqlite3.Error as e:
            print("SQLite error: ", e)
    else:
        print("Failed to create database connection.")


def db_get_secret(tag):
    conn = db_create_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM secrets where tag=?", (tag,))

    rows = cur.fetchall()

    if len(rows) == 0:
        value = None  # Or raise an exception like: raise Exception("No matching row found")

    elif len(rows) == 1:
        # One row returned, extract the value
        value = rows[0][0]  # Access the first (and only) row, then access the 'value' column

    else:
        # More than one row returned (unexpected), handle this case accordingly
        raise Exception("More than one row returned, which is unexpected")

    # Close the cursor and connection when done with the query
    cur.close()
    conn.close()

    return value


def get_presigned_url(access_token, filename, file_size, content_type):
    url = "https://api.podbean.com/v1/files/uploadAuthorize"
    params = {
        "access_token": access_token,
        "filename": filename,
        "filesize": file_size,
        "content_type": content_type
    }

    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        return data.get("presigned_url"), data.get("file_key")
    else:
        # Handle API request failure
        print(f"Error: {response.text}")
        sys.exit(0)


def check_and_refresh_access_token():
    # Connect to SQLite database
    conn = db_create_connection()
    cursor = conn.cursor()

    # Retrieve access token and expiration from database
    cursor.execute("SELECT client_id, client_secret, access_token, expire FROM podbean_auth")
    row = cursor.fetchone()
    if row:
        client_id, client_secret, access_token, expire_timestamp = row
        expire_datetime = datetime.fromtimestamp(expire_timestamp)

        # Check if access token is expired
        if datetime.now() < expire_datetime:
            return access_token
    else:
        print('Error accessing any saved auth code')
        sys.exit(0)

    auth_url = "https://api.podbean.com/v1/oauth/multiplePodcastsToken"
    auth_data = {
        "grant_type": "client_credentials"
    }
    auth_headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    auth_response = requests.post(auth_url, auth=(client_id, client_secret), data=auth_data, headers=auth_headers)

    if auth_response.status_code == 200:
        auth_data = auth_response.json()
        new_access_token = auth_data.get("access_token")
        expires_in = auth_data.get("expires_in")

        # Save new access token and expiration time in database
        new_expire_datetime = datetime.now() + timedelta(seconds=expires_in)
        cursor.execute("REPLACE INTO podbean_auth (client_id, client_secret, access_token, expire) VALUES (?, ?, ?, ?)",
                       (client_id, client_secret, new_access_token, new_expire_datetime.timestamp()))
        conn.commit()
        conn.close()
        return new_access_token
    else:
        conn.close()
        print(f"Failed to refresh access token: {auth_response.text}")
        sys.exit(0)


def upload_file_via_presigned_url(presigned_url, file_path):
    with open(file_path, 'rb') as file:
        response = requests.put(presigned_url, data=file)
        if response.status_code == 200:
            print(f"File uploaded successfully. ]{response.text}[")
        else:
            print(f"Upload failed with status code: {response.status_code} - {response.text}")
            sys.exit(0)


def get_list_of_videos():
    payload = {}
    headers = {}
    channel_id = db_get_secret('google_channel')
    google_api = db_get_secret('google_api_key')

    publish_time = datetime.utcnow() - timedelta(days=1)
    # Convert to RFC 3339 format
    publish_time_rfc3339 = publish_time.isoformat() + 'Z'

    url = (
        "https://youtube.googleapis.com/youtube/v3/search?"
        f"part=id&part=snippet&channelId={channel_id}&"
        f"order=date&type=video&key={google_api}&"
        f"publishedAfter={publish_time_rfc3339}"
    )

    response = requests.request("GET", url, headers=headers, data=payload)

    data = json.loads(response.text)
    video_ids = [item['id']['videoId'] for item in data['items']]
    return video_ids


def get_transcripts(video_ids):
    concatenated_transcripts = ''

    for video_id in video_ids:
        try:
            # Get transcript for the current video
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)

            # Concatenate text fields into a single string
            for transcript_item in transcript_list:
                concatenated_transcripts += transcript_item['text'] + ' '
        except Exception as e:
            print(f"Error processing video with ID {video_id}: {str(e)}")
            sys.exit()
    return concatenated_transcripts


def product_script(transcription_text, api_key):
    transcription_text = trim_string(transcription_text, 10000 - 1500)
    # Prepare payload for ChatGPT API
    payload = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "system",
                "content": "You are Heather a 40 something year old, left wing podcaster focused on US politics, "
                           "your audience are 35 to 54 years old, living in the US and are worried about the current "
                           "state of American politics. Politically they are on the left of the democrat party and "
                           "worry about how their children, or more generally, younger people, will cope. You produce "
                           "scripts for podcasts, all scripts must be free of notes or direction and only contain the "
                           "words to be read out loud. Washington Watch, your podcast, is a concise podcast "
                           "delivering timely insights into U.S. political developments from the nation's capital. "
                           "Each episode, lasting under 5 minutes or under 4096 characters, provides a succinct overview of key events, "
                           "policy updates, and political analysis relevant to Washington, D.C. Stay informed with "
                           "daily episodes that offer a quick, digestible snapshot of the latest in American politics. "
                           "Please do not include any name within the script other than the podcast name Washington Watch"
            },
            {
                "role": "user",
                "content": f"please rewrite the following text as a single script, lasting no more than 10 "
                           f"minutes. Please expand upon any complex concepts but keep it short/n'{transcription_text}'"
            }
        ]
    }

    # Prepare headers with bearer token
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Make API request to ChatGPT
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    # Extract processed text from response
    try:
        processed_text = response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"{str(e)}")
        print(response.json())
        sys.exit()

    return processed_text


def text_to_speech(processed_text, api_key):
    # Prepare payload for TTS API
    payload = {
        "model": "tts-1",
        "input": processed_text,
        "voice": "nova"
    }

    # Prepare headers with bearer token
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"  # Request MP3 format response
    }

    # Make API request to TTS API
    response = requests.post("https://api.openai.com/v1/audio/speech", headers=headers, json=payload)

    # Save the resulting mp3 file
    if response.status_code == 200:
        # Determine content type from response headers
        content_type = response.headers.get('Content-Type', '')

        # Check if the response is in MP3 format
        if 'audio/mpeg' in content_type:
            audio_data = response.content  # Use the MP3 data directly
        elif 'audio/wav' in content_type:
            # Convert WAV to MP3 using pydub
            audio = AudioSegment.from_wav(response.content)
            audio_data = audio.export(format="mp3").read()
        else:
            raise ValueError(f"Unsupported content type: {content_type}")

        # Save the resulting MP3 file
        current_date = datetime.now().strftime("%Y-%m-%d")
        dir_path = os.path.dirname(os.path.realpath(__file__))
        output_file = f"{dir_path}/show-{current_date}.mp3"
        with open(output_file, 'wb') as f:
            f.write(audio_data)
    else:
        raise Exception(f"API request failed with status code: {response.status_code} - {response.text}")


def merge_files(episode):
    current_date = datetime.now().strftime("%Y-%m-%d")

    in_file = f"show-{current_date}.mp3"
    out_file = f"episode-{episode}-{current_date}.mp3"
    dir_path = os.path.dirname(os.path.realpath(__file__))
    part1 = f"{dir_path}/Part1.{generate_filename()}"
    part3 = f"{dir_path}/Part3.{generate_filename()}"

    os.system(
        f'ffmpeg -i {part1} -i {dir_path}/{in_file} -i {part3} -filter_complex "concat=n=3:v=0:a=1[out]" -map "[out]" {dir_path}/{out_file}')
    time.sleep(5)
    if not os.path.exists(f'{dir_path}/{out_file}'):
        print('File merge failed, please run the following command to find out why')
        print(
            f'ffmpeg -i part1.mp3 -i {in_file} -i part3.mp3 -filter_complex "concat=n=3:v=0:a=1[out]" -map "[out]" {out_file}')
        sys.exit(0)

    return out_file


def create_podcast_episode(access_token, title, content, media_key, episode_number, publish_timestamp):
    url = "https://api.podbean.com/v1/episodes"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "access_token": access_token,
        "title": title,
        "content": content,
        "status": "publish",
        "type": "public",
        "media_key": media_key,
        "episode_number": episode_number,
        "apple_episode_type": "full",
        "publish_timestamp": publish_timestamp,
        "content_explicit": "clean"
    }

    response = requests.post(url, headers=headers, data=data)

    if response.status_code == 200:
        print("Episode created successfully!")
    else:
        print(f"Failed to create episode. Status code: {response.status_code}")
        print(response.text)  # Print error message if available


def upload_file(out_file, episode, description):
    dir_path = os.path.dirname(os.path.realpath(__file__))
    file_size = os.path.getsize(f"{dir_path}/{out_file}")
    filename = out_file
    content_type = 'audio/mpeg'
    access_token = check_and_refresh_access_token()
    presigned_url, file_key = get_presigned_url(access_token, filename, file_size, content_type)
    upload_file_via_presigned_url(presigned_url, f"{dir_path}/{out_file}")
    date = datetime.now().strftime("%B %d, %Y")
    title = f'Washington Watch Ep. {episode} {date}'
    current_unix_time = int(time.time())
    create_podcast_episode(access_token, title, description, file_key, episode, current_unix_time)
    # https://developers.podbean.com/podbean-api-docs/#api-Episode-Publish_New_Episode


def product_description(script, api_key):
    script = trim_string(script, 10000 - 2000)
    # Prepare payload for ChatGPT API
    payload = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "system",
                "content": "You are Heather a 40 something year old, left wing podcaster focused on US politics, "
                           "your audience are 35 to 54 years old, living in the US and are worried about the current "
                           "state of American politics. Politically they are on the left of the democrat party and "
                           "worry about how their children, or more generally, younger people, will cope. You produce "
                           "scripts for podcasts, all scripts must be free of notes or direction and only contain the "
                           "words to be read out loud. Washington Watch, your podcast, is a concise podcast "
                           "delivering timely insights into U.S. political developments from the nation's capital. "
                           "Each episode, lasting under 5 minutes, provides a succinct overview of key events, "
                           "policy updates, and political analysis relevant to Washington, D.C. Stay informed with "
                           "daily episodes that offer a quick, digestible snapshot of the latest in American politics."
            },
            {
                "role": "user",
                "content": f"please provide a description for the following podcast script, no longer than 400 characters in length /n '{script}'"
            }
        ]
    }

    # Prepare headers with bearer token
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Make API request to ChatGPT
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    # Extract processed text from response
    try:
        processed_text = response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"{str(e)}")
        print(response.json())
        sys.exit()

    return trim_text(processed_text, 500)


def clean_up(out_file):
    dir_path = os.path.dirname(os.path.realpath(__file__))
    os.remove(f"{dir_path}/{out_file}")
    current_date = datetime.now().strftime("%Y-%m-%d")
    in_file = f"{dir_path}/show-{current_date}.mp3"
    os.remove(in_file)


def main():
    episode = db_return_next_episode()
    api_key = db_get_secret('openai_api')
    video_ids = get_list_of_videos()
    transcript = get_transcripts(video_ids)
    script = product_script(transcript, api_key)
    text_to_speech(script, api_key)
    out_file = merge_files(episode)
    description = product_description(script, api_key)
    upload_file(out_file, episode, description)
    db_insert_episode(description)
    clean_up(out_file)


if __name__ == "__main__":
    main()
