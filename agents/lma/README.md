# Local Monitoring Agent (LMA)

The LMA is the local execution agent. Its primary role is to run models over per-source data close to the originating source boundary.

## Responsibilities

- extract and profile source-scoped datasets
- run source-level models over local data partitions
- invoke supporting services or product adapters
- publish model outputs and artifact pointers upward to the GMA or downstream systems

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
- source registration and validation endpoints
- source dataset profiling endpoint
- source model execution endpoint
- local bundle, model-run, and run inspection endpoints

Runtime settings are loaded from `env/.env.shared` and `env/.env.lma`.
