FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
ENV PORT=8000 DB_PATH=/data/monitor.db
VOLUME /data
CMD gunicorn app.main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180
