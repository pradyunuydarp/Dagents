# Agent Architecture Docs

This directory captures the reusable agentic control-plane patterns in Dagents.

## Documents

- `lma-gma-architecture.md`: high-level architecture for the Local Monitoring Agent and Global Monitoring Agent model

## Current Implementation Notes

- the LMA now supports in-memory bundle deployment, run execution, and telemetry inspection
- the GMA now supports registration, heartbeat tracking, deployment planning, deployment sync, telemetry aggregation, and run dispatch inspection

## Conventions

The agent scaffolds in `agents/` follow the same layered shape used across agent-oriented systems:

- `domain/`: pure typed models
- `application/`: orchestration/use-cases
- `adapters/`: technology-facing boundaries
- `infrastructure/`: messaging, persistence, and concrete implementations
- `config.py`, `di.py`, `main.py`: composition root

The intent is to keep the framework portable across products while preserving clear local and global execution boundaries.
