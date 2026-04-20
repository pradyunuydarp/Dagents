# Agent Architecture Docs

This directory captures the reusable agentic ML orchestration patterns in Dagents.

## Documents

- `lma-gma-architecture.md`: high-level architecture for the Local Monitoring Agent and Global Monitoring Agent model

## Current Implementation Notes

- the LMA now supports source dataset profiling and source-level model execution
- the GMA now supports assimilated dataset profiling, aggregate model execution, and deployment planning
- agent runtime startup is env-driven through committed files under `env/`

## Conventions

The agent scaffolds in `agents/` follow the same layered shape used across agent-oriented systems:

- `domain/`: pure typed models
- `application/`: orchestration/use-cases
- `adapters/`: technology-facing boundaries
- `infrastructure/`: messaging, persistence, and concrete implementations
- `config.py`, `di.py`, `main.py`: composition root

The intent is to keep the framework portable across products while preserving clear local and global execution boundaries.
