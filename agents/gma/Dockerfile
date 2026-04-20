FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY agents/gma/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY agents ./agents

USER appuser

EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c "python -c 'import os, urllib.request; urllib.request.urlopen(\"http://127.0.0.1:%s/health\" % os.environ[\"GMA_API_PORT\"])'"

CMD ["sh", "-c", "uvicorn agents.gma.main:app --host ${GMA_API_HOST} --port ${GMA_API_PORT}"]
