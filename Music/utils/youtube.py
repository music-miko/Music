import os
import re
import time
import asyncio
import uuid
import aiohttp
import aiofiles
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional, Any

import requests
import yt_dlp
from lyricsgenius import Genius
from pyrogram.types import CallbackQuery
from youtubesearchpython.__future__ import VideosSearch

from config import Config
from Music.core.clients import hellbot
from Music.core.logger import LOGS
from Music.helpers.strings import TEXTS

# === API INTEGRATION HELPERS ===

def is_safe_url(text: str) -> bool:
    DANGEROUS_CHARS = [
        ";", "|", "$", "`", "\n", "\r", 
        "&", "(", ")", "<", ">", "{", "}", 
        "\\", "'", '"'
    ]
    ALLOWED_DOMAINS = {
        "youtube.com", "www.youtube.com", "m.youtube.com", 
        "youtu.be", "music.youtube.com"
    }
    
    if not text: return False
    is_url = text.strip().lower().startswith(("http:", "https:", "www."))
    if not is_url: return True
    try:
        target_url = text.strip()
        if target_url.lower().startswith("www."):
            target_url = "https://" + target_url
        decoded_url = unquote(target_url)
        if any(char in decoded_url for char in DANGEROUS_CHARS):
            LOGS.warning(f"🚫 Blocked URL (Dangerous Chars): {text}")
            return False
        p = urlparse(target_url)
        if p.netloc.replace("www.", "") not in ALLOWED_DOMAINS:
            LOGS.warning(f"🚫 Blocked URL (Invalid Domain): {p.netloc}")
            return False
        return True
    except Exception as e:
        LOGS.error(f"URL Parsing Error: {e}")
        return False

def extract_safe_id(link: str) -> Optional[str]:
    YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
    try:
        if "v=" in link: vid = link.split("v=")[-1].split("&")[0]
        elif "youtu.be" in link: vid = link.split("/")[-1].split("?")[0]
        else: return None
        if YOUTUBE_ID_RE.match(vid): return vid
    except: pass
    return None

def cookie_txt_file():
    cookie_path = os.path.join(os.getcwd(), "cookies", "cookies.txt")
    if os.path.exists(cookie_path):
        return cookie_path
    return None

_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()

async def get_http_session() -> aiohttp.ClientSession:
    global _session
    HARD_TIMEOUT = 80
    
    if _session and not _session.closed:
        return _session
    async with _session_lock:
        if _session and not _session.closed:
            return _session
        timeout = aiohttp.ClientTimeout(total=HARD_TIMEOUT, sock_connect=10, sock_read=30)
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, enable_cleanup_closed=True)
        _session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return _session

def _looks_like_status_text(s: Optional[str]) -> bool:
    if not s: return False
    low = s.lower()
    return any(x in low for x in ("download started", "background", "jobstatus", "job_id", "processing", "queued"))

def _extract_candidate(obj: Any) -> Optional[str]:
    if obj is None: return None
    if isinstance(obj, str):
        s = obj.strip()
        return s if s else None
    if isinstance(obj, list) and obj:
        return _extract_candidate(obj[0])
    if isinstance(obj, dict):
        job = obj.get("job")
        if isinstance(job, dict):
            res = job.get("result")
            if isinstance(res, dict):
                for k in ("public_url", "cdnurl", "download_url", "url"):
                    v = res.get(k)
                    if isinstance(v, str) and v.strip(): return v.strip()
        for k in ("public_url", "cdnurl", "download_url", "url", "tg_link"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip(): return v.strip()
        for wrap in ("result", "results", "data", "items"):
            v = obj.get(wrap)
            if v: return _extract_candidate(v)
    return None

def _normalize_url(candidate: str) -> Optional[str]:
    api_url = getattr(Config, "API_URL", None)
    if not api_url or not candidate: return None
    c = candidate.strip()
    if c.startswith(("http://", "https://")): return c
    if c.startswith("/"):
        if c.startswith(("/root", "/home")): return None
        return f"{api_url.rstrip('/')}{c}"
    return f"{api_url.rstrip('/')}/{c.lstrip('/')}"

async def _download_cdn(url: str, out_path: str) -> bool:
    CHUNK_SIZE = 1024 * 1024
    CDN_RETRIES = 5
    CDN_RETRY_DELAY = 2
    HARD_TIMEOUT = 80
    
    LOGS.info(f"🔗 Downloading from CDN: {url}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, CDN_RETRIES + 1):
        try:
            session = await get_http_session()
            async with session.get(url, timeout=HARD_TIMEOUT) as resp:
                if resp.status != 200:
                    if attempt < CDN_RETRIES:
                        await asyncio.sleep(CDN_RETRY_DELAY)
                        continue
                    return False
                async with aiofiles.open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        if not chunk: break
                        await f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return True
        except asyncio.TimeoutError:
            if attempt < CDN_RETRIES: await asyncio.sleep(CDN_RETRY_DELAY)
        except Exception as e:
            LOGS.error(f"CDN Fail: {e}")
            if attempt < CDN_RETRIES: await asyncio.sleep(CDN_RETRY_DELAY)
    return False

async def v2_download_process(link: str, video: bool) -> Optional[str]:
    V2_DOWNLOAD_CYCLES = 5
    V2_HTTP_RETRIES = 5
    NO_CANDIDATE_WAIT = 4
    JOB_POLL_ATTEMPTS = 15     
    JOB_POLL_INTERVAL = 2.0    
    JOB_POLL_BACKOFF = 1.2
    
    vid = extract_safe_id(link) or link 
    file_id = extract_safe_id(link) or uuid.uuid4().hex[:10]
    ext = "mp4" if video else "m4a"
    out_path = Path("downloads") / f"{file_id}.{ext}"

    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    api_key = getattr(Config, "API_KEY", None)
    api_url = getattr(Config, "API_URL", None)
    if not api_url or not api_key:
        LOGS.error("API Creds Missing")
        return None

    for cycle in range(1, V2_DOWNLOAD_CYCLES + 1):
        try:
            session = await get_http_session()
            url = f"{api_url.rstrip('/')}/youtube/v2/download"
            params = {"query": vid, "isVideo": str(video).lower(), "api_key": api_key}
            LOGS.info(f"📡 API Job Start (Cycle {cycle}): {vid}...")
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    if cycle < V2_DOWNLOAD_CYCLES: await asyncio.sleep(1); continue
                    return None
                data = await resp.json()

            candidate = _extract_candidate(data)
            if candidate and _looks_like_status_text(candidate):
                candidate = None

            job_id = data.get("job_id")
            if isinstance(data.get("job"), dict):
                 job_id = data.get("job").get("id")

            if job_id and not candidate:
                LOGS.info(f"⏳ Polling Job: {job_id}")
                interval = JOB_POLL_INTERVAL
                for _ in range(JOB_POLL_ATTEMPTS):
                    await asyncio.sleep(interval)
                    status_url = f"{api_url.rstrip('/')}/youtube/jobStatus"
                    try:
                        async with session.get(status_url, params={"job_id": job_id}) as s_resp:
                            if s_resp.status == 200:
                                s_data = await s_resp.json()
                                candidate = _extract_candidate(s_data)
                                if candidate and not _looks_like_status_text(candidate):
                                    break
                                job_data = s_data.get("job", {}) if isinstance(s_data, dict) else {}
                                if job_data.get("status") == "error":
                                    LOGS.error(f"❌ Job Error: {job_data.get('error')}")
                                    break
                    except Exception:
                        pass
                    interval *= JOB_POLL_BACKOFF
            
            if not candidate:
                if cycle < V2_DOWNLOAD_CYCLES: await asyncio.sleep(NO_CANDIDATE_WAIT); continue
                return None

            final_url = _normalize_url(candidate)
            if not final_url:
                 if cycle < V2_DOWNLOAD_CYCLES: await asyncio.sleep(NO_CANDIDATE_WAIT); continue
                 return None

            if await _download_cdn(final_url, str(out_path)):
                return str(out_path)
        except Exception as e:
            LOGS.error(f"API Cycle Error: {e}")
            if cycle < V2_DOWNLOAD_CYCLES: await asyncio.sleep(1)
    return None

def _run_ytdlp(link: str, out_path: str, cookie_file: str, format_id: str = None):
    ydl_opts = {
        'format': format_id if format_id else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': out_path,
        'cookiefile': cookie_file,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([link])
    return True

async def yt_dlp_download_video(link: str, format_id: str = None) -> Optional[str]:
    vid = extract_safe_id(link) or uuid.uuid4().hex[:10]
    out_dir = Path("downloads")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{vid}.mp4"
    
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    cookie_file = cookie_txt_file()
    if not cookie_file:
        LOGS.error("No cookie file found for yt-dlp video download.")
        return None

    try:
        LOGS.info(f"Downloading VIDEO via yt-dlp: {vid} (Cookies: {os.path.basename(cookie_file)})")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_ytdlp, link, str(out_path), cookie_file, format_id)
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)
    except Exception as e:
        LOGS.error(f"yt-dlp Video Download Failed (Skipping): {e}")
    return None
# === END API INTEGRATION ===


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.listbase = "https://youtube.com/playlist?list="
        self.regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/|shorts\/)|youtu\.be\/|youtube\.com\/playlist\?list=)"
        self.audio_opts = {"format": "bestaudio[ext=m4a]"}
        self.video_opts = {
            "format": "best",
            "addmetadata": True,
            "key": "FFmpegMetadata",
            "prefer_ffmpeg": True,
            "geo_bypass": True,
            "nocheckcertificate": True,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "outtmpl": "%(id)s.mp4",
            "logtostderr": False,
            "quiet": True,
        }
        self.yt_opts_audio = {
            "format": "bestaudio/best",
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "geo_bypass": True,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
        }
        self.yt_opts_video = {
            "format": "(bestvideo[height<=?720][width<=?1280][ext=mp4])+(bestaudio[ext=m4a])",
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "geo_bypass": True,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
        }
        self.yt_playlist_opts = {
            "exctract_flat": True,
        }
        self.lyrics = Config.LYRICS_API
        try:
            if self.lyrics:
                self.client = Genius(self.lyrics, remove_section_headers=True)
            else:
                self.client = None
        except Exception as e:
            LOGS.warning(f"[Exception in Lyrics API]: {e}")
            self.client = None

    def check(self, link: str):
        return bool(re.match(self.regex, link))

    async def format_link(self, link: str, video_id: bool) -> str:
        link = link.strip()
        if video_id:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        return link

    async def get_data(self, link: str, video_id: bool, limit: int = 1) -> list:
        yt_url = await self.format_link(link, video_id)
        collection = []
        results = VideosSearch(yt_url, limit=limit)
        for result in (await results.next())["result"]:
            vid = result["id"]
            channel = result["channel"]["name"]
            channel_url = result["channel"]["link"]
            duration = result["duration"]
            published = result["publishedTime"]
            thumbnail = f"https://i.ytimg.com/vi/{result['id']}/hqdefault.jpg"
            title = result["title"]
            url = result["link"]
            views = result["viewCount"]["short"]
            context = {
                "id": vid,
                "ch_link": channel_url,
                "channel": channel,
                "duration": duration,
                "link": url,
                "published": published,
                "thumbnail": thumbnail,
                "title": title,
                "views": views,
            }
            collection.append(context)
        return collection[:limit]

    async def get_playlist(self, link: str) -> list:
        yt_url = await self.format_link(link, False)
        with yt_dlp.YoutubeDL(self.yt_playlist_opts) as ydl:
            results = ydl.extract_info(yt_url, False)
            playlist = [video['id'] for video in results['entries']]
        return playlist

    # APPLIED API TO DOWNLOAD
    async def download(self, link: str, video_id: bool, video: bool = False) -> str:
        yt_url = await self.format_link(link, video_id)
        if not is_safe_url(yt_url):
            raise Exception("Unsafe URL blocked.")
            
        if video:
            path = await yt_dlp_download_video(yt_url)
        else:
            path = await v2_download_process(yt_url, video=False)
            
        if not path:
            raise Exception("Download failed via API.")
        return path

    # APPLIED API TO SEND_SONG
    async def send_song(
        self, message: CallbackQuery, rand_key: str, key: int, video: bool = False
    ) -> dict:
        track = Config.SONG_CACHE[rand_key][key]
        hell = await message.message.reply_text("Downloading...")
        await message.message.delete()
        try:
            output = None
            thumb = f"{track['id']}{time.time()}.jpg"
            _thumb = requests.get(track["thumbnail"], allow_redirects=True)
            open(thumb, "wb").write(_thumb.content)
            
            # Use the new API-powered download method instead of yt_dlp
            output = await self.download(track["link"], False, video)

            # Convert 'track["duration"]' string to integer seconds for Pyrogram
            duration_sec = 0
            try:
                duration_sec = sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(track["duration"]).split(":"))))
            except:
                pass

            if not video:
                await message.message.reply_audio(
                    audio=output,
                    caption=TEXTS.SONG_CAPTION.format(
                        track["title"],
                        track["link"],
                        track["views"],
                        track["duration"],
                        message.from_user.mention,
                        hellbot.app.mention,
                    ),
                    duration=duration_sec,
                    performer=TEXTS.PERFORMER,
                    title=track["title"],
                    thumb=thumb,
                )
            else:
                await message.message.reply_video(
                    video=output,
                    caption=TEXTS.SONG_CAPTION.format(
                        track["title"],
                        track["link"],
                        track["views"],
                        track["duration"],
                        message.from_user.mention,
                        hellbot.app.mention,
                    ),
                    duration=duration_sec,
                    thumb=thumb,
                    supports_streaming=True,
                )

            chat = message.message.chat.title or message.message.chat.first_name
            await hellbot.logit(
                "Video" if video else "Audio",
                f"**⤷ User:** {message.from_user.mention} [`{message.from_user.id}`]\n**⤷ Chat:** {chat} [`{message.message.chat.id}`]\n**⤷ Link:** [{track['title']}]({track['link']})",
            )
            await hell.delete()
        except Exception as e:
            await hell.edit_text(f"**Error:**\n`{e}`")
        try:
            Config.SONG_CACHE.pop(rand_key)
            if thumb and os.path.exists(thumb): os.remove(thumb)
            if output and os.path.exists(output): os.remove(output)
        except Exception:
            pass

    def get_lyrics(self, song: str, artist: str) -> dict:
        context = {}
        if not self.client:
            return context
        results = self.client.search_song(song, artist)
        if results:
            results.to_dict()
            title = results["full_title"]
            image = results["song_art_image_url"]
            lyrics = results["lyrics"]
            context = {
                "title": title,
                "image": image,
                "lyrics": lyrics,
            }
        return context

ytube = YouTube()
