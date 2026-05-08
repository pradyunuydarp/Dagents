FROM ocaml/opam:debian-ocaml-5.0 as ocaml-builder
USER root
RUN apt-get update && apt-get install -y m4 && apt-get clean
USER opam
WORKDIR /home/opam/src
COPY --chown=opam:opam bindings/ocaml ./bindings/ocaml
WORKDIR /home/opam/src/bindings/ocaml
RUN opam install -y dune yojson
RUN opam exec -- dune build ./bin/dagentsc.exe

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=ocaml-builder /home/opam/src/bindings/ocaml/_build/default/bin/dagentsc.exe /usr/local/bin/dagentsc
COPY services/nl2sql-demo/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/nl2sql-demo/backend/app ./app
COPY agents ./agents
COPY env ./env

USER appuser

EXPOSE 8070

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('NL2SQL_API_PORT', '8070'))"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${NL2SQL_API_HOST:-0.0.0.0} --port ${NL2SQL_API_PORT:-8070}"]
