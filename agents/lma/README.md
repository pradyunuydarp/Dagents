# Local Monitoring Agent (LMA)

The LMA is the local execution agent. It mirrors the role that product-local monitoring or execution agents play in larger agent fleets.

## Responsibilities

- execute local monitoring runs for a scoped boundary
- register and heartbeat with the GMA
- invoke local supporting services or product adapters
- emit telemetry, findings, and artifact pointers upward to the GMA

## Layered Structure

- `domain/`: pure models
- `application/`: run orchestration
- `adapters/`: technology-facing contracts
- `infrastructure/`: messaging and persistence implementations

## Current State

The current implementation supports an in-memory developer flow:

- health endpoint
- bundle deployment endpoint
- run trigger endpoint
- local bundle and run inspection endpoints
- in-memory telemetry publisher
