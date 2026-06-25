FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    APP_DEBUG=false \
    REQUEST_TIMEOUT=30 \
    DOUYIN_BROWSER_PATH=/usr/bin/chromium \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=600

WORKDIR /app

# Chromium is required to recover Douyin Live Photo data. Fonts are included
# so Chinese pages render correctly during browser verification.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        ca-certificates \
        fonts-noto-cjk \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app.py wsgi.py ./
COPY templates ./templates

RUN mkdir -p /app/.preview_cache \
    && chown -R nobody:nogroup /app

USER nobody

EXPOSE 8080

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${APP_PORT} --workers ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} --access-logfile - --error-logfile - wsgi:app"]
