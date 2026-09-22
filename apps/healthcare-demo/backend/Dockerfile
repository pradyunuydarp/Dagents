# The planner is built first and copied in, so the runtime image carries no
# OCaml toolchain. The app needs the binary because the Ethical Guard fails
# closed without it: no planner means every request is denied.
FROM ocaml/opam:debian-ocaml-5.0 AS ocaml-builder
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
COPY apps/healthcare-demo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/healthcare-demo/backend/app ./app
COPY agents ./agents
COPY env ./env
COPY apps/healthcare-demo/env ./apps/healthcare-demo/env

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('HEALTHCARE_DEMO_API_PORT', '8080'))"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${HEALTHCARE_DEMO_API_HOST:-0.0.0.0} --port ${HEALTHCARE_DEMO_API_PORT:-8080}"]
