# Dagents Agents

This directory contains the reusable ML orchestration agents for Dagents. It is
the Python package `dagents`: `pyproject.toml` at the repository root builds it,
and a wheel holds this folder without `tests/`. `python -m dagents` prints the
installed version and which planner the runner would start.

## Install and start the agents

The package is on PyPI. Install it with the `lma` and `gma` extras, check that
it found a planner, and start an agent:

```bash
pip install "dagents[lma,gma]"
python -m dagents   # the version and the planner; exits with status 1 without one
dagents-lma         # an LMA on 127.0.0.1:8010
dagents-gma         # a GMA on 127.0.0.1:8020
```

The wheels for Linux x86_64, Linux aarch64 and macOS arm64 include the planner.
On other platforms, set `DAGENTSC_BIN` to a planner built from
`bindings/ocaml/`. In a clone, `python -m dagents.lma` and
`python -m dagents.gma`, run from the repository root, start the agents from
this folder instead, and they need `DAGENTSC_BIN` too. The guide's
[Install the package](../docs/guide/00-install.md) page has every step, and the
[LMA](lma/README.md) and [GMA](gma/README.md) READMEs list the settings.

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
