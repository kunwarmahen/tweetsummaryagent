# Container image for the Twitter Summary Agent (works with podman or docker).
#
# Auth note: the container does NOT log into X. Run `python main.py import-profile` on the
# HOST first (it decrypts your Chrome cookies into auth/storage_state.json), then mount the
# auth/ directory into the container — the scraper reuses that session, no keyring needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps + the Chromium browser with its system libraries.
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

# App source (data/, auth/, venv/ are excluded via .dockerignore).
COPY . .

EXPOSE 8000

# Serves the web UI and runs the in-process daily scheduler.
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
