"""The demo API for the browser approval check (``tests/run_approval_check.sh``).

``frontend/approve.mjs`` needs two candidates that the release gates allow.
On the committed records the real gates decide ``reject``, so this server
gives the condition pack one gate that every candidate clears, as the API
tests do (``support.gated_extension``). The planner still makes each release
decision, and the GMA still decides each approval. The agents run in this
process and every hospital keeps its records in memory.

The check changes the model in use, so this server refuses to start with a
database setting: an approval there would change the model a real store uses.

Run it from ``apps/healthcare-demo`` with ``PYTHONPATH=<Dagents>:backend``:

    python -m tests.approval_server --port 8080
"""

from __future__ import annotations

import argparse
import os
import sys

#: One gate every candidate clears (an AUC of at least 0), so a pilot decides ``release``.
GATE_ID = "any_auc"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    # The env files never replace an exported variable, so these hold.
    os.environ["HEALTHCARE_DEMO_AGENTS"] = "in-process"
    os.environ["HEALTHCARE_DEMO_DEPLOYER"] = "off"

    import uvicorn

    from app import main as api
    from app.services.agents import HealthcareDemo
    from dagents.common.domain.federation import GateComparison, ReleaseGate

    from tests.support import gated_extension

    # Checked after the import, because the app's settings also read the env files.
    databases = sorted(
        name
        for name, value in os.environ.items()
        if value and name.startswith("HEALTHCARE_DEMO_") and name.endswith("DATABASE_URL")
    )
    if databases or api.settings.database_url:
        names = ", ".join(databases or ["HEALTHCARE_DEMO_DATABASE_URL"])
        print(f"The approval check runs on in-memory records only. Unset {names}.", file=sys.stderr)
        return 1

    gate = ReleaseGate(gate_id=GATE_ID, metric="auc", comparison=GateComparison(kind="at_least", value=0.0))
    # The routes read ``main.demo`` on each request, so replacing it before the
    # server starts gives every route these gates.
    api.demo = HealthcareDemo(extension=gated_extension([gate]))
    uvicorn.run(api.app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
