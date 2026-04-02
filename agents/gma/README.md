# Global Monitoring Agent (GMA)

The GMA is the global accumulation and coordination agent. It accepts telemetry and control-plane updates from one or more LMAs and produces global conclusions for downstream systems.

## Responsibilities

- register and track LMAs
- receive heartbeats and telemetry
- coordinate bundle deployment and run dispatch
- accumulate outputs into cross-LMA findings
- prepare outputs for downstream orchestration in consumer systems

## Layered Structure

- `domain/`: pure models
- `application/`: aggregation and control use-cases
- `adapters/`: interfaces for telemetry/control ingress
- `infrastructure/`: persistence and in-memory repos

## Current State

The current implementation supports an in-memory control plane with:

- registration endpoint
- heartbeat endpoint
- deployment planning and sync endpoints
- telemetry ingestion and summary endpoints
- run dispatch tracking
- in-memory storage of agent registrations and telemetry events
