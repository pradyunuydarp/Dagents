# LMA/GMA Architecture for Dagents

## Summary

Dagents defines a reusable agentic control plane built around:

- **LMA**: Local Monitoring Agent
- **GMA**: Global Monitoring Agent

The LMA is the local execution boundary. The GMA is the aggregation and coordination boundary.

## Why This Exists

Consumer products such as Watchdog and Datalytics need a control plane that can:

- execute local monitoring close to sources
- aggregate findings globally
- keep service roles clean and decoupled

## Preserved Product-specific Services

The introduction of LMAs and a GMA does **not** replace product-specific services.

The agent layer wraps around those services as a monitoring and coordination plane.

## LMA Responsibilities

An LMA should operate near a local monitoring scope, for example:

- a tenant
- a service cluster
- an application boundary
- a specific environment or event stream

Responsibilities:

- register with the GMA
- send heartbeat and local status
- sync monitoring bundles/configuration
- execute monitoring runs for a local scope
- invoke supporting product services
- publish telemetry, summaries, and artifact pointers

Expected service interactions:

- ingestion services: consume normalized events or submit scoped triggers
- analysis services: classify or enrich local signals
- orchestration systems: publish governed outputs and actions

## GMA Responsibilities

The GMA is the fleet-wide coordinator.

Responsibilities:

- register and track deployed LMAs
- accept telemetry and heartbeat streams
- distribute bundle/config updates
- trigger or coordinate local runs
- accumulate outputs from many LMAs
- correlate cross-LMA incidents and patterns
- forward actionable outputs to a downstream orchestration or case-management system

## Communication Model

The intended control contract follows the same shape across consumers:

- agent registration
- heartbeat
- telemetry push
- deployment/bundle sync
- run trigger/dispatch

In Dagents, the shared contract lives at:

- `contracts/grpc/dagents/agents/v1/lma_gma.proto`

## Suggested Run Flow

1. GMA registers an LMA and records its scope/capabilities.
2. LMA receives or syncs a bundle/config revision.
3. LMA executes a local run over its local scope.
4. LMA optionally calls local product services.
5. LMA pushes telemetry and summarized findings to GMA.
6. GMA correlates outputs across agents.
7. GMA forwards incident-level outcomes into a downstream orchestration system.

## Repository Shape

```text
agents/
├── gma/
│   ├── domain/
│   ├── application/
│   ├── adapters/
│   └── infrastructure/
└── lma/
    ├── domain/
    ├── application/
    ├── adapters/
    └── infrastructure/
```

## Immediate Implementation Strategy

- start with in-memory repositories and publishers for both agents
- expose health and control endpoints first
- keep messaging and persistence behind interfaces
- add broker-backed infrastructure later
- integrate with consumer-specific services after the contracts stabilize

## Current In-memory Delivery

The current framework implementation now includes:

- LMA bundle deployment and local run history
- GMA deployment planning and sync evaluation
- telemetry rollups and fleet overview inspection
- run dispatch recording for downstream execution orchestration
