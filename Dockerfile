FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/until_bot.sqlite3
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY until_bot ./until_bot
VOLUME /data
CMD ["python", "-m", "until_bot"]
