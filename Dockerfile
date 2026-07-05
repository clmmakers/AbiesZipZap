FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN python3 -m playwright install-deps chromium

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN python3 -m playwright install chromium \
    && mkdir -p /app/state /app/v2/data /app/v2/logs /app/v2/downloads /opt/playwright-browsers \
    && chown -R appuser:appuser /app /opt/playwright-browsers

COPY . /app/v2
RUN chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1
ENV HEADLESS=true
ENV BROWSER_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"

USER appuser
CMD ["sleep", "infinity"]
