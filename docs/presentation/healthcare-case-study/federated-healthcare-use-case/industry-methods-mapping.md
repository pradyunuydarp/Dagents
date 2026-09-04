# Industry methods mapped to the Dagents healthcare use case

This companion note answers the professor's four prompts directly: federated AI, the NVIDIA example, federated databases and analytics, and enterprise backup software.

## Short answer

Dagents should not try to replace all four areas.

- Use a specialist federated-learning runtime for distributed training.
- Use Dagents to coordinate participants, jobs, versions, policies, evidence, rollout, and rollback.
- Use federated analytics before training and throughout evaluation.
- Use a federated database mainly inside each hospital or for tightly controlled aggregate queries.
- Borrow enterprise backup patterns for model integrity and recovery.

## Method map

| Industry method | Basic pattern | Best Dagents use | What Dagents must not assume |
|---|---|---|---|
| Federated learning | central model goes out; local updates come back; server aggregates | GMA manages the round; LMA runs the local job; FL engine aggregates | updates are safe simply because raw data stayed local |
| Federated analytics | local query or metric; combined result only | preflight data readiness, evaluation, drift, workload and alert summaries | all aggregates are non-sensitive |
| Federated database | global catalog and query planner over remote sources | build a local hospital feature view and push filters to sources | federation means no data movement or no privacy risk |
| Enterprise backup | central policy, local mover, baseline, incrementals, catalog, verification, restore | signed seed, bounded updates, immutable releases, health checks, rollback drills | an incremental model update is equivalent to a trusted data block |

## Federated AI study notes

Federated averaging starts with a global model, sends it to clients, trains locally, and averages returned updates. In a hospital consortium this is **cross-silo federated learning** because the clients are organizations rather than millions of personal devices.

Production concerns beyond the averaging algorithm:

- participant identity and eligibility;
- local code approval;
- source and schema contracts;
- minimum participation threshold;
- secure aggregation;
- contribution clipping and privacy analysis;
- site dropouts, retries, and round cancellation;
- model poisoning and anomalous updates;
- cross-site evaluation;
- signed release and rollback.

## NVIDIA case-study notes

NVIDIA FLARE provides a practical server/client runtime for federated jobs. The EXAM research collaboration used 20 institutions and 16,148 cases to train a model using electronic-record features and chest X-rays without exchanging the raw site datasets. The study reported stronger average performance and generalizability than local-only models for the studied tasks.

The useful lesson is operational: a real healthcare federation needs repeatable experiments, site workers, model exchange, local evaluation, and strong governance. The result cannot be copied as a performance promise for stroke triage or Dagents.

## Federated analytics study notes

Federated analytics runs local computations and exposes approved aggregates instead of training a model. For Dagents, this should be the first cross-hospital workload:

- count eligible cases;
- measure missing fields and unit mismatches;
- compare label availability;
- evaluate the seed model at every site;
- measure runtime, failures, and message size;
- compare drift and alert burden later.

Secure aggregation and minimum cohort rules can protect site-level or patient-group results. Small groups still require special care.

## Federated database study notes

Database federation gives one logical query surface over remote sources. A query planner may push work to the source and combine returned results. This is useful for LMA-side feature construction across the EHR, laboratory database, and device data.

It is not the main cross-hospital privacy mechanism. A federated query may still return detailed rows or let a user infer sensitive facts through repeated queries. The GMA path should be limited to approved aggregate contracts.

## Enterprise backup study notes

Common backup architecture separates the central control server from source-side data movers and repositories. Backup systems also use baselines, incremental chains, catalogs, integrity checks, immutable copies, and restore tests.

The Dagents translation is:

```text
backup server          -> GMA release and policy control
source-side proxy      -> hospital LMA / local worker
full baseline          -> signed seed model and code package
incremental backup     -> bounded model update and round manifest
backup catalog         -> model registry and lineage
health check           -> digest, signature, schema, and evaluation checks
immutable restore      -> locked approved release
restore test           -> scheduled rollback drill
```

Do not push the analogy further than operations. Backup systems copy and reconstruct state; federated learning optimizes a new model from distributed signals.

## Design recommendation

The clean architecture is a composition:

```text
Dagents GMA/LMA control plane
  + specialist FL engine such as NVIDIA FLARE
  + federated analytics jobs
  + hospital-local data federation where useful
  + backup-inspired model registry, verification, and recovery controls
```

This gives Dagents a credible role: it is the reusable governance and execution layer that makes specialist techniques safer and repeatable across hospitals.

## Dagents versus existing frameworks and SaaS

| Option | Pros | Cons |
|---|---|---|
| Dagents + specialist FL engine | source-local LMA boundary; reusable GMA orchestration; portable contracts; evidence, deployment, and rollback under one design | integration effort; consortium owns production operations and assurance |
| FL framework alone | strong learning algorithms, simulation, and privacy extensions | orchestration, release governance, evidence, and product integration still need a surrounding control plane |
| Managed FL SaaS | fast setup and vendor support | lock-in, recurring cost, constrained deployment choices, and less control over evidence or recovery |
| Central warehouse + ML | familiar central tools | large and delayed data flows, copied sensitive records, and a larger compliance boundary |
| One-off custom build | tailored to one network | hard to reuse, maintain, and extend to the next condition |

## Sources

See `dagents-federated-healthcare-use-case.md` for the full source list and links.
