# Dagents Agents

This directory contains the reusable ML orchestration agents for Dagents. It is
the Python package `dagents`: `pyproject.toml` at the repository root builds it,
and a wheel holds this folder without `tests/`. `python -m dagents` prints the
installed version and which planner the runner would start.

## Agents

- `lma/`: Local Monitoring Agent
- `gma/`: Global Monitoring Agent

## Intent

The agent layer provides a shared foundation for products that need local source-level model execution and global aggregate model execution.

- `services/` can host reusable training, pipeline, and deployment services
- `dagents/` provides LMA and GMA execution layers for local and aggregate model runs
- runtime configuration comes from `env/.env.shared` plus `env/.env.lma` or `env/.env.gma` in a checkout or an image, and only from the process environment in an installed package

## Package Conventions

Each agent follows the same layered layout:

- `domain/`
- `application/`
- `adapters/`
- `infrastructure/`
- `config.py`
- `di.py`
- `main.py`

The current contents include working in-memory ML orchestration flows, source registration/profile/model APIs, and env-driven runtime startup intended to make future infrastructure integrations straightforward without another repo reshuffle.
