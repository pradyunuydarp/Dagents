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

# Which port to bind, in order of precedence:
#
#   HEALTHCARE_DEMO_API_PORT  this repository's own convention, an explicit choice
#   PORT                      what a platform host injects and then routes to
#   8080                      the documented local default
#
# `PORT` is in the middle on purpose. A host that assigns a port and expects the
# service to bind it gets no response at all when the service binds something
# else: the edge accepts the connection, waits for an origin that is listening
# elsewhere, and the caller sees a timeout with zero bytes — which reads like
# the service being down rather than like a misconfigured port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; port = os.environ.get('HEALTHCARE_DEMO_API_PORT') or os.environ.get('PORT') or '8080'; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % port)"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${HEALTHCARE_DEMO_API_HOST:-0.0.0.0} --port ${HEALTHCARE_DEMO_API_PORT:-${PORT:-8080}}"]
