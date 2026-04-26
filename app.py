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
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads/yt-dlp")


class DownloadRequest(BaseModel):
    url: HttpUrl
    show_name: str | None = None
    season: int = 1
    format: str = "bestvideo+bestaudio/best"
    notify_sonarr: bool = True


class JobResponse(BaseModel):
    job_id: str
    status: str


def _build_outtmpl(show_dir: Path, show_name: str | None, season: int) -> str:
    name = show_name or "%(series,title)s"
    return str(show_dir / f"{name} - S{season:02d}E%(episode_number)02d.%(ext)s")


def _notify_sonarr(scan_path: str):
    if not SONARR_API_KEY:
        return {"skipped": "no SONARR_API_KEY configured"}
    resp = httpx.post(
        f"{SONARR_URL}/api/v3/command",
        json={"name": "DownloadedEpisodesScan", "path": scan_path},
        headers={"X-Api-Key": SONARR_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _run_download(job_id: str, url: str, show_name: str | None, season: int, fmt: str, notify: bool):
    job = jobs[job_id]

    show_dir = Path(DOWNLOAD_DIR) / (show_name or "unknown")
    show_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        if d["status"] == "downloading":
            job["progress"] = d.get("_percent_str", "").strip()
            job["filename"] = d.get("filename", "")
        elif d["status"] == "finished":
            job["downloaded_files"].append(d.get("filename", ""))

    opts = {
        "format": fmt,
        "outtmpl": _build_outtmpl(show_dir, show_name, season),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            resolved_name = show_name or info.get("series") or info.get("title") or "Unknown"
            job["title"] = resolved_name

            if not show_name and resolved_name != "unknown":
                final_dir = Path(DOWNLOAD_DIR) / resolved_name
                if show_dir != final_dir:
                    show_dir.rename(final_dir)
                    show_dir = final_dir

            job["total_entries"] = info.get("n_entries")
            job["download_path"] = str(show_dir)

        if notify:
            try:
                job["sonarr"] = _notify_sonarr(str(show_dir))
            except Exception as e:
                job["sonarr"] = {"error": str(e)}

        job["status"] = "completed"
        job["finished_at"] = time.time()
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["finished_at"] = time.time()


@app.post("/download", response_model=JobResponse)
def start_download(req: DownloadRequest):
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "status": "downloading",
        "url": str(req.url),
        "show_name": req.show_name,
        "season": req.season,
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
        args=(job_id, str(req.url), req.show_name, req.season, req.format, req.notify_sonarr),
        daemon=True,
    )
    thread.start()
    return JobResponse(job_id=job_id, status="downloading")


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
