# Dagents Agents

This directory contains the reusable agentic control-plane services for Dagents.

## Agents

- `lma/`: Local Monitoring Agent
- `gma/`: Global Monitoring Agent

## Intent

The agent layer provides a shared foundation for products that need local execution boundaries and global coordination.

- `services/` can host reusable analysis and orchestration services
- `agents/` provides the control plane that can operate locally and globally

## Package Conventions

Each agent follows the same layered layout:

- `domain/`
- `application/`
- `adapters/`
- `infrastructure/`
- `config.py`
- `di.py`
- `main.py`

The current contents include working in-memory control-plane flows intended to make future infrastructure integrations straightforward without another repo reshuffle.
