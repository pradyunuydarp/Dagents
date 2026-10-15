# LMA Changelog

## Unreleased

- The LMA can serve one site. With `LMA_SITE_ID` and `LMA_CONDITION_ID` it reads
  the site's records through a site source that an extension contributes, plans
  against the condition pack's classification in strict mode, and serves the
  site routes (`/site`, `/site/summary`, `/site/profile`, `/site/source:refresh`,
  `/site/model`, `/site/model:evaluate`, `/governance/restrictions:probe`,
  `/governance/audit:summary`) and the federated job routes
  (`/federation/registration`, `/federation/jobs:offer`,
  `/federation/jobs:execute`, `/federation/jobs`), each with a short and an
  `/api/v1` path.
- A training job's clipped update goes to the GMA's update store over HTTP
  (`adapters/http_update_channel.py`). The job log records each refused offer and
  each result, in memory or in a Postgres table (`infrastructure/job_log.py`).
- Routes take bearer tokens of the `site` and `coordinator` scopes when tokens
  are configured. Without tokens the LMA runs open, as before.
- `config.py` reads nothing at import; `Settings.from_env()` reads the `LMA_*`
  variables. `app_factory.create_app(settings, deps)` builds one LMA, so one
  process can serve several.
- Errors are answered as `{detail, code}`. A planner failure on
  `restrictions:plan` now answers 503 `planner_unavailable` instead of an
  unhandled 500.
- Stored source definitions come back with their connection options redacted.
- The image builds the `dagentsc` planner and puts it on `PATH`.
- Added initial Local Monitoring Agent scaffold
- Added layered package layout and placeholder FastAPI entrypoint
