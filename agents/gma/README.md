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
