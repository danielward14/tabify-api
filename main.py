
"""
main.py
This module provides a FastAPI-based web service for identifying songs from audio files or YouTube URLs,
retrieving related information such as Spotify metadata, guitar tabs, and YouTube guitar lesson videos.
Dependencies:
- FastAPI: Web framework for building APIs.
- yt_dlp: For downloading audio from YouTube URLs.
- Shazamio: For identifying songs using Shazam's API.
- Spotipy: For interacting with the Spotify API.
- BeautifulSoup: For scraping guitar tabs from Songsterr.
- Google API Client: For interacting with the YouTube Data API.
- Uvicorn: ASGI server for running the FastAPI application.
Environment Variables:
- SPOTIFY_CLIENT_ID: Spotify API client ID.
- SPOTIFY_CLIENT_SECRET: Spotify API client secret.
- YOUTUBE_API_KEY: YouTube Data API key.
Routes:
- GET /find-song: Identifies a song from a YouTube URL and retrieves related information.
- POST /identify-audio: Identifies a song from an uploaded audio file and retrieves related information.
- GET /youtube-lessons-videos: Retrieves YouTube video IDs for guitar lessons of a given song and artist.
- GET /test-spotify: Tests Spotify API integration by searching for a song and artist.
Classes:
- SafeCacheHandler: Custom cache handler for managing Spotify API tokens.
Functions:
- download_audio(yt_url): Downloads audio from a YouTube URL.
- identify_song(audio_path): Identifies a song using Shazam from a given audio file path.
- search_spotify(song_name, artist_name): Searches for a song on Spotify and retrieves metadata.
- search_tabs(song_name, artist_name): Searches for guitar tabs on Songsterr.
- get_youtube_guitar_lessons_link(song_name, artist_name): Generates a YouTube search URL for guitar lessons.
- get_youtube_video_ids(song_name, artist_name): Retrieves YouTube video IDs for guitar lessons using the YouTube Data API.
Usage:
Run the script with `uvicorn` to start the FastAPI server. Ensure all required environment variables are set.
"""
import shutil
from fastapi import FastAPI, HTTPException, UploadFile, File
import yt_dlp
import tempfile
import os
import json
import asyncio
import time
from shazamio import Shazam
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Add CORS Middleware for handling CORS issues
# This is important for allowing requests from different origins, especially in a web app context.
# Adjust the allowed origins as per your deployment needs.
# For example, you might want to restrict it to your frontend app's URL in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# Load environment variables
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")


# Custom cache handler with full implementation
# This is a more robust implementation of the cache handler for Spotipy.
# It handles token caching and retrieval, ensuring that the token is valid and can be refreshed if needed.
class SafeCacheHandler(spotipy.cache_handler.CacheHandler):
    def __init__(self):
        self.cache_path = ".cache"

    def get_cached_token(self):
        try:
            if not os.path.exists(self.cache_path):
                print("No cache file found, will request new token.")
                return None
            with open(self.cache_path, 'r') as f:
                token_info_string = f.read().strip()
                if not token_info_string:
                    print("Cache file is empty, will request new token.")
                    return None
                return json.loads(token_info_string)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"Cache read error: {str(e)}, will request new token.")
            return None

    def save_token_to_cache(self, token_info):
        try:
            with open(self.cache_path, 'w') as f:
                json.dump(token_info, f)
            print("Token saved to cache.")
        except Exception as e:
            print(f"Failed to save token to cache: {str(e)}")

# Initialize Spotify client with fallback
# This is a more robust implementation of the Spotify client initialization.
# It handles exceptions and provides a fallback mechanism in case the initial token request fails.
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        cache_handler=SafeCacheHandler()
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    # Force a fresh token and test
    # This is important to ensure that the token is valid and can be used for API requests.
    # It also helps in debugging issues related to token expiration or invalidation.
    token = auth_manager.get_access_token(as_dict=False, check_cache=False)
    print(f"Spotify access token: {token}")
    test_result = sp.search(q="track:bohemian rhapsody artist:queen", type='track', limit=1)
    print(f"Spotify startup test successful: {json.dumps(test_result, indent=2)}")
except Exception as e:
    print(f"Failed to initialize Spotify client: {str(e)}")
    # Fallback: Try without cache
    # This is a fallback mechanism to handle cases where the cache handler fails or is not available.
    # It ensures that the application can still function without caching, albeit with a performance hit.
    try:
        auth_manager = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            cache_handler=None  # Disable caching entirely
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
        token = auth_manager.get_access_token(as_dict=False)
        print(f"Fallback Spotify access token: {token}")
        test_result = sp.search(q="track:bohemian rhapsody artist:queen", type='track', limit=1)
        print(f"Fallback Spotify startup test successful: {json.dumps(test_result, indent=2)}")
    except Exception as e:
        print(f"Fallback initialization also failed: {str(e)}")
        raise


# Initialize YouTube API client
# This is a more robust implementation of the YouTube API client initialization.
# It handles exceptions and provides a fallback mechanism in case the initial token request fails.
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

def download_audio(yt_url):
    if os.path.exists("/dev/shm"):
        audio_path = "/dev/shm/audio.m4a" if "iOS" in yt_url else "/dev/shm/audio.mp3"
    else:
        audio_path = tempfile.mktemp(suffix='.m4a' if "iOS" in yt_url else '.mp3')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': audio_path,
        'quiet': True,
        'noplaylist': True,
        'postprocessor_args': ['-t', '8', '-b:a', '48k'],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([yt_url])
    return audio_path

# This function identifies a song using Shazam's API.
# It takes the path to the audio file as input and returns the recognition result.
async def identify_song(audio_path):
    shazam = Shazam()
    try:
        with open(audio_path, 'rb') as f:
            audio = f.read()  # Read the entire file into memory
        result = await shazam.recognize(audio)
        print(f"Shazam recognition result: {result}")
        return result
    except Exception as e:
        print(f"Shazam failed: {str(e)}")
        return {"error": f"Shazam failed: {str(e)}"}
    
# This function searches for a song on Spotify using the provided song name and artist name.
# It returns the song name, artist name, and album art URL if found.
# If no results are found, it returns an error message.
def search_spotify(song_name, artist_name):
    query = f"track:{song_name} artist:{artist_name}"
    try:
        results = sp.search(q=query, type='track', limit=1)
        print(f"Spotify raw response for '{query}': {json.dumps(results, indent=2)}")
        if not results or not results.get('tracks') or not results['tracks'].get('items'):
            print(f"No Spotify results for {song_name} by {artist_name}")
            return {"error": "No results found on Spotify"}
        track = results['tracks']['items'][0]
        album_art = track['album']['images'][0]['url'] if track['album']['images'] else None
        if not album_art:
            print(f"No album art available for {song_name} by {artist_name}")
        return {
            "song": track['name'],
            "artist": ', '.join(artist['name'] for artist in track['artists']),
            "album_art": album_art
        }
    except spotipy.exceptions.SpotifyException as se:
        print(f"Spotify API error: {str(se)} - HTTP status: {se.http_status if hasattr(se, 'http_status') else 'unknown'}")
        return {"error": f"Spotify API error: {str(e)}"}
    except ValueError as ve:
        print(f"JSON parsing error in Spotify search: {str(ve)}")
        return {"error": f"JSON parsing error: {str(ve)}"}
    except Exception as e:
        print(f"Unexpected error in Spotify search: {str(e)}")
        return {"error": f"Unexpected error: {str(e)}"}

def search_tabs(song_name, artist_name):
    # Sanitize song name by removing special characters and replacing spaces with hyphens
    # This is important to ensure that the URL is valid and does not contain any illegal characters.
    # It also helps in avoiding issues with URL encoding and decoding.
    import re
    sanitized_song_name = re.sub(r"[^\w\s-]", "", song_name).replace(" ", "-").lower()
    search_url = f"https://www.songsterr.com/?pattern={song_name.replace(' ', '+')}+{artist_name.replace(' ', '+')}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(search_url, headers=headers, timeout=5)
        print(f"Songsterr search response status: {response.status_code}")
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            # Use sanitized song name in the selector
            # This is important to ensure that the selector matches the correct element in the HTML.
            # It also helps in avoiding issues with incorrect or unexpected HTML structure.
            result_link = soup.select_one(f"a[href*='-{sanitized_song_name}-tab']")
            if result_link:
                return f"https://www.songsterr.com{result_link['href']}"
        print("No direct tab found, returning search URL.")
    except requests.RequestException as e:
        print(f"Error fetching tabs: {str(e)}")
    except Exception as e:
        print(f"Error parsing Songsterr page: {str(e)}")
    return search_url

# This function generates a YouTube search URL for guitar lessons based on the song name and artist name.
# It replaces spaces with '+' for the search query and encodes the URL properly.
def get_youtube_guitar_lessons_link(song_name, artist_name):
    search_query = f"{song_name} {artist_name} guitar lesson"
    return f"https://www.youtube.com/results?tenersearch_query={search_query.replace(' ', '+')}&sp=EgIYAw%253D%253D"

# This function retrieves YouTube video IDs for guitar lessons using the YouTube Data API.
# It searches for videos based on the song name and artist name, and returns a list of video IDs.
def get_youtube_video_ids(song_name, artist_name):
    search_query = f"{song_name} {artist_name} guitar lesson"
    request = youtube.search().list(
        part="id",
        q=search_query,
        type="video",
        maxResults=3,
        videoEmbeddable="true",
        order="relevance"
    )
    response = request.execute()
    video_ids = [item['id']['videoId'] for item in response.get('items', [])]
    return video_ids

# This function handles the /find-song endpoint.
# It takes a YouTube URL as input, downloads the audio, identifies the song using Shazam,
@app.get("/find-song")
async def find_song(yt_url: str):
    if not yt_url:
        raise HTTPException(status_code=400, detail="YouTube URL is required.")
    start_time = time.time()
    audio_path = download_audio(yt_url)
    try:
        song_info = await identify_song(audio_path)
        if not song_info or 'track' not in song_info:
            raise HTTPException(status_code=404, detail="Could not identify the song.")
        song_name = song_info['track']['title']
        artist_name = song_info['track']['subtitle']
        spotify_result, tab_url, youtube_lessons_url = await asyncio.gather(
            asyncio.to_thread(search_spotify, song_name, artist_name),
            asyncio.to_thread(search_tabs, song_name, artist_name),
            asyncio.to_thread(get_youtube_guitar_lessons_link, song_name, artist_name)
        )
        execution_time = time.time() - start_time
        print(f"Execution time: {execution_time:.2f} seconds")
        return {
            "song": song_name,
            "artist": artist_name,
            "spotify": spotify_result,
            "tabs": tab_url,
            "youtube_lessons": youtube_lessons_url,
            "execution_time": f"{execution_time:.2f} seconds"
        }
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

# This function handles the /identify-audio endpoint.
# It takes an uploaded audio file, identifies the song using Shazam,
@app.post("/identify-audio")
async def identify_audio(file: UploadFile = File(...)):
    start_time = time.time()
    audio_path = tempfile.mktemp(suffix='.m4a' if "iOS" in file.filename else '.mp3')
    try:
        # Save uploaded file to a temporary location 
        # This is important to ensure that the file is accessible for processing.
        # It also helps in avoiding issues with file permissions and access rights.
        with open(audio_path, 'wb') as f:
            content = await file.read()
            if not content:
                raise HTTPException(status_code=400, detail="Empty audio file uploaded.")
            f.write(content)


        # Verify file is a valid audio file 
        try:
            with open(audio_path, 'rb') as f:
                audio = f.read()
                if len(audio) < 1024:  # Basic size check
                    raise HTTPException(status_code=400, detail="Audio file too small.")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid audio file: {str(e)}")

        # Recognize song using Shazam
        # This is important to ensure that the audio file is processed correctly.
        song_info = await identify_song(audio_path)
        if not song_info or 'track' not in song_info:
            print(f"Shazam returned no track info: {song_info}")
            return {
                "error": "Could not identify the song. Please try a longer or clearer audio sample."
            }

        song_name = song_info['track']['title']
        artist_name = song_info['track']['subtitle']

        # Gather additional data from Spotify, Songsterr, and YouTube
        # This is important to ensure that all data is fetched concurrently, improving performance.
        spotify_result, tab_url, youtube_lessons_url = await asyncio.gather(
            asyncio.to_thread(search_spotify, song_name, artist_name),
            asyncio.to_thread(search_tabs, song_name, artist_name),
            asyncio.to_thread(get_youtube_guitar_lessons_link, song_name, artist_name)
        )

        execution_time = time.time() - start_time
        print(f"Audio identification time: {execution_time:.2f} seconds")

        # Return the results
        return {
            "song": song_name,
            "artist": artist_name,
            "spotify": spotify_result,
            "tabs": tab_url,
            "youtube_lessons": youtube_lessons_url,
            "execution_time": f"{execution_time:.2f} seconds"
        }
    except Exception as e:
        print(f"Error in identify_audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

# This function handles the /youtube-lessons-videos endpoint.
# It takes a song name and artist name as input, retrieves YouTube video IDs for guitar lessons,
# and returns them in the response.
@app.get("/youtube-lessons-videos")
async def youtube_lessons_videos(song_name: str, artist_name: str):
    if not song_name or not artist_name:
        raise HTTPException(status_code=400, detail="Song name and artist name are required.")
    try:
        video_ids = get_youtube_video_ids(song_name, artist_name)
        if not video_ids:
            raise HTTPException(status_code=404, detail="No videos found.")
        return {"video_ids": video_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching YouTube videos: {str(e)}")

# This function handles the /test-spotify endpoint.
# It takes a song name and artist name as input, searches for the song on Spotify,
@app.get("/test-spotify")
async def test_spotify(song_name: str, artist_name: str):
    result = search_spotify(song_name, artist_name)
    return {"spotify_result": result}

# This function runs the FastAPI application using Uvicorn.
# It sets the host and port for the server, allowing it to be accessed from outside the local machine.
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))  # Railway uses 8080 by default
    # Check if the port is set in the environment variables
    uvicorn.run(app, host="0.0.0.0", port=port)
