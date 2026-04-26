import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

import yt_dlp

app = FastAPI(title="yt-dlp API", version="0.1.0")

jobs: dict[str, dict] = {}


class DownloadRequest(BaseModel):
    url: HttpUrl
    output_dir: str = "/media/tvb"
    show_name: str | None = None
    season: int = 1
    format: str = "bestvideo+bestaudio/best"


class JobResponse(BaseModel):
    job_id: str
    status: str


def _build_outtmpl(output_dir: str, show_name: str | None, season: int) -> str:
    if show_name:
        return str(Path(output_dir) / show_name / f"Season {season:02d}" / f"{show_name} - S{season:02d}E%(episode_number)02d.%(ext)s")
    return str(Path(output_dir) / "%(series,title)s" / f"Season {season:02d}" / f"%(series,title)s - S{season:02d}E%(episode_number)02d.%(ext)s")


def _run_download(job_id: str, url: str, output_dir: str, show_name: str | None, season: int, fmt: str):
    job = jobs[job_id]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        if d["status"] == "downloading":
            job["progress"] = d.get("_percent_str", "").strip()
            job["filename"] = d.get("filename", "")
        elif d["status"] == "finished":
            job["downloaded_files"].append(d.get("filename", ""))

    opts = {
        "format": fmt,
        "outtmpl": _build_outtmpl(output_dir, show_name, season),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            job["title"] = info.get("title") or info.get("series") or "Unknown"
            job["total_entries"] = info.get("n_entries")
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
        "output_dir": req.output_dir,
        "show_name": req.show_name,
        "season": req.season,
        "progress": "0%",
        "filename": "",
        "downloaded_files": [],
        "title": None,
        "total_entries": None,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    thread = threading.Thread(
        target=_run_download,
        args=(job_id, str(req.url), req.output_dir, req.show_name, req.season, req.format),
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
