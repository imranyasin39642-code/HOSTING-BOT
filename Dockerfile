# ═══════════════════════════════════════════════════════════════════════
#  GameOver Hosting Bot — Production Dockerfile
#  Target: Hugging Face Spaces (Docker SDK)
#  Port 7860 is exposed for the built-in aiohttp health-check server
# ═══════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# ── System labels ────────────────────────────────────────────────────
LABEL maintainer="GameOver Hosting"
LABEL description="Telegram VPS Hosting Manager — Hugging Face Spaces Edition"

# ── Environment tweaks ───────────────────────────────────────────────
# • PYTHONUNBUFFERED  : flush logs immediately (no buffering)
# • PYTHONDONTWRITEBYTECODE : skip .pyc files to save space
# • PIP_NO_CACHE_DIR  : slim the image by not caching pip downloads
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# ── System-level dependencies ────────────────────────────────────────
#   ffmpeg      — audio/video processing for multimedia bots
#   git         — some pip packages install from git
#   curl, wget  — general download utilities
#   libmagic1   — file-type detection (python-magic)
#   build-essential — needed to compile C-extension wheels
#   libssl-dev  — TLS support for crypto libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
        wget \
        libmagic1 \
        build-essential \
        libssl-dev \
        libffi-dev \
        libjpeg-dev \
        libpng-dev \
        libwebp-dev \
        libopus-dev \
        libvpx-dev \
        libsm6 \
        libxext6 \
        libgl1 \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────
# Copy requirements first so Docker can cache this layer
COPY requirements.txt .

# Install core hosting-bot requirements only
# (individual hosted bots install their own deps at runtime)
RUN pip install --upgrade pip setuptools wheel && \
    pip install \
        python-telegram-bot==21.3 \
        psutil==5.9.8 \
        python-dotenv==1.0.1 \
        httpx==0.28.1 \
        aiohttp==3.14.1 \
        requests==2.34.2 \
        pillow==12.2.0 \
        uvloop \
        aiofiles

# ── Application source ───────────────────────────────────────────────
COPY . .

# ── Persistent volumes (data that must survive restarts) ─────────────
# Hugging Face Spaces mounts /data as a persistent volume.
# We symlink the critical directories into /data so data survives
# container restarts (HF Spaces resets the container FS on each deploy).
RUN mkdir -p /data/database /data/bots /data/logs && \
    rm -rf /app/database /app/bots /app/logs && \
    ln -s /data/database /app/database && \
    ln -s /data/bots /app/bots && \
    ln -s /data/logs /app/logs

# ── Expose port ──────────────────────────────────────────────────────
# Hugging Face Spaces REQUIRES a service listening on port 7860.
# The built-in aiohttp health-check server handles this.
EXPOSE 7860

# ── Entrypoint ───────────────────────────────────────────────────────
CMD ["python", "hosting.py"]
