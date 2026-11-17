FROM python:3.12-slim

WORKDIR /app

# System deps: curl for healthcheck, ffmpeg for camera frame capture in tools/vision.py
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Pin pip and certifi before anything else (Black October 2026 CVE-2026-115557)
RUN pip install --no-cache-dir pip==26.2.2 certifi==2026.10.19

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
