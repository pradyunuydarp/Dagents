# Local Monitoring Agent (LMA)

The LMA runs next to one data source. Records are read, profiled, governed and
used for local model work inside it, and only what the source's own Guard
permits leaves it. The framework's architecture page,
[`docs/agents/lma-gma-architecture.md`](../../docs/agents/lma-gma-architecture.md),
shows where the LMA fits, and every endpoint is in the
[API reference](../../docs/reference/api/agents.md#the-local-monitoring-agent).

## Two ways to start it

| | Generic LMA | LMA for one site |
|---|---|---|
| Started with | No `LMA_SITE_ID` | `LMA_SITE_ID` and `LMA_CONDITION_ID`, or `LmaDeps(site=SiteConfig(...))` in code |
| Records | Sent in each request, or read from a registered source | Read from the site's own source, as the site's database role |
| Guard | Plans against the classification in each request | Plans against the condition pack's classification, and refuses (`409`) a request whose inline classification differs |
| Routes | Health, profiling, model runs, sources, bundles and runs, the Guard and the audit log | All of those, plus the site and federated job routes below |

A generic LMA answers `404` on the site and job routes. It behaves as the LMA
did before sites existed.

## Run it

From the `dagents` package on PyPI, whose wheels for Linux x86_64, Linux
aarch64 and macOS arm64 include the planner:

```bash
pip install "dagents[lma]"
dagents-lma
```

From a clone of the repository, with the planner built:

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
PYTHONPATH=. .venv/bin/uvicorn dagents.lma.main:app --port 8010
```

`PYTHONPATH=. .venv/bin/python -m dagents.lma` does the same as `dagents-lma`.
Both listen on `--host` and `--port`, else `LMA_API_HOST` and `LMA_API_PORT`,
else `127.0.0.1:8010`. The guide's
[Install the package](../../docs/guide/00-install.md) page covers the package,
its extras and platforms without a planner.

`dagents/lma/main.py` builds the app with `create_app(Settings.from_env())`.
Nothing reads the environment when a module is imported, so one process can
build several LMAs, one per site:

```python
from dagents.lma.app_factory import LmaDeps, create_app
from dagents.lma.config import Settings
from dagents.lma.domain.site_config import SiteConfig

app = create_app(
    Settings(site_token="...", coordinator_token="...", gma_token="..."),
    LmaDeps(registry=registry, site=SiteConfig(site_id="site-a", condition_id="my_condition"),
            source=my_source, network=network),
)
```

`create_app` sets everything up at once, because an app called in process
through `httpx.ASGITransport` gets no lifespan events. Objects passed in
`LmaDeps` (the registry, the site, the source, the audit sink, the job log and
the agent network) belong to the caller; objects the LMA builds from its
settings are closed when it shuts down under uvicorn.
`examples/getting-started/shop/agents.py` builds two LMAs and a GMA this way.

The Docker image (`dagents/lma/Dockerfile`) builds `dagentsc` in a first stage
and copies it onto `PATH`. Without the planner the Guard denies every request
and every federated job is refused, and the LMA logs a warning when it is
built.

## Settings

`Settings.from_env()` loads `env/.env.shared` and `env/.env.lma` without
replacing variables already set, then reads the variables below. It loads the
files only from a source tree: a checkout, or an image that copies `dagents/`
and `env/` side by side. An installed `dagents` package reads only the process
environment.

| Variable | Default | Meaning |
|---|---|---|
| `LMA_APP_NAME`, `LMA_APP_ENV`, `LMA_LOG_LEVEL` | `dagents-lma`, `development`, `info` | Names and logging |
| `LMA_API_HOST`, `LMA_API_PORT` | none | Where `dagents-lma` listens, and where uvicorn serves the LMA in the container; the app itself does not read them |
| `LMA_SITE_ID`, `LMA_CONDITION_ID` | none | The site the LMA serves and its condition pack. Without `LMA_SITE_ID` the LMA is generic |
| `LMA_STUDY_ID`, `LMA_REPORTING_PURPOSE` | the condition id, the pack's first approved purpose | Who asks for the site's summary and why; the label-rate decision uses them |
| `LMA_PROFILE_FIELDS` | every field of the contract | Comma-separated fields the running profile tracks |
| `LMA_CAPABILITIES`, `LMA_POLICY_VERSION` | none, `site-policy-v1` | What the site's registration says |
| `LMA_EXTENSIONS` | none | `module:attribute,...`: extensions loaded into this LMA's own registry. Without it the LMA reads the default registry |
| `LMA_SITE_SOURCE` | none | The name of a site source factory an extension contributes (`site_sources()`). The LMA reads the site's records through it |
| `LMA_DATABASE_URL`, `LMA_SITE_ROLE` | none | The site's database and the role every operation acts as. Never shown in a log or a response |
| `LMA_AUDIT_TABLE`, `LMA_AUDIT_ANCHOR_TABLE` | none | Where the audit chain is kept in that database; in memory without them |
| `LMA_JOB_LOG_TABLE` | none | Where the job log is kept in that database; in memory without it |
| `LMA_GMA_ENDPOINT`, `LMA_GMA_TOKEN` | none | The GMA's address and the GMA `site` token bound to this site. The LMA reads the released model there and sends training updates there |
| `LMA_SITE_TOKEN`, `LMA_COORDINATOR_TOKEN` | none | The tokens this LMA accepts, for the `site` and `coordinator` scopes |
| `LMA_REQUIRE_TOKENS` | `0` | `1` refuses to start without a token |
| `LMA_SOURCE_MAX_STALENESS_MS` | `30000` | How long the LMA keeps the records it read before it asks the source for its version again |
| `LMA_STATEMENT_TIMEOUT_MS` | `15000` | The statement timeout of each database operation |

`env/.env.lma` sets only the names, the port and the GMA address. Set the rest
in the environment; never commit a token or a database address.

## Routes

Each route has a short path and an `/api/v1` path that run the same handler.
The site and job routes, in their `/api/v1` form:

| Route | Token scope | What it does |
|---|---|---|
| `GET /site` | `site`, `coordinator` | Describe the site: condition, classification, label field, record count, and the source by kind and location, never a host |
| `GET /site/summary` | `site`, `coordinator` | Counts, and the label rate only when the site's planner permits it (`label_rate_status`) |
| `GET /site/profile` | `site` | The running profile, for the site's own staff |
| `POST /site/source:refresh` | `site` | Ask the source for its version now; call it after a write |
| `GET /site/model`, `POST /site/model:evaluate` | `site` | The released model, read from the GMA and checked by its model kind; its metrics on the site's labelled records |
| `POST /governance/restrictions:probe` | `site` | What the Guard would release over the site's first records, never the rows |
| `GET /governance/audit:summary` | `site`, `coordinator` | Whether the audit chain verifies, and its length |
| `GET /federation/registration` | `coordinator` | How the site enrols, with its current record count |
| `POST /federation/jobs:offer` | `coordinator` | Check a job before reading any data; accept or refuse it with the reason |
| `POST /federation/jobs:execute` | `coordinator` | Run an accepted job once, on the Guard's payload |
| `POST /federation/jobs:run` | `coordinator` | Both steps in one call: check the job and, when the site accepts it, run it. The GMA's HTTP engine sends this one |
| `GET /federation/jobs?limit=20` | `site` | The job log, newest first |

With tokens configured, every other route except `/health` needs the `site`
token. Without tokens the LMA answers every caller and logs a warning.

## What a site's LMA refuses

- A request that embeds a source definition in `dataset` (`403`). Inline
  records and a registered `source_id` still work.
- A source registration that is not `inline`, or that sets a connection option
  other than `rows` or `documents`: databases, roles, credentials and SQL come
  from configuration only (`403`).
- A Guard request whose inline classification differs from the registered one
  (`409`).

On every LMA, the source endpoints show each connection option's value as
`[redacted]`, except `role`, `sslmode` and `connect_timeout`.

## Errors

The LMA answers its own errors as `{"detail": ..., "code": ...}`: `401`
`unauthorized`, `403` `forbidden`, `404` `not_found` (no site, or no released
model), `409` `conflict`, `502` `bad_gateway` (the GMA answered with an error),
`503` `planner_unavailable`, `store_unavailable`, `store_not_set_up` or
`agent_unavailable` (the GMA did not answer). No `detail` holds a host, a
token or a connection string.

## Layout

- `domain/`: pure models, including `SiteConfig`
- `application/`: `site_service.py` (summary, profile, probe, model in use),
  `federation_jobs.py` (offer and execute), `monitoring_service.py` (profiling,
  model runs, sources)
- `adapters/`: `gma_link.py` (calls to the GMA), `http_update_channel.py`
  (training updates to the GMA's update store)
- `infrastructure/`: `job_log.py` (`InMemoryJobLog`, `PostgresJobLog`), and
  in-memory bundles, runs and telemetry
- `app_factory.py`, `routes.py`, `config.py`, `di.py`, `main.py`: wiring
