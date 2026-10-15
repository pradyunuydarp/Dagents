"""The model registry and the runtime that averages model updates.

Two parts, both app code:

- :class:`ModelRegistry` keeps every model version in the coordinator store:
  the seed rule, the starting point of federated training, and each version a
  training round produces. A site loads the model a round names from here and
  checks its digest before it uses it.
- :class:`ParameterAveragingRuntime` is the app's stand-in for a dedicated
  federated runtime such as Flower or NVIDIA FLARE. It implements the
  framework's ``FederationEngine`` protocol (the coordinator's side) and its
  ``UpdateChannel`` protocol (the transport a site sends its bounded update
  through). It wraps the framework's ``InProcessFederationEngine`` for job
  dispatch and metric averaging, and adds the one step that engine leaves out on
  purpose: applying the planner's FedAvg weights to the sites' clipped updates.

The framework's in-process engine stays a simulator. The parameter averaging
lives here because it needs the app's model: the parameter names, the digest
rule and where versions are stored.

The runtime holds each site's update privately, keyed by round and site, until
the round is aggregated or abandoned, and then deletes it. Updates never appear
in a ``SiteResult``, so the round evidence shows a norm and a digest per site
and no vector.
"""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from agents.common.application.federation import InProcessFederationEngine
from agents.common.domain.federation import (
    AggregationReadiness,
    CandidateModel,
    FederatedJob,
    RoundManifest,
    RoundPlan,
    RoundSubmission,
    SiteResult,
)

from app.domain import stroke_model, stroke_rule
from app.services.site_store import CoordinatorStore

#: The seed release: the hand-written points rule.
SEED_VERSION = stroke_rule.RULE_VERSION

#: Where federated training starts: the logistic-regression model with every
#: coefficient at zero. Every pilot starts from it, so it is stored once.
INITIAL_VERSION = "stroke-outcome-logreg-init"

#: Versions a training round produces are named ``<prefix>+<round id>``.
CANDIDATE_PREFIX = "stroke-outcome-logreg"

#: The model kinds this app can run.
MODEL_KINDS = (stroke_rule.RULE_KIND, stroke_model.MODEL_KIND)

#: Readable names for the logistic-regression coordinates, for the basis of an
#: assessment.
FEATURE_LABELS: dict[str, str] = {
    "age_band": "age band",
    "sex_female": "sex",
    "conscious_level": "conscious level",
    "face_weakness": "face weakness",
    "arm_weakness": "arm weakness",
    "leg_weakness": "leg weakness",
    "speech_difficulty": "speech difficulty",
    "visual_field_loss": "visual field loss",
    "hours_since_onset": "hours since onset",
    "systolic_bp": "systolic blood pressure",
    "atrial_fibrillation": "atrial fibrillation",
    "onset_on_waking": "onset on waking",
    "subtype_tacs": "subtype TACS",
    "subtype_pacs": "subtype PACS",
    "subtype_lacs": "subtype LACS",
    "subtype_pocs": "subtype POCS",
    "subtype_other": "subtype other",
}


class ModelVerificationError(ValueError):
    """A model cannot be used: it is not registered, or its digest does not match."""


class AggregationRefused(RuntimeError):
    """The runtime refused to aggregate a training round."""


class UpdateDigestMismatch(AggregationRefused):
    """A stored update does not match the digest the site reported."""


def model_digest(kind: str, params: Mapping[str, Any]) -> str:
    """The digest of a model's parameters.

    - ``points_rule``: the rule's digest. The rule is code, so the stored
      parameters must equal the parameters of the rule this process runs;
      otherwise the site would run a different rule from the one registered.
    - ``logistic_regression``: :func:`stroke_model.parameter_digest`.

    Raises :class:`ModelVerificationError` for an unknown kind, unknown
    parameters or a rule that differs from the code.
    """
    if kind == stroke_rule.RULE_KIND:
        if dict(params) != stroke_rule.rule_parameters():
            raise ModelVerificationError("the registered rule differs from the rule this site runs")
        return stroke_rule.rule_digest()
    if kind == stroke_model.MODEL_KIND:
        try:
            return stroke_model.parameter_digest(params)
        except (TypeError, ValueError) as error:
            raise ModelVerificationError(f"the model's parameters are not valid: {error}") from error
    raise ModelVerificationError(f"model kind {kind!r} is not one this site can run")


def code_digest(kind: str) -> str:
    """The digest of the code a model kind runs, which a manifest names.

    The seed rule's code is the rule itself, so it is the rule's digest. The
    logistic regression runs ``stroke_model``, including its training settings.
    """
    if kind == stroke_rule.RULE_KIND:
        return stroke_rule.rule_digest()
    if kind == stroke_model.MODEL_KIND:
        return stroke_model.training_code_digest()
    raise ModelVerificationError(f"model kind {kind!r} is not one this site can run")


def update_digest(update: Mapping[str, float]) -> str:
    """The digest of one site's update, as the site reports it."""
    return stroke_model.parameter_digest(update)


@dataclass(frozen=True)
class RegisteredModel:
    """One model version from the registry.

    ``release_decision`` is the latest release decision for this version, or
    ``None``: ``{action, guard_permitted, gate_results, round_id,
    baseline_version, decided_at}``. ``baseline_version`` is the model in use
    the gates compared the candidate with; older decisions may lack it.
    """

    version: str
    kind: str
    params: dict[str, Any]
    digest: str
    status: str
    parent_version: str | None = None
    created_at: str | None = None
    release_decision: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RegisteredModel":
        decision = row.get("release_decision")
        return cls(
            version=row["version"],
            kind=row["kind"],
            params=dict(row["params"]),
            digest=row["digest"],
            status=row["status"],
            parent_version=row.get("parent_version"),
            created_at=row.get("created_at"),
            release_decision=dict(decision) if decision is not None else None,
        )

    def row(self) -> dict[str, Any]:
        """Every field, with the parameters: the shape :meth:`from_row` reads."""
        return {**self.summary(), "params": dict(self.params)}

    def summary(self) -> dict[str, Any]:
        """The version, kind, digest, status, parent and release decision. No parameters."""
        return {
            "version": self.version,
            "kind": self.kind,
            "digest": self.digest,
            "status": self.status,
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "release_decision": self.release_decision,
        }

    def score(self, record: Mapping[str, Any]) -> float | None:
        """The estimated risk of a poor outcome at six months, 0-1.

        ``None`` when the seed rule suppresses the record because it has no
        examination finding. The logistic regression scores every record.
        """
        if self.kind == stroke_rule.RULE_KIND:
            return stroke_rule.rule_score(record)
        return stroke_model.score(self.params, record)

    def assess(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Score one record and explain the score.

        The seed rule returns its own assessment. For the logistic regression
        the basis lists the three coordinates that moved the log-odds most for
        this record. Both carry the rule's data checks as warnings, because a
        missing or unusual input matters to the reviewing clinician whichever
        model scored the record.
        """
        checks = stroke_rule.assess(record)
        if self.kind == stroke_rule.RULE_KIND:
            assessment = checks.as_dict()
            assessment["model_version"] = self.version
            assessment["model_kind"] = self.kind
            return assessment
        weights = [float(self.params.get(name, 0.0)) for name in stroke_model.FEATURES]
        values = stroke_model.encode(record)
        terms = {
            name: weight * value
            for name, weight, value in zip(stroke_model.FEATURES[1:], weights[1:], values[1:], strict=True)
            if weight * value != 0.0
        }
        largest = sorted(terms.items(), key=lambda item: (-abs(item[1]), item[0]))[:3]
        score = stroke_model.score(self.params, record)
        return {
            "model_version": self.version,
            "model_kind": self.kind,
            "score": round(score, 4),
            "priority": stroke_rule.priority_for(score),
            "contributions": {name: round(value, 4) for name, value in sorted(terms.items())},
            "basis": [f"{FEATURE_LABELS[name]}: {value:+.2f} on the log-odds scale" for name, value in largest],
            "warnings": list(checks.warnings),
            "data_quality_issues": list(checks.data_quality_issues),
            "suppressed": False,
        }


def verify_model(model: RegisteredModel, expected_digest: str) -> RegisteredModel:
    """Check a loaded model against its stored digest and the digest a round names.

    Returns the model. Raises :class:`ModelVerificationError` when its
    parameters do not match its stored digest or ``expected_digest``. A
    hospital runs this check whether it reads the registry from the
    coordinator store or over HTTP.
    """
    computed = model_digest(model.kind, model.params)
    if computed != model.digest:
        raise ModelVerificationError(f"the stored parameters of {model.version} do not match its stored digest")
    if computed != expected_digest:
        raise ModelVerificationError(f"the parameters of {model.version} do not match the digest the round names")
    return model


class ModelRegistry:
    """Model versions, stored in the coordinator store.

    A site reads model parameters from here, never patient records, so a site
    and the coordinator can both use it.

    Every read goes to the store, except inside :meth:`pinned`.
    """

    def __init__(self, store: CoordinatorStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def store(self) -> CoordinatorStore:
        return self._store

    # --- Reads pinned for the length of a pilot ---------------------------------

    @contextmanager
    def pinned(self) -> Iterator[None]:
        """Read each version from the store at most once, on this thread, inside the block.

        A pilot runs inside it. Its rounds load the same few versions about 60
        times, and each load through a pooler costs round trips. Inside the
        block:

        - the first read of a version goes to the store, and later reads of it
          use that copy;
        - a change made through this registry (a new version, a status, a
          release decision) updates the copy;
        - :meth:`load` still checks the copy's digests on every call.

        A change made to the store in some other way during the block is not
        seen until the block ends. Outside the block every read goes to the
        store, so a parameter changed in the database is refused by the next
        load. Blocks may nest; the copies are dropped when the outermost ends.
        """
        outermost = getattr(self._local, "pinned", None) is None
        if outermost:
            self._local.pinned = {}
        try:
            yield
        finally:
            if outermost:
                self._local.pinned = None

    def _pinned(self) -> dict[str, RegisteredModel] | None:
        return getattr(self._local, "pinned", None)

    def _remember(self, row: Mapping[str, Any]) -> RegisteredModel:
        model = RegisteredModel.from_row(row)
        pinned = self._pinned()
        if pinned is not None:
            pinned[model.version] = model
        return model

    # --- Reads and writes ---------------------------------------------------------

    def ensure_seed(self) -> RegisteredModel:
        """Return the released model, registering the seed rule if none is released.

        The first use of a new store registers ``stroke-outcome-rule-v1`` as the
        release. Nothing is written while a model is released. The released
        model is always read from the store, because an approval in another
        process can change it.
        """
        with self._lock:
            released = self._store.released_model()
            if released is not None:
                return self._remember(released)
            row = self._store.save_model(
                SEED_VERSION,
                stroke_rule.RULE_KIND,
                stroke_rule.rule_parameters(),
                stroke_rule.rule_digest(),
                status="released",
            )
            if row["status"] != "released":
                row = self._store.set_model_status(SEED_VERSION, "released")
            return self._remember(row)

    def model_in_use(self) -> RegisteredModel:
        """The released model: the one hospitals score their worklists with."""
        return self.ensure_seed()

    def ensure_initial(self) -> RegisteredModel:
        """The all-zero logistic regression that federated training starts from."""
        pinned = self._pinned()
        if pinned is not None and INITIAL_VERSION in pinned:
            return pinned[INITIAL_VERSION]
        params = stroke_model.initial_params()
        row = self._store.save_model(
            INITIAL_VERSION, stroke_model.MODEL_KIND, params, stroke_model.parameter_digest(params)
        )
        return self._remember(row)

    def get(self, version: str) -> RegisteredModel | None:
        """One version, or ``None``. Inside :meth:`pinned`, read once."""
        pinned = self._pinned()
        if pinned is not None and version in pinned:
            return pinned[version]
        row = self._store.get_model(version)
        return self._remember(row) if row is not None else None

    def load(self, version: str, expected_digest: str) -> RegisteredModel:
        """Load a model and check it against the digest a manifest names.

        Raises :class:`ModelVerificationError` when the version is not
        registered, when the stored parameters do not match the stored digest,
        or when they do not match ``expected_digest``.
        """
        model = self.get(version)
        if model is None:
            raise ModelVerificationError(f"model {version} is not in the registry")
        return verify_model(model, expected_digest)

    def register_candidate(self, version: str, params: Mapping[str, float], parent_version: str) -> RegisteredModel:
        """Store a version a training round produced, with status ``candidate``."""
        row = self._store.save_model(
            version,
            stroke_model.MODEL_KIND,
            dict(params),
            stroke_model.parameter_digest(params),
            parent_version=parent_version,
            status="candidate",
        )
        return self._remember(row)

    def set_status(self, version: str, status: str) -> RegisteredModel:
        return self._remember(self._store.set_model_status(version, status))

    def record_release_decision(
        self,
        version: str,
        *,
        action: str,
        guard_permitted: bool,
        gate_results: list[dict[str, Any]],
        round_id: str,
        baseline_version: str,
    ) -> RegisteredModel:
        """Store the latest release decision with the version, replacing any earlier one.

        ``baseline_version`` is the model in use that the gates compared the
        candidate with. :meth:`Consortium.approve` reads the decision from
        here, so an approval works after a restart, and refuses it once a
        different model is in use.
        """
        decision = {
            "action": action,
            "guard_permitted": guard_permitted,
            "gate_results": gate_results,
            "round_id": round_id,
            "baseline_version": baseline_version,
        }
        return self._remember(self._store.set_release_decision(version, decision))

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every version, or the newest ``limit``, oldest first, without parameters."""
        rows = self._store.list_models() if limit is None else self._store.list_models(limit=limit)
        return [RegisteredModel.from_row(row).summary() for row in rows]


@dataclass
class _StoredUpdate:
    vector: dict[str, float]
    digest: str
    job_digest: str
    norm: float = field(default=0.0)


def _checked_vector(update: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(update, Mapping):
        raise ValueError("an update must map parameter names to numbers")
    unknown = set(update) - set(stroke_model.FEATURES)
    if unknown:
        raise ValueError(f"the update names unknown parameters: {sorted(unknown)}")
    vector: dict[str, float] = {}
    for name, value in update.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"the update value for {name} is not a finite number")
        vector[name] = float(value)
    return vector


class ParameterAveragingRuntime:
    """The app's federated runtime: dispatch, then weighted parameter averaging.

    A stand-in for a dedicated runtime such as Flower or NVIDIA FLARE, run in
    one process. It implements two framework protocols:

    - ``FederationEngine``: :meth:`submit_round`, :meth:`collect_results` and
      :meth:`aggregate`. Dispatch and metric averaging are delegated to an
      ``InProcessFederationEngine``.
    - ``UpdateChannel``: :meth:`submit`, which a site's worker calls with the
      update its ``before_send`` Guard clipped.

    For a training round, :meth:`aggregate` computes::

        new = params(manifest.model_version) + sum(weight[s] * update[s])

    over the sites the planner accepted, with the planner's weights as given.
    It checks every stored update against the digest the site reported, stores
    the new version in the registry as a ``candidate`` and returns the
    candidate with ``artifact_uri`` and ``artifact_digest`` set. Other rounds
    produce metrics only.
    """

    name = "in-process-parameter-averaging"

    def __init__(self, registry: ModelRegistry, inner: InProcessFederationEngine | None = None) -> None:
        self._registry = registry
        self._inner = inner or InProcessFederationEngine()
        self._updates: dict[tuple[str, str], _StoredUpdate] = {}
        self._lock = threading.Lock()

    # --- FederationEngine -------------------------------------------------------

    def register_worker(self, worker: Any) -> None:
        """Attach one site's worker for the next round."""
        self._inner.register_worker(worker)

    def submit_round(self, manifest: RoundManifest, plan: RoundPlan) -> RoundSubmission:
        return self._inner.submit_round(manifest, plan)

    def collect_results(self, round_id: str) -> list[SiteResult]:
        return self._inner.collect_results(round_id)

    def aggregate(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> CandidateModel:
        """Average metrics, and for a training round, apply the weighted updates.

        Raises :class:`AggregationRefused` (or :class:`UpdateDigestMismatch`)
        when an accepted site's update is missing, does not match its reported
        digest, or has no planner weight, or when the starting model does not
        verify. The round's stored updates are deleted either way.
        """
        metrics_only = self._inner.aggregate(manifest, readiness, results)
        if manifest.phase != "training":
            return metrics_only
        try:
            try:
                base = self._registry.load(manifest.model_version, manifest.model_artifact_digest)
            except ModelVerificationError as error:
                raise AggregationRefused(str(error)) from error
            if base.kind != stroke_model.MODEL_KIND:
                raise AggregationRefused(f"training starts from a {base.kind}, which has no parameters to update")
            reported = {result.site_id: result for result in results}
            weighted: list[tuple[float, dict[str, float]]] = []
            with self._lock:
                for site_id in sorted(readiness.accepted_sites):
                    stored = self._updates.get((manifest.round_id, site_id))
                    if stored is None:
                        raise AggregationRefused(f"no update from {site_id} is stored for this round")
                    if update_digest(stored.vector) != stored.digest:
                        raise UpdateDigestMismatch(f"the stored update from {site_id} changed after it was sent")
                    result = reported.get(site_id)
                    if result is None or result.update_digest != stored.digest:
                        raise UpdateDigestMismatch(
                            f"the update stored for {site_id} does not match the digest the site reported"
                        )
                    weight = readiness.site_weights.get(site_id)
                    if weight is None:
                        raise AggregationRefused(f"the planner gave no weight for {site_id}")
                    weighted.append((float(weight), dict(stored.vector)))
            params = stroke_model.apply_update(base.params, weighted)
            version = f"{CANDIDATE_PREFIX}+{manifest.round_id}"
            model = self._registry.register_candidate(version, params, parent_version=manifest.model_version)
        finally:
            self.discard(manifest.round_id)
        return metrics_only.model_copy(
            update={
                "candidate_version": model.version,
                "parent_version": manifest.model_version,
                "artifact_uri": f"registry://models/{model.version}",
                "artifact_digest": model.digest,
            }
        )

    # --- UpdateChannel -------------------------------------------------------------

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        """Store one site's clipped update for this round and return its digest.

        Only training jobs send updates, and each site sends one per round.
        """
        if job.task != "train":
            raise ValueError("only a training job sends a model update")
        vector = _checked_vector(update)
        digest = update_digest(vector)
        key = (job.round_id, job.site_id)
        with self._lock:
            if key in self._updates:
                raise ValueError(f"an update from {job.site_id} is already stored for {job.round_id}")
            self._updates[key] = _StoredUpdate(
                vector=vector,
                digest=digest,
                job_digest=job.manifest_digest,
                norm=stroke_model.l2_norm(vector),
            )
        return digest

    def discard(self, round_id: str) -> None:
        """Delete every update stored for a round."""
        with self._lock:
            for key in [key for key in self._updates if key[0] == round_id]:
                del self._updates[key]

    def pending_rounds(self) -> list[str]:
        """Rounds that still have stored updates, oldest first."""
        with self._lock:
            return sorted({round_id for round_id, _ in self._updates})
