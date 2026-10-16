"""Shared test helpers for the healthcare demo suite."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import pathlib
import shutil
from typing import Any, Iterator
from unittest import mock
import warnings

from agents.common.infrastructure.dagents_runner import dagentsc_binary

#: Where ``dune build`` puts the planner, relative to the Dagents checkout.
#:
#: Resolved from this file so the suite works whether the demo is run inside the
#: Dagents repository or extracted into its own, provided ``DAGENTS_HOME`` or a
#: sibling checkout supplies the framework.
_CANDIDATES = [
    pathlib.Path(__file__).resolve().parents[3] / "bindings/ocaml/_build/default/bin/dagentsc.exe",
    pathlib.Path(os.getenv("DAGENTS_HOME", "")) / "bindings/ocaml/_build/default/bin/dagentsc.exe",
]


def dagentsc_available() -> bool:
    """Whether the OCaml planner can be reached from this environment.

    The governed tests need the real planner: stubbing it would prove the app
    calls something, not that the framework's governance holds.
    """
    binary = dagentsc_binary()
    if os.path.isfile(binary) or shutil.which(binary) is not None:
        return True
    for candidate in _CANDIDATES:
        if candidate.is_file():
            os.environ.setdefault("DAGENTSC_BIN", str(candidate))
            return True
    # Warn rather than skipping in silence. A suite that quietly drops its
    # governance tests reports OK without having checked anything, and the
    # reader has no way to tell that from a real pass.
    warnings.warn(
        "dagentsc was not found, so the governed tests will skip. Build it with "
        "`dune build ./bin/dagentsc.exe` in the Dagents checkout, and set DAGENTS_HOME "
        "or DAGENTSC_BIN if this app lives outside that repository.",
        RuntimeWarning,
        stacklevel=2,
    )
    return False


def tiny_site(count: int = 5, site_id: str = "tiny-001"):
    """A fourth hospital holding ``count`` records copied from uk-134's extract.

    Returns ``(site, store)``: a ``Site`` and a ``MemorySiteStore``. The records
    keep their fields and get the tiny site's own ids and source rows.
    """
    from app.domain.dataset import Site, load_site_records
    from app.services.site_store import MemorySiteStore

    site = Site(site_id, "Tiny clinic", "Nowhere", 0, "site_" + site_id.replace("-", "_"), count)
    records = []
    for number, record in enumerate(load_site_records("uk-134")[:count], start=1):
        copy = dict(record)
        copy["encounter_id"] = f"{site_id}-{number:05d}"
        copy["source_row"] = number
        records.append(copy)
    return site, MemorySiteStore(site, records)


@dataclass
class BuiltAgent:
    """One agent app the app built in process: its settings, what the app handed it, and the app."""

    settings: Any
    deps: Any
    app: Any

    @property
    def state(self) -> Any:
        """What the agent serves from: ``app.state.lma`` (an LMA runtime) or ``app.state.gma`` (the GMA state)."""
        return getattr(self.app.state, "lma", None) or self.app.state.gma


@contextmanager
def recorded_agents() -> Iterator[dict[str, BuiltAgent]]:
    """Record the agent apps ``app.services.agents`` builds while the block runs.

    Yields a dict that fills as the agents are built: ``"gma"`` and
    ``"lma:<site_id>"`` map to a :class:`BuiltAgent`. Tests use it to look at
    what each agent was given, and to change one agent's behaviour, without the
    app exposing its agents.
    """
    import app.services.agents as wiring

    built: dict[str, BuiltAgent] = {}
    real_lma, real_gma = wiring.create_lma_app, wiring.create_gma_app

    def lma(settings: Any, deps: Any) -> Any:
        app = real_lma(settings, deps)
        built[f"lma:{settings.site_id}"] = BuiltAgent(settings, deps, app)
        return app

    def gma(settings: Any, deps: Any) -> Any:
        app = real_gma(settings, deps)
        built["gma"] = BuiltAgent(settings, deps, app)
        return app

    with mock.patch.object(wiring, "create_lma_app", lma), mock.patch.object(wiring, "create_gma_app", gma):
        yield built


def built_demo(demo: Any) -> dict[str, BuiltAgent]:
    """Build ``demo``'s services now and return the agents it built (see :func:`recorded_agents`)."""
    with recorded_agents() as built:
        demo.ready()
    return built


def gated_extension(gates: list[Any]) -> Any:
    """The app's extension with these release gates on its condition pack, for tests that need a known decision."""
    from app.domain.conditions import STROKE_CONDITION
    from app.extension import HealthcareStrokeExtension

    class GatedExtension(HealthcareStrokeExtension):
        def condition_packs(self) -> list[Any]:
            return [STROKE_CONDITION.model_copy(update={"release_gates": list(gates)})]

    return GatedExtension()


#: A record the seed rule scores near 1: over 90, unconscious, every deficit,
#: atrial fibrillation and a total anterior circulation stroke.
HIGH_RISK_ENTRY = {
    "age": 93,
    "sex": "female",
    "conscious_level": "unconscious",
    "face_weakness": True,
    "arm_weakness": True,
    "leg_weakness": True,
    "speech_difficulty": True,
    "hours_since_onset": 5.0,
    "systolic_bp": 170.0,
    "visual_field_loss": True,
    "atrial_fibrillation": True,
    "onset_on_waking": False,
    "stroke_subtype": "TACS",
}
