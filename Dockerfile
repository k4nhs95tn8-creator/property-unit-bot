FROM python:3.14-slim
WORKDIR /app
COPY matching.py bot.py ./
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
USER bot
ENV BOT_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "bot.py"]
