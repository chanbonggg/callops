FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY migrations/ migrations/
COPY scripts/manage.py scripts/manage.py
COPY deploy/server/serve.py deploy/server/serve.py
USER 1000:1000
