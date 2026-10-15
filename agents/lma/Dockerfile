# The planner is built in its own stage and copied in, so the runtime image
# carries no OCaml toolchain. The Ethical Guard calls it for every decision and
# fails closed without it: no planner means every request is denied.
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

COPY agents/lma/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY agents ./agents

USER appuser

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c "python -c 'import os, urllib.request; urllib.request.urlopen(\"http://127.0.0.1:%s/health\" % os.environ[\"LMA_API_PORT\"])'"

CMD ["sh", "-c", "uvicorn agents.lma.main:app --host ${LMA_API_HOST} --port ${LMA_API_PORT}"]
