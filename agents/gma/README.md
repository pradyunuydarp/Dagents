# Global Monitoring Agent (GMA)

The GMA is the global accumulation and coordination agent. Its primary role is to run models over assimilated data collected across multiple sources or local agents.

## Responsibilities

- register and track LMA capabilities
- profile assimilated datasets
- run aggregate models across multi-source data
- accumulate outputs into cross-source findings
- prepare outputs for downstream orchestration in consumer systems

## Layered Structure

- `domain/`: pure models
- `application/`: aggregation and control use-cases
- `adapters/`: interfaces for aggregate data and control ingress
- `infrastructure/`: persistence and in-memory repos

## Current State

The current implementation supports an in-memory orchestration surface with:

- registration endpoint
- heartbeat endpoint
- telemetry ingestion endpoint
- deployment planning and sync endpoints
- agent inspection endpoints
- assimilated dataset profiling endpoint
- aggregate model execution endpoint
- run dispatch tracking
- in-memory storage of agent registrations and model-run history

Runtime settings are loaded from `env/.env.shared` and `env/.env.gma`.

## Federated rounds over HTTP

By default the GMA runs a round's site work through `InProcessFederationEngine`
with no site attached, so each site posts its own result. Two variables in
`env/.env.gma` change that:

| Variable | Meaning |
|---|---|
| `GMA_FEDERATION_SITES` | The sites to reach over HTTP, as `site_id=url` entries separated by commas, for example `uk-134=http://lma-uk-134:8082,it-002=http://lma-it-002:8083`. The GMA does not start if an entry cannot be read. |
| `GMA_FEDERATION_TIMEOUT_SECONDS` | Seconds to wait for each call to a site. Default 120. |

With `GMA_FEDERATION_SITES` set, `di.py` builds `HttpFederationEngine`
(`agents/common/infrastructure/federation_http.py`). Dispatching a round then
sends the job to `POST {site}/api/v1/federation/jobs:offer` and
`POST {site}/api/v1/federation/jobs:execute` for each selected site. A site that
does not answer the offer, such as a site that is down, is recorded as a
refusal, a `rejected` result. A site that accepts and then does not answer the
execution is recorded as `failed`. The engine is still a simulator: it
calls one site at a time, does not retry and sends no credentials, so run it on
a private network. The healthcare demo's split mode uses it to reach one
service per hospital.
