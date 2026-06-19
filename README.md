# yt-dlp API

A small FastAPI wrapper around [yt-dlp](https://github.com/yt-dlp/yt-dlp) for
queued, background video downloads — with optional [Sonarr](https://sonarr.tv)
/ [Radarr](https://radarr.video) integration so finished files land in the
right library and trigger a rescan. Also ships a custom **Olevod** extractor
plugin, a YouTube transcript endpoint, and an Olevod search endpoint.

Intended to run as an internal-only service (e.g. on a k3s cluster). It has **no
authentication** — restrict access at the network layer (ingress/`NetworkPolicy`).

## How it works

`POST /download` enqueues a job and returns immediately with a `job_id`. A fixed
pool of worker threads (`MAX_CONCURRENT_DOWNLOADS`) drains the queue; poll
`GET /status/{job_id}` for progress. Finished jobs are pruned from memory after
`JOB_RETENTION` seconds.

A job moves through: `queued → downloading → (retrying (n/m)) → completed | failed`.

If `sonarr_series_id` / `radarr_movie_id` is supplied (and the matching API key is
configured), the output path and naming come from the *arr API and a
`RescanSeries` / `RescanMovie` command fires on completion. Otherwise files go to
`DOWNLOAD_DIR/<name>/`.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/download` | Enqueue a download. Returns `{job_id, status: "queued"}`. |
| `GET` | `/status/{job_id}` | Full job record. `404` once a finished job is pruned. |
| `GET` | `/jobs` | Map of all known jobs (id → status/url/title). |
| `DELETE` | `/jobs/{job_id}` | Remove a finished job. `409` if still active, `404` if unknown. |
| `GET` | `/series` | Sonarr series tagged with `ARR_TAG` (with missing-episode counts). |
| `GET` | `/movies` | Radarr movies tagged with `ARR_TAG`. |
| `GET` | `/transcript?url=&lang=en` | YouTube transcript as text + timed segments. |
| `GET` | `/search/olevod?q=` | Search Olevod; returns detail URLs usable as `/download` input. |
| `GET` | `/health` | Liveness probe. |

Interactive docs are served at `/docs` (Swagger) and `/redoc`.

### `POST /download` body

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string (URL) | — | **Required.** Any yt-dlp-supported URL (incl. Olevod). |
| `show_name` | string | `null` | Folder name for generic (non-arr) downloads. |
| `season` | int | `1` | Season number for Sonarr naming (`S0xE0y`). |
| `sonarr_series_id` | int | `null` | Route into a Sonarr series' path + rescan. |
| `radarr_movie_id` | int | `null` | Route into a Radarr movie's path + rescan. |
| `format` | string | `bestvideo+bestaudio/best` | yt-dlp format selector. |
| `force` | bool | `false` | Overwrite existing files. |
| `episodes` | int[] | `null` | For playlists/series: only download these episode numbers. |

`sonarr_series_id` and `radarr_movie_id` are mutually exclusive — sending both
returns `400`.

### Examples

```bash
# Generic download into DOWNLOAD_DIR/MyClip/
curl -X POST localhost:8191/download \
  -H 'content-type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "show_name": "MyClip"}'

# Into a Sonarr series, season 2, episodes 3 and 4 only
curl -X POST localhost:8191/download \
  -H 'content-type: application/json' \
  -d '{"url": "https://www.olevod.com/detail/81328.html", "sonarr_series_id": 42, "season": 2, "episodes": [3, 4]}'

# Poll
curl localhost:8191/status/<job_id>
```

## Configuration

All via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `SONARR_URL` | `http://localhost:8989` | Sonarr base URL. |
| `SONARR_API_KEY` | `` | Required for `/series` and Sonarr downloads. |
| `RADARR_URL` | `http://localhost:7878` | Radarr base URL. |
| `RADARR_API_KEY` | `` | Required for `/movies` and Radarr downloads. |
| `ARR_TAG` | `tvb` | Tag used to filter `/series` and `/movies`. |
| `DOWNLOAD_DIR` | `/downloads/yt-dlp` | Base dir for generic (non-arr) downloads. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Worker pool size (concurrent downloads). |
| `JOB_RETENTION` | `86400` | Seconds to keep finished jobs in memory; `0` = forever. |
| `MAX_RETRIES` | `3` | Attempts per job on transient (network) errors. |
| `RETRY_DELAY` | `30` | Base backoff seconds between retries (scaled by attempt). |

> Container mount paths must match what Sonarr/Radarr report as their series/movie
> paths, or the *arr rescan won't find the files.

## Running

### Docker Compose

Set `SONARR_API_KEY` (and optionally `RADARR_API_KEY`, etc.) in a `.env` file
next to `docker-compose.yml`, adjust the volume mounts, then:

```bash
docker compose up -d --build
```

The service listens on `8191` inside the container (mapped to `30191` on the host
in the bundled compose file).

### Local

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8191
# ffmpeg must be on PATH for muxing/merging.
```

## Olevod plugin

`plugins/olevod.py` is a yt-dlp extractor plugin (with shared crypto helpers in
`plugins/olevod_common.py`). The Dockerfile copies `plugins/` into
`yt_dlp_plugins/extractor/` so yt-dlp auto-discovers it. It handles both legacy
(`index.php/vod/play/...`) and new (`player/vod/...`) Olevod URLs, plus
`olevod:series` playlist URLs.
```
