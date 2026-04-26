FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY services/model-service/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

COPY services/model-service/app ./app
COPY agents ./agents
COPY services/model-service/artifacts ./artifacts
COPY services/model-service/data ./data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c "python -c 'import os, urllib.request; urllib.request.urlopen(\"http://127.0.0.1:%s/api/v1/health\" % os.environ[\"API_PORT\"])'"

CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST} --port ${API_PORT}"]
