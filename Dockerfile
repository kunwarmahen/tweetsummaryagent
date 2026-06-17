# Container image for the Twitter Summary Agent (works with podman or docker).
#
# Auth note: the container does NOT log into X. Run `python main.py import-profile` on the
# HOST first (it decrypts your Chrome cookies into auth/storage_state.json), then mount the
# auth/ directory into the container — the scraper reuses that session, no keyring needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765

WORKDIR /app

# Install Python deps + the Chromium browser with its system libraries.
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

# App source (data/, auth/, venv/ are excluded via .dockerignore).
COPY . .

# Default UI port (override with -e APP_PORT=...); 8765 avoids the common 8000 clash.
EXPOSE 8765

# Serves the web UI and runs the in-process scheduler. Host/port come from APP_HOST/APP_PORT.
CMD ["python", "main.py", "serve"]
