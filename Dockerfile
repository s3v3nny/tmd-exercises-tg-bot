FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app bot \
    && mkdir /data \
    && chown bot:bot /data

WORKDIR /app
COPY bot.py .

USER bot
CMD ["python", "bot.py"]
