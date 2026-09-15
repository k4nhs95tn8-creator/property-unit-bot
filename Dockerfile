FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY matching.py bot.py release_data.py update_data.py data_errors.py ./
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
USER bot
ENV BOT_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "bot.py"]
