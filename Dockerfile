FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:${EVB_STATUS_PORT:-8099}/status >/dev/null
CMD ["echo-voice-bridge"]
