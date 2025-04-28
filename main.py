import shutil
import threading
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
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
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from dotenv import load_dotenv
from bson import ObjectId
import httpx
from fastapi import APIRouter

router = APIRouter()
app = FastAPI()

app.include_router(router)


# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


GITHUB_REPO_TREE_URL = "https://api.github.com/repos/OpenTabOrg/opentab/git/trees/main?recursive=1"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/OpenTabOrg/opentab/main"


SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")


mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client['TabifyDB'] # Database name
tabs_collection = db['tabs'] # Collection where we will save tabs

def send_refresh_request():
    url = "https://web-production-7ba9.up.railway.app/refresh-tabs"
    try:
        response = requests.post(url)
        print(f"Request sent! Status: {response.status_code}")
    except Exception as e:
        print(f"Error sending request: {e}")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=send_refresh_request).start()
    yield  # Lifespan context
    # Add any cleanup code here if needed

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def read_root():
    return {"message": "FastAPI server running"}


@router.post("/refresh-tabs")
async def refresh_tabs():
    try:
        async with httpx.AsyncClient() as client:
            # Step 1: Get all .txt file paths from GitHub repo
            tree_resp = await client.get(GITHUB_REPO_TREE_URL)
            tree_data = tree_resp.json()

            tab_files = [item['path'] for item in tree_data.get('tree', []) if item['path'].endswith('.txt')]
            print(f"Found {len(tab_files)} tab files.")

            imported_count = 0

            # Step 2: For each tab file, fetch the content and store it
            for file_path in tab_files:
                full_url = f"{GITHUB_RAW_URL}/{file_path}"
                file_resp = await client.get(full_url)
                tab_text = file_resp.text

                # Parse artist and song name from path
                parts = file_path.split('/')
                if len(parts) != 2:
                    continue  # skip weird files

                artist_name = parts[0].replace('-', ' ').title()
                song_name = parts[1].replace('.txt', '').replace('-', ' ').title()

                # Insert into MongoDB
                new_tab = {
                    "artist": artist_name,
                    "title": song_name,
                    "tab_text": tab_text
                }
                await tabs_collection.insert_one(new_tab)  # Make sure you have your Mongo collection ready

                imported_count += 1

        return {"message": f"Successfully imported {imported_count} tabs!"}
    except Exception as e:
        print("Error refreshing tabs:", str(e))
        return {"error": str(e)}


# Fetch and Save tabs to MongoDB
class TabItem(BaseModel):
    title: str
    artist: str
    tab_text: str

@app.post("/save-tab")
async def save_tab(tab: TabItem):
    new_tab = tab.model_dump()  # safe and correct in Pydantic 2
    result = await tabs_collection.insert_one(new_tab)
    return {"message": "Tab saved", "id": str(result.inserted_id)}

@app.get("/get-tabs")
async def get_tabs(artist: str = Query(None), song: str = Query(None)):
    query = {}
    if artist:
        query["artist"] = {"$regex": artist, "$options": "i"}  # case-insensitive
    if song:
        query["title"] = {"$regex": song, "$options": "i"}

    tabs = await tabs_collection.find(query).to_list(50)  # limit 50 results
    for tab in tabs:
        tab["_id"] = str(tab["_id"])  # convert ObjectId to string for JSON
    return tabs


@router.get("/tabs")
async def get_tab(artist: str = Query(...), title: str = Query(...)):
    try:
        # Normalize inputs
        artist_clean = artist.strip().lower()
        title_clean = title.strip().lower()

        # Search in MongoDB
        tab = await tabs_collection.find_one({
            "artist": {"$regex": f"^{artist_clean}$", "$options": "i"},
            "title": {"$regex": f"^{title_clean}$", "$options": "i"}
        })

        if not tab:
            return {"error": "Tab not found"}

        return {
            "artist": tab["artist"],
            "title": tab["title"],
            "tab_text": tab["tab_text"]
        }
    except Exception as e:
        print("Error fetching tab:", str(e))
        return {"error": str(e)}

# Custom cache handler with full implementation
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
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        cache_handler=SafeCacheHandler()
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    # Force a fresh token and test
    token = auth_manager.get_access_token(as_dict=False, check_cache=False)
    print(f"Spotify access token: {token}")
    test_result = sp.search(q="track:bohemian rhapsody artist:queen", type='track', limit=1)
    print(f"Spotify startup test successful: {json.dumps(test_result, indent=2)}")
except Exception as e:
    print(f"Failed to initialize Spotify client: {str(e)}")
    # Fallback: Try without cache
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

async def identify_song(audio_path):
    shazam = Shazam()
    try:
        with open(audio_path, 'rb') as f:
            audio = f.read()  # Read the entire file
        result = await shazam.recognize(audio)
        print(f"Shazam recognition result: {result}")
        return result
    except Exception as e:
        print(f"Shazam failed: {str(e)}")
        return {"error": f"Shazam failed: {str(e)}"}

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
            result_link = soup.select_one(f"a[href*='-{sanitized_song_name}-tab']")
            if result_link:
                return f"https://www.songsterr.com{result_link['href']}"
        print("No direct tab found, returning search URL.")
    except requests.RequestException as e:
        print(f"Error fetching tabs: {str(e)}")
    except Exception as e:
        print(f"Error parsing Songsterr page: {str(e)}")
    return search_url

def get_youtube_guitar_lessons_link(song_name, artist_name):
    search_query = f"{song_name} {artist_name} guitar lesson"
    return f"https://www.youtube.com/results?tenersearch_query={search_query.replace(' ', '+')}&sp=EgIYAw%253D%253D"

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

        spotify_result, youtube_lessons_url, tab_doc = await asyncio.gather(
        asyncio.to_thread(search_spotify, song_name, artist_name),
        asyncio.to_thread(get_youtube_guitar_lessons_link, song_name, artist_name),
        tabs_collection.find_one({
            "artist": {"$regex": f"^{artist_name}$", "$options": "i"},
            "title": {"$regex": f"^{song_name}$", "$options": "i"}
            })
        )

        tab_text = tab_doc["tab_text"] if tab_doc else None


        execution_time = time.time() - start_time
        print(f"Execution time: {execution_time:.2f} seconds")
        return {
            "song": song_name,
            "artist": artist_name,
            "spotify": spotify_result,
            "tabs": tab_text,
            "youtube_lessons": youtube_lessons_url,
            "execution_time": f"{execution_time:.2f} seconds"
        }
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

@app.post("/identify-audio")
async def identify_audio(file: UploadFile = File(...)):
    start_time = time.time()
    audio_path = tempfile.mktemp(suffix='.m4a' if "iOS" in file.filename else '.mp3')
    try:
        # Save uploaded file
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

        # Recognize song
        song_info = await identify_song(audio_path)
        if not song_info or 'track' not in song_info:
            print(f"Shazam returned no track info: {song_info}")
            return {
                "error": "Could not identify the song. Please try a longer or clearer audio sample."
            }

        song_name = song_info['track']['title']
        artist_name = song_info['track']['subtitle']

        # Gather additional data
        spotify_result, youtube_lessons_url, tab_doc = await asyncio.gather(
        asyncio.to_thread(search_spotify, song_name, artist_name),
        asyncio.to_thread(get_youtube_guitar_lessons_link, song_name, artist_name),
        tabs_collection.find_one({
            "artist": {"$regex": f"^{artist_name}$", "$options": "i"},
            "title": {"$regex": f"^{song_name}$", "$options": "i"}
        })
        )

        tab_text = tab_doc["tab_text"] if tab_doc else None


        execution_time = time.time() - start_time
        print(f"Audio identification time: {execution_time:.2f} seconds")
        return {
            "song": song_name,
            "artist": artist_name,
            "spotify": spotify_result,
            "tabs": tab_text,
            "youtube_lessons": youtube_lessons_url,
            "execution_time": f"{execution_time:.2f} seconds"
        }
    except Exception as e:
        print(f"Error in identify_audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

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

@app.get("/test-spotify")
async def test_spotify(song_name: str, artist_name: str):
    result = search_spotify(song_name, artist_name)
    return {"spotify_result": result}

@app.get("/search-song")
async def search_song(song_name: str, artist_name: str):
    try:
        spotify_result, youtube_lessons_url = await asyncio.gather(
            asyncio.to_thread(search_spotify, song_name, artist_name),
            asyncio.to_thread(get_youtube_guitar_lessons_link, song_name, artist_name)
        )

        # Try fetching tabs from MongoDB
        tab_doc = await tabs_collection.find_one({
            "artist": {"$regex": f"^{artist_name}$", "$options": "i"},
            "title": {"$regex": f"^{song_name}$", "$options": "i"}
        })

        tab_text = tab_doc["tab_text"] if tab_doc else None

        return {
            "song": song_name,
            "artist": artist_name,
            "spotify": spotify_result,
            "tabs": tab_text,
            "youtube_lessons": youtube_lessons_url,
        }
    except Exception as e:
        print(f"Error in search_song: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))  # Railway uses 8080
    uvicorn.run(app, host="0.0.0.0", port=port)
