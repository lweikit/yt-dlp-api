import os
import queue
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# Shared with the yt-dlp extractor plugin. In the image the plugins dir is copied
# to yt_dlp_plugins/extractor/; locally it lives at plugins/.
try:
    from yt_dlp_plugins.extractor.olevod_common import decrypt_api_data, make_vv
except ImportError:
    from plugins.olevod_common import decrypt_api_data, make_vv

app = FastAPI(title="yt-dlp API", version="0.3.0")

jobs: dict[str, dict] = {}

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
ARR_TAG = os.environ.get("ARR_TAG", "tvb")
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads/yt-dlp")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "30"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
# Finished jobs older than this (seconds) are pruned from memory; 0 disables.
JOB_RETENTION = int(os.environ.get("JOB_RETENTION", "86400"))

job_queue: "queue.Queue[str]" = queue.Queue()


def _prune_jobs():
    if JOB_RETENTION <= 0:
        return
    cutoff = time.time() - JOB_RETENTION
    for jid, v in list(jobs.items()):
        if v.get("finished_at") and v["finished_at"] < cutoff:
            jobs.pop(jid, None)


def _worker():
    while True:
        job_id = job_queue.get()
        try:
            job = jobs.get(job_id)
            if job:
                _run_download(
                    job_id, job["url"], job["show_name"], job["season"],
                    job["sonarr_series_id"], job["radarr_movie_id"],
                    job["format"], job["force"], job["episodes"],
                )
        except Exception as e:  # safety net: never let a worker thread die
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["finished_at"] = time.time()
        finally:
            _prune_jobs()
            job_queue.task_done()


for _ in range(MAX_CONCURRENT_DOWNLOADS):
    threading.Thread(target=_worker, daemon=True).start()


def _arr_get(base_url: str, api_key: str, path: str, params: dict | None = None):
    resp = httpx.get(
        f"{base_url}/api/v3/{path}",
        params=params,
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _arr_post(base_url: str, api_key: str, path: str, body: dict):
    resp = httpx.post(
        f"{base_url}/api/v3/{path}",
        json=body,
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _sonarr_get(path: str, params: dict | None = None):
    return _arr_get(SONARR_URL, SONARR_API_KEY, path, params)


def _sonarr_post(path: str, body: dict):
    return _arr_post(SONARR_URL, SONARR_API_KEY, path, body)


def _radarr_get(path: str, params: dict | None = None):
    return _arr_get(RADARR_URL, RADARR_API_KEY, path, params)


def _radarr_post(path: str, body: dict):
    return _arr_post(RADARR_URL, RADARR_API_KEY, path, body)


def _get_tag_id(get_fn) -> int | None:
    for tag in get_fn("tag"):
        if tag["label"] == ARR_TAG:
            return tag["id"]
    return None


class DownloadRequest(BaseModel):
    url: HttpUrl
    show_name: str | None = None
    season: int = 1
    sonarr_series_id: int | None = None
    radarr_movie_id: int | None = None
    format: str = "bestvideo+bestaudio/best"
    force: bool = False
    episodes: list[int] | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str


def _build_series_outtmpl(series_path: str, series_title: str, season: int) -> str:
    season_dir = Path(series_path) / f"Season {season:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    return str(season_dir / f"{series_title} - S{season:02d}E%(episode_number)02d.%(ext)s")


def _build_movie_outtmpl(movie_path: str) -> str:
    movie_dir = Path(movie_path)
    movie_dir.mkdir(parents=True, exist_ok=True)
    return str(movie_dir / "%(title)s.%(ext)s")


def _build_generic_outtmpl(name: str) -> str:
    dl_dir = Path(DOWNLOAD_DIR) / name
    dl_dir.mkdir(parents=True, exist_ok=True)
    return str(dl_dir / "%(title)s.%(ext)s")


def _run_download(job_id: str, url: str, show_name: str | None, season: int,
                  sonarr_series_id: int | None, radarr_movie_id: int | None,
                  fmt: str, force: bool = False, episodes: list[int] | None = None):
    job = jobs[job_id]
    job["status"] = "downloading"
    job["started_at"] = time.time()

    outtmpl = None
    download_path = None
    media_title = show_name or "Unknown"

    if sonarr_series_id and SONARR_API_KEY:
        try:
            series = _sonarr_get(f"series/{sonarr_series_id}")
            series_path = series["path"]
            media_title = series["title"]
            job["sonarr_series"] = media_title
            outtmpl = _build_series_outtmpl(series_path, media_title, season)
            download_path = series_path
        except Exception as e:
            job["arr_result"] = {"error": f"Failed to get series: {e}"}

    elif radarr_movie_id and RADARR_API_KEY:
        try:
            movie = _radarr_get(f"movie/{radarr_movie_id}")
            movie_path = movie["path"]
            media_title = movie["title"]
            job["radarr_movie"] = media_title
            outtmpl = _build_movie_outtmpl(movie_path)
            download_path = movie_path
        except Exception as e:
            job["arr_result"] = {"error": f"Failed to get movie: {e}"}

    if not outtmpl:
        outtmpl = _build_generic_outtmpl(media_title)
        download_path = str(Path(DOWNLOAD_DIR) / media_title)

    def progress_hook(d):
        if d["status"] == "downloading":
            job["progress"] = d.get("_percent_str", "").strip()
            job["filename"] = d.get("filename", "")
        elif d["status"] == "finished":
            job["downloaded_files"].append(d.get("filename", ""))

    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }
    if force:
        opts["overwrites"] = True
    if episodes:
        ep_set = set(episodes)
        opts["match_filter"] = lambda info, *_: None if info.get("episode_number") in ep_set else "episode filtered out"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            job["attempt"] = attempt
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                job["title"] = info.get("title") or info.get("series") or media_title
                job["total_entries"] = info.get("n_entries")
                job["download_path"] = download_path

            if sonarr_series_id and SONARR_API_KEY:
                try:
                    job["arr_result"] = _sonarr_post("command", {
                        "name": "RescanSeries",
                        "seriesId": sonarr_series_id,
                    })
                except Exception as e:
                    job["arr_result"] = {"error": str(e)}
            elif radarr_movie_id and RADARR_API_KEY:
                try:
                    job["arr_result"] = _radarr_post("command", {
                        "name": "RescanMovie",
                        "movieId": radarr_movie_id,
                    })
                except Exception as e:
                    job["arr_result"] = {"error": str(e)}

            job["status"] = "completed"
            job["finished_at"] = time.time()
            return
        except Exception as e:
            last_error = e
            is_transient = any(s in str(e).lower() for s in [
                "name resolution", "connection", "timeout", "temporary failure",
                "network", "reset by peer", "broken pipe",
            ])
            if is_transient and attempt < MAX_RETRIES:
                job["status"] = f"retrying ({attempt}/{MAX_RETRIES})"
                time.sleep(RETRY_DELAY * attempt)
                continue
            break

    job["status"] = "failed"
    job["error"] = str(last_error)
    job["finished_at"] = time.time()


@app.post("/download", response_model=JobResponse)
def start_download(req: DownloadRequest):
    if req.sonarr_series_id and req.radarr_movie_id:
        raise HTTPException(
            status_code=400,
            detail="Provide only one of sonarr_series_id or radarr_movie_id",
        )
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "queued",
        "attempt": 0,
        "url": str(req.url),
        "show_name": req.show_name,
        "season": req.season,
        "sonarr_series_id": req.sonarr_series_id,
        "radarr_movie_id": req.radarr_movie_id,
        "format": req.format,
        "force": req.force,
        "episodes": req.episodes,
        "sonarr_series": None,
        "radarr_movie": None,
        "progress": "0%",
        "filename": "",
        "downloaded_files": [],
        "title": None,
        "total_entries": None,
        "download_path": None,
        "arr_result": None,
        "error": None,
        "queued_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    job_queue.put(job_id)
    return JobResponse(job_id=job_id, status="queued")


@app.get("/series")
def list_tagged_series():
    """List all Sonarr series tagged with ARR_TAG."""
    if not SONARR_API_KEY:
        raise HTTPException(status_code=500, detail="SONARR_API_KEY not configured")
    tag_id = _get_tag_id(_sonarr_get)
    if tag_id is None:
        return []
    all_series = _sonarr_get("series")
    return [
        {
            "id": s["id"],
            "title": s["title"],
            "path": s["path"],
            "seasons": len(s["seasons"]),
            "episodeCount": s.get("episodeCount", 0),
            "episodeFileCount": s.get("episodeFileCount", 0),
            "missing": s.get("episodeCount", 0) - s.get("episodeFileCount", 0),
        }
        for s in all_series if tag_id in s.get("tags", [])
    ]


@app.get("/movies")
def list_tagged_movies():
    """List all Radarr movies tagged with ARR_TAG."""
    if not RADARR_API_KEY:
        raise HTTPException(status_code=500, detail="RADARR_API_KEY not configured")
    tag_id = _get_tag_id(_radarr_get)
    if tag_id is None:
        return []
    all_movies = _radarr_get("movie")
    return [
        {
            "id": m["id"],
            "title": m["title"],
            "year": m.get("year"),
            "path": m["path"],
            "hasFile": m.get("hasFile", False),
        }
        for m in all_movies if tag_id in m.get("tags", [])
    ]


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs")
def list_jobs():
    return {
        jid: {"status": j["status"], "url": j["url"], "title": j["title"]}
        for jid, j in list(jobs.items())
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Remove a job from memory. Only finished (completed/failed) jobs can be deleted."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in ("queued", "downloading") or job["status"].startswith("retrying"):
        raise HTTPException(status_code=409, detail="Job is still active")
    jobs.pop(job_id, None)
    return {"deleted": job_id}


@app.get("/transcript")
def get_transcript(url: str, lang: str = "en"):
    """Fetch YouTube video transcript as plain text or timed segments."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    if not video_id:
        path = parsed.path.lstrip("/")
        if path:
            video_id = path.split("/")[-1]
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")

    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=[lang])
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    segments = [
        {"text": s.text, "start": s.start, "duration": s.duration}
        for s in transcript
    ]
    text = " ".join(s.text for s in transcript)

    return {
        "video_id": video_id,
        "language": lang,
        "text": text,
        "segments": segments,
    }


OLEVOD_API = "https://api.olelive.com"
OLEVOD_SITE = "https://www.olevod.com"


@app.get("/search/olevod")
def search_olevod(q: str):
    headers = {"Origin": OLEVOD_SITE, "Referer": f"{OLEVOD_SITE}/"}
    resp = httpx.get(
        f"{OLEVOD_API}/v1/pub/index/search/{q}/0/0/0/1",
        params={"_vv": make_vv()},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        return []
    data = body.get("data")
    if isinstance(data, str):
        data = decrypt_api_data(data)
    if not data:
        return []
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for group in data.get("data", []):
            if isinstance(group, dict):
                for entry in (group.get("list") or []):
                    if isinstance(entry, dict):
                        items.append(entry)
        if not items:
            items = data.get("records", [])
    results = []
    for item in items:
        vid = item.get("id")
        if not vid:
            continue
        results.append({
            "id": vid,
            "name": item.get("name"),
            "type": item.get("typeId1Name"),
            "year": item.get("year"),
            "episodes": item.get("remarks") or item.get("episodesTxt"),
            "url": f"{OLEVOD_SITE}/detail/{vid}.html",
        })
    return results


@app.get("/health")
def health():
    return {"status": "ok"}
