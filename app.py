import os
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

import yt_dlp

app = FastAPI(title="yt-dlp API", version="0.1.0")

jobs: dict[str, dict] = {}

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
SONARR_TAG = os.environ.get("SONARR_TAG", "tvb")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "30"))


def _sonarr_get(path: str, params: dict | None = None):
    resp = httpx.get(
        f"{SONARR_URL}/api/v3/{path}",
        params=params,
        headers={"X-Api-Key": SONARR_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _sonarr_post(path: str, body: dict):
    resp = httpx.post(
        f"{SONARR_URL}/api/v3/{path}",
        json=body,
        headers={"X-Api-Key": SONARR_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _get_tvb_tag_id() -> int | None:
    for tag in _sonarr_get("tag"):
        if tag["label"] == SONARR_TAG:
            return tag["id"]
    return None


class DownloadRequest(BaseModel):
    url: HttpUrl
    show_name: str | None = None
    season: int = 1
    sonarr_series_id: int | None = None
    format: str = "bestvideo+bestaudio/best"


class JobResponse(BaseModel):
    job_id: str
    status: str


def _build_outtmpl(series_path: str, series_title: str, season: int) -> str:
    season_dir = Path(series_path) / f"Season {season:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    return str(season_dir / f"{series_title} - S{season:02d}E%(episode_number)02d.%(ext)s")


def _run_download(job_id: str, url: str, show_name: str | None, season: int,
                  sonarr_series_id: int | None, fmt: str):
    job = jobs[job_id]

    series_path = None
    series_title = show_name or "Unknown"

    if sonarr_series_id and SONARR_API_KEY:
        try:
            series = _sonarr_get(f"series/{sonarr_series_id}")
            series_path = series["path"]
            series_title = series["title"]
            job["sonarr_series"] = series_title
        except Exception as e:
            job["sonarr"] = {"error": f"Failed to get series: {e}"}

    if not series_path:
        series_path = f"/tv/{series_title}"

    outtmpl = _build_outtmpl(series_path, series_title, season)

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

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            job["attempt"] = attempt
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                job["title"] = info.get("title") or info.get("series") or series_title
                job["total_entries"] = info.get("n_entries")
                job["download_path"] = series_path

            if sonarr_series_id and SONARR_API_KEY:
                try:
                    job["sonarr"] = _sonarr_post("command", {
                        "name": "RescanSeries",
                        "seriesId": sonarr_series_id,
                    })
                except Exception as e:
                    job["sonarr"] = {"error": str(e)}

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
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "downloading",
        "url": str(req.url),
        "show_name": req.show_name,
        "season": req.season,
        "sonarr_series_id": req.sonarr_series_id,
        "sonarr_series": None,
        "progress": "0%",
        "filename": "",
        "downloaded_files": [],
        "title": None,
        "total_entries": None,
        "download_path": None,
        "sonarr": None,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    thread = threading.Thread(
        target=_run_download,
        args=(job_id, str(req.url), req.show_name, req.season,
              req.sonarr_series_id, req.format),
        daemon=True,
    )
    thread.start()
    return JobResponse(job_id=job_id, status="downloading")


@app.get("/series")
def list_tvb_series():
    """List all Sonarr series tagged with the tvb tag."""
    if not SONARR_API_KEY:
        raise HTTPException(status_code=500, detail="SONARR_API_KEY not configured")
    tag_id = _get_tvb_tag_id()
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
        for jid, j in jobs.items()
    }


@app.get("/health")
def health():
    return {"status": "ok"}
