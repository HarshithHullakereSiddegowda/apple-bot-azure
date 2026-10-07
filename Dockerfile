FROM python:3.12-slim

# Links the GHCR package to the GitHub repo.
LABEL org.opencontainers.image.source="https://github.com/HarshithHullakereSiddegowda/apple-bot-azure"

# Flush logs immediately so every JSON log line reaches Azure Log Analytics.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Only the runtime code. No .env, no data, no evals (see .dockerignore).
COPY config.py retrieval.py ingest.py ratelimit.py app.py ./
COPY static/ ./static/

# Don't run as root inside the container.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
