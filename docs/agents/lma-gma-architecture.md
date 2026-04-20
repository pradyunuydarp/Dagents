# LMA/GMA ML Architecture for Dagents

## Summary

Dagents defines a reusable ML orchestration layer built around:

- **LMA**: Local Monitoring Agent
- **GMA**: Global Monitoring Agent

The LMA is the local execution boundary. The GMA is the aggregation and coordination boundary.

## Why This Exists

Consumer products such as Watchdog and Datalytics need a framework that can:

- execute source-level models close to data sources
- run aggregate models on assimilated datasets
- automate deployment of model workloads into Kubernetes

## Preserved Product-specific Services

The introduction of LMAs and a GMA does **not** replace product-specific services.

The agent layer wraps around those services as an ML execution and coordination plane.

## LMA Responsibilities

An LMA should operate near a local source scope, for example:

- a tenant
- a service cluster
- an application boundary
- a specific environment or event stream

Responsibilities:

- profile source-scoped datasets
- partition source data for large-scale model execution
- execute source-level models for anomaly detection, classification, or forecasting
- invoke supporting product services
- publish model summaries and artifact pointers

Expected service interactions:

- ingestion services: provide source-scoped data extraction
- analysis services: enrich local source records
- orchestration systems: publish governed outputs and actions

## GMA Responsibilities

The GMA is the fleet-wide aggregate execution agent.

Responsibilities:

- register and track deployed LMAs
- assimilate source-level outputs and shared datasets
- execute global models on multi-source or tenant-level data
- coordinate aggregate workloads and downstream automation
- correlate cross-source incidents and patterns
- forward actionable outputs to downstream orchestration or case-management systems

## Communication Model

The intended shared contract now centers on:

- dataset profiling
- model execution
- deployment planning
- workload generation
- source registration and validation

In Dagents, the shared contract lives at:

- `contracts/grpc/dagents/agents/v1/lma_gma.proto`

## Suggested Run Flow

1. GMA registers an LMA and records its scope/capabilities.
2. LMA receives or syncs a bundle/config revision.
3. LMA executes a local run over its local scope.
4. LMA optionally calls local product services.
5. LMA pushes source-level model outputs to GMA or downstream storage.
6. GMA runs aggregate models over assimilated datasets.
7. Core-service generates Kubernetes manifests for the required workloads.
8. Downstream orchestration systems apply the manifests and consume the model outcomes.

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
- keep network settings and service URLs env-driven rather than hardcoded

## Current In-memory Delivery

The current framework implementation now includes:

- LMA source dataset profiling and model execution
- GMA assimilated dataset profiling and aggregate model execution
- pipeline-level dataset profiling and model job steps
- core-service Kubernetes manifest generation for workload deployment
- env-driven Docker and compose startup for agents and services
