FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plugins/ /app/yt_dlp_plugins/extractor/
COPY app.py .

EXPOSE 8191
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8191"]
