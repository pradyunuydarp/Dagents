"""The GMA's model registry: every model version a study uses, and its status.

A version gets into the registry in one of two ways:

- an application registers it under a name it chooses
  (``PUT /api/v1/model-versions/{version}``), for example the rule its sites
  use today and the starting point of federated training;
- the GMA's HTTP engine registers the candidate a training round produced,
  named ``<version_prefix>+<round_id>``.

Every status change comes from the planner's
``dagentsc federation model lifecycle`` command: registration, aggregation,
a release evaluation and an approval are events, and the planner answers with
the status changes that follow, or a refusal. The registry applies the
changes in one atomic step of its :class:`ModelStore`, and each change names
the status it expects, so two requests cannot both change one version. If the
planner cannot be run, nothing changes.

The model kind an extension contributes computes each version's parameter
digest. The registry checks it when a version is registered, and again on
every load:

- :meth:`ModelRegistry.load` keeps a version's parameters after the first
  read, because a version's content never changes after registration, and
  recomputes the digest from the kept copy on every call;
- :meth:`ModelRegistry.released` always reads the released version from the
  store, because an approval in another process can change it.

The errors below carry the JSON ``code`` and HTTP status an agent answers
with, like the errors in :mod:`agents.common.domain.errors`:

| Error | `code` | HTTP status |
|---|---|---|
| `UnknownModelVersion` | `not_found` | 404 |
| `UnknownModelKind` | `not_found` | 404 |
| `InvalidModelVersion` | `invalid_request` | 422 |
| `ModelVersionConflict`, `ModelVersionExists`, `ModelStatusConflict` | `conflict` | 409 |
| `ModelLifecycleRefused` | `conflict` | 409; its ``refusal`` names the planner's refusal |
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import threading
from typing import Any, ClassVar, Mapping, Protocol, Sequence

from pydantic import ValidationError

from agents.common.domain.errors import DagentsError, ModelVerificationError
from agents.common.domain.federation import (
    ApprovalResult,
    JobModel,
    ModelLifecycleDecision,
    ModelReleaseDecision,
    ModelStatusChange,
    ModelVersion,
    ModelVersionList,
    ModelVersionRegistration,
)
from agents.common.extensions.registry import ExtensionError, ModelKind
from agents.common.infrastructure.dagents_runner import PlannerUnavailableError, model_lifecycle

#: Length limits of a stored version, the same as the columns' checks.
VERSION_MAX_LENGTH = 200
KIND_MAX_LENGTH = 64
DIGEST_MAX_LENGTH = 200


class UnknownModelVersion(DagentsError, KeyError):
    """No model version with this name is registered."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404

    def __str__(self) -> str:
        # KeyError would quote the message.
        return str(self.args[0]) if self.args else ""


class UnknownModelKind(DagentsError, LookupError):
    """No registered extension contributes this model kind."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class InvalidModelVersion(DagentsError, ValueError):
    """A version name, kind or digest is empty or too long, or parameters are not plain JSON."""

    code: ClassVar[str] = "invalid_request"
    http_status: ClassVar[int] = 422


class ModelVersionConflict(DagentsError, ValueError):
    """The version is registered with different contents."""

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class ModelVersionExists(ModelVersionConflict):
    """A store was asked to add a version it already holds."""


class ModelStatusConflict(ModelVersionConflict):
    """A version's status changed while the request ran, or a second version would be released."""


class ModelLifecycleRefused(DagentsError, PermissionError):
    """The planner's lifecycle rule refused the event.

    ``refusal`` is the planner's refusal kind, such as ``not_candidate`` or
    ``baseline_changed``, and ``reason`` is its sentence.
    """

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409

    def __init__(self, refusal: str, reason: str) -> None:
        super().__init__(reason)
        self.refusal = refusal
        self.reason = reason


def check_version_name(version: Any, what: str = "a model version") -> str:
    """Return ``version`` if it is a name of 1 to 200 characters, or raise :class:`InvalidModelVersion`."""
    if not isinstance(version, str) or not 1 <= len(version) <= VERSION_MAX_LENGTH:
        raise InvalidModelVersion(f"{what} must be a name of 1 to {VERSION_MAX_LENGTH} characters")
    return version


def check_new_model(model: ModelVersion) -> None:
    """Check a version before a store adds it. Raises :class:`InvalidModelVersion`."""
    check_version_name(model.version)
    if not 1 <= len(model.kind) <= KIND_MAX_LENGTH:
        raise InvalidModelVersion(f"a model kind must be a name of 1 to {KIND_MAX_LENGTH} characters")
    if not 1 <= len(model.digest) <= DIGEST_MAX_LENGTH:
        raise InvalidModelVersion(f"a digest must have 1 to {DIGEST_MAX_LENGTH} characters")
    if model.parent_version is not None:
        check_version_name(model.parent_version, "a parent version")
    if not isinstance(model.params, dict):
        raise InvalidModelVersion(f"model version {model.version} needs its parameters")
    json_copy(model.params)


def check_limit(limit: Any) -> int:
    """Return ``limit`` if it is a whole number of 1 or more, or raise ``ValueError``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"a model version listing limit must be a whole number of 1 or more, got {limit!r}")
    return limit


def json_copy(value: Any) -> Any:
    """Copy through JSON, as a database does, so both stores return the same types.

    Raises :class:`InvalidModelVersion` for a value JSON cannot hold, such as
    NaN, infinity or an object that is not a dict, list, string, number,
    boolean or null.
    """
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise InvalidModelVersion(f"model parameters must be plain JSON with finite numbers: {error}") from error


def adds_version(status_change: ModelStatusChange) -> bool:
    """Whether a status change adds a new version (it has no earlier status)."""
    return status_change.from_status is None


class ModelStore(Protocol):
    """Where model versions are kept.

    ``kind`` names the store, such as ``in-memory`` or ``postgres``.
    """

    kind: str

    def get(self, version: str) -> ModelVersion | None:
        """One version with its parameters and release decision, or ``None``."""

    def released(self) -> ModelVersion | None:
        """The released version with its parameters, or ``None``."""

    def list(self, limit: int) -> list[ModelVersion]:
        """The newest ``limit`` versions, newest first, with their parameters."""

    def apply(
        self,
        changes: Sequence[ModelStatusChange],
        *,
        new: ModelVersion | None = None,
        decision: tuple[str, ModelReleaseDecision] | None = None,
    ) -> None:
        """Apply one lifecycle event in one atomic step.

        - ``new``: a version to add. Its status must be the ``to_status`` of
          the change in ``changes`` that adds it. Raises
          :class:`ModelVersionExists` when it is stored and
          :class:`UnknownModelVersion` when its parent is not.
        - ``changes``: each other change moves one version from
          ``from_status`` to ``to_status``. Raises :class:`ModelStatusConflict`
          when a version is not stored with ``from_status``, or when the
          result would release two versions.
        - ``decision``: ``(version, decision)`` to store as the version's
          latest release decision. The store sets ``decided_at``. Raises
          :class:`UnknownModelVersion` for a version it does not hold.

        Either everything is applied or nothing is.
        """


class ModelKindSource(Protocol):
    """Where model kinds are looked up, such as an ``ExtensionRegistry``."""

    def model_kind(self, kind: str) -> ModelKind:
        """The model kind with this name. Raises when no extension contributes it."""


@dataclass(frozen=True)
class RegistrationResult:
    """What a registration did. ``created`` is false when the same content was registered before."""

    model: ModelVersion
    created: bool


def _without_params(model: ModelVersion) -> ModelVersion:
    return model.model_copy(update={"params": None})


def _planner_decision(decision: ModelReleaseDecision | None) -> dict[str, Any] | None:
    """The stored decision as the lifecycle command reads it. A missing source means unrecorded."""
    if decision is None:
        return None
    return {
        "action": decision.action,
        "guard_permitted": decision.guard_permitted,
        "metrics_source": decision.metrics_source,
        "baseline_version": decision.baseline_version,
    }


class ModelRegistry:
    """Model versions, their parameters and their status, over a :class:`ModelStore`.

    Params:
    - `store`: where the versions are kept.
    - `kinds`: where model kinds are looked up, usually the agent's
      ``ExtensionRegistry``.
    """

    def __init__(self, store: ModelStore, kinds: ModelKindSource) -> None:
        self._store = store
        self._kinds = kinds
        # version -> (kind, parameters, stored digest), kept after a load verified them.
        self._params: dict[str, tuple[str, dict[str, Any], str]] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> ModelStore:
        """The store the versions are kept in."""
        return self._store

    # --- Lookups -----------------------------------------------------------------

    def kind(self, name: str) -> ModelKind:
        """The model kind with this name. Raises :class:`UnknownModelKind`."""
        try:
            return self._kinds.model_kind(name)
        except (ExtensionError, LookupError) as error:
            raise UnknownModelKind(f"no extension contributes the model kind {name}") from error

    def get(self, version: str) -> ModelVersion | None:
        """One version without its parameters, or ``None``. Read from the store."""
        model = self._store.get(version)
        return _without_params(model) if model is not None else None

    def kind_of(self, version: str) -> str | None:
        """The name of a version's model kind, or ``None`` when it is not registered.

        A version's kind never changes, so a kind kept by :meth:`load` is used
        without reading the store again. A round's update uploads ask for the
        kind of its starting model, which the dispatch has loaded.
        """
        with self._lock:
            kept = self._params.get(version)
        if kept is not None:
            return kept[0]
        model = self._store.get(version)
        return model.kind if model is not None else None

    def require(self, version: str) -> ModelVersion:
        """One version without its parameters. Raises :class:`UnknownModelVersion`."""
        model = self.get(version)
        if model is None:
            raise UnknownModelVersion(f"model version {version} is not registered")
        return model

    def released(self, *, with_params: bool = True) -> ModelVersion | None:
        """The released version, the one sites use, or ``None``. Always read from the store.

        With ``with_params``, the parameters are included and their digest is
        recomputed first. Raises :class:`ModelVerificationError` when it does
        not match the stored digest.
        """
        model = self._store.released()
        if model is None:
            return None
        if not with_params:
            return _without_params(model)
        self._verified(model.version, model.kind, model.params or {}, model.digest)
        return model

    def list(self, limit: int = 20) -> ModelVersionList:
        """The released version and the newest ``limit`` versions, newest first, without parameters."""
        check_limit(limit)
        released = self._store.released()
        return ModelVersionList(
            model_in_use=_without_params(released) if released is not None else None,
            versions=[_without_params(model) for model in self._store.list(limit)],
        )

    def load(self, version: str, expected_digest: str | None = None) -> JobModel:
        """A version's parameters, checked, in the form a federated job carries them.

        The first load reads the version from the store; later loads use the
        kept copy. Every load recomputes the digest with the version's model
        kind.

        Raises :class:`ModelVerificationError` when the version is not
        registered, when no extension contributes its kind, when the
        parameters do not match the stored digest, or when they do not match
        ``expected_digest`` (a round manifest's ``model_artifact_digest``).
        """
        with self._lock:
            kept = self._params.get(version)
        if kept is None:
            model = self._store.get(version)
            if model is None:
                raise ModelVerificationError(f"model {version} is not in the registry")
            kept = (model.kind, json_copy(model.params or {}), model.digest)
            self._verified(version, *kept)
            with self._lock:
                kept = self._params.setdefault(version, kept)
        kind_name, params, digest = kept
        computed = self._verified(version, kind_name, params, digest)
        if expected_digest is not None and computed != expected_digest:
            raise ModelVerificationError(f"the parameters of {version} do not match the digest the round names")
        return JobModel(version=version, kind=kind_name, params=copy.deepcopy(params), digest=digest)

    # --- Events ------------------------------------------------------------------

    def register(self, version: str, registration: ModelVersionRegistration) -> RegistrationResult:
        """Register a version under a name the caller chooses.

        Registering the same content under the same name again changes
        nothing and returns ``created=False``, whatever its status is now, so
        an application can register its versions at every start.

        Raises :class:`InvalidModelVersion`, :class:`UnknownModelKind`,
        :class:`ModelVerificationError` (parameters the kind refuses),
        :class:`ModelVersionConflict` (the name holds different contents) and
        :class:`ModelLifecycleRefused` (``already_released``: a release was
        asked for while another version is released).
        """
        check_version_name(version)
        params = json_copy(registration.params)
        digest = self._digest(self.kind(registration.kind), params)
        existing = self._store.get(version)
        if existing is not None:
            return RegistrationResult(self._same_content(existing, registration.kind, params, digest, None), False)
        released = self._store.released()
        outcome = self._lifecycle(
            version,
            {
                "kind": "registered",
                "requested_status": registration.requested_status,
                "released_version": released.version if released is not None else None,
            },
        )
        new = ModelVersion(
            version=version,
            kind=registration.kind,
            digest=digest,
            status=self._added_status(outcome, version),
            params=params,
        )
        try:
            self._store.apply(outcome.status_changes, new=new)
        except ModelVersionExists:
            stored = self._store.get(version)
            if stored is None:
                raise
            return RegistrationResult(self._same_content(stored, registration.kind, params, digest, None), False)
        return RegistrationResult(self.require(version), True)

    def register_candidate(
        self, version: str, kind: str, params: Mapping[str, Any], parent_version: str
    ) -> ModelVersion:
        """Register the candidate a training round produced, as the lifecycle rule's ``aggregated`` event.

        The parent becomes ``superseded`` when it is a candidate; a released
        parent stays released. Registering the same candidate again changes
        nothing. Returns the candidate without its parameters.

        Raises :class:`UnknownModelVersion` for an unknown parent, and the
        errors :meth:`register` raises for the name, kind and parameters.
        """
        check_version_name(version)
        check_version_name(parent_version, "a parent version")
        parameters = json_copy(dict(params))
        digest = self._digest(self.kind(kind), parameters)
        existing = self._store.get(version)
        if existing is not None:
            return self._same_content(existing, kind, parameters, digest, parent_version)
        parent = self._store.get(parent_version)
        if parent is None:
            raise UnknownModelVersion(f"the parent version {parent_version} is not registered")
        outcome = self._lifecycle(
            version, {"kind": "aggregated", "parent_version": parent_version, "parent_status": parent.status}
        )
        new = ModelVersion(
            version=version,
            kind=kind,
            digest=digest,
            status=self._added_status(outcome, version),
            parent_version=parent_version,
            params=parameters,
        )
        try:
            self._store.apply(outcome.status_changes, new=new)
        except ModelVersionExists:
            stored = self._store.get(version)
            if stored is None:
                raise
            return self._same_content(stored, kind, parameters, digest, parent_version)
        return self.require(version)

    def record_release_decision(self, version: str, decision: ModelReleaseDecision) -> ModelVersion:
        """Store a release decision with the version, as the lifecycle rule's ``release_evaluated`` event.

        A candidate stays a candidate when the gates decided ``release`` and
        the Guard permitted it; otherwise it becomes ``rejected``. The store
        sets the decision's ``decided_at``. Returns the version without its
        parameters.

        Raises :class:`UnknownModelVersion`, and :class:`ModelLifecycleRefused`
        (``not_candidate``) for a version that is not a candidate. A refused
        decision is not stored.
        """
        model = self._store.get(version)
        if model is None:
            raise UnknownModelVersion(f"model version {version} is not registered")
        outcome = self._lifecycle(
            version,
            {
                "kind": "release_evaluated",
                "status": model.status,
                "action": decision.action,
                "guard_permitted": decision.guard_permitted,
            },
        )
        self._store.apply(outcome.status_changes, decision=(version, decision.model_copy(update={"decided_at": None})))
        return self.require(version)

    def approve(self, version: str) -> ApprovalResult:
        """Release a candidate, as the lifecycle rule's ``approval_requested`` event.

        The planner checks the stored release decision in this order: there
        is one, the gates decided ``release``, the Guard permitted it, its
        metrics came from the rounds, the version is a candidate, and the
        version released now is the one the gates compared it with. The
        released version and the version it replaces change in one step.

        Raises :class:`UnknownModelVersion`, :class:`ModelLifecycleRefused`
        with the planner's refusal and sentence, and
        :class:`ModelStatusConflict` when another approval changed the release
        meanwhile.
        """
        model = self._store.get(version)
        if model is None:
            raise UnknownModelVersion(f"model version {version} is not registered")
        released = self._store.released()
        outcome = self._lifecycle(
            version,
            {
                "kind": "approval_requested",
                "status": model.status,
                "decision": _planner_decision(model.release_decision),
                "released_version": released.version if released is not None else None,
            },
        )
        self._store.apply(outcome.status_changes)
        previous = next(
            (
                item.version
                for item in outcome.status_changes
                if item.from_status == "released" and item.version != version
            ),
            None,
        )
        return ApprovalResult(released=self.require(version), previous=previous)

    # --- Helpers -----------------------------------------------------------------

    def _digest(self, kind: ModelKind, params: Mapping[str, Any]) -> str:
        """The kind's digest of the parameters. Raises :class:`ModelVerificationError` for parameters it refuses."""
        try:
            return kind.parameter_digest(params)
        except ModelVerificationError:
            raise
        except (TypeError, ValueError, KeyError) as error:
            raise ModelVerificationError(f"the model kind {kind.kind} refused the parameters: {error}") from error

    def _verified(self, version: str, kind_name: str, params: Mapping[str, Any], digest: str) -> str:
        """Recompute a stored version's digest and return it. Raises :class:`ModelVerificationError`."""
        try:
            kind = self.kind(kind_name)
        except UnknownModelKind as error:
            raise ModelVerificationError(f"model {version} cannot be checked: {error}") from error
        computed = self._digest(kind, params)
        if computed != digest:
            raise ModelVerificationError(f"the stored parameters of {version} do not match its stored digest")
        return computed

    @staticmethod
    def _same_content(
        stored: ModelVersion, kind: str, params: dict[str, Any], digest: str, parent: str | None
    ) -> ModelVersion:
        same = (
            stored.kind == kind
            and stored.digest == digest
            and stored.params == params
            and stored.parent_version == parent
        )
        if not same:
            raise ModelVersionConflict(f"model version {stored.version} is already registered with different contents")
        return _without_params(stored)

    @staticmethod
    def _lifecycle(version: str, event: dict[str, Any]) -> ModelLifecycleDecision:
        """Ask the planner, and raise :class:`ModelLifecycleRefused` when it refuses."""
        payload = model_lifecycle(version, event)
        try:
            outcome = ModelLifecycleDecision.model_validate(payload)
        except ValidationError as error:
            raise PlannerUnavailableError(
                f"the planner's model lifecycle answer has the wrong shape: {error}"
            ) from error
        if not outcome.allowed:
            raise ModelLifecycleRefused(outcome.refusal or "refused", outcome.reason)
        return outcome

    @staticmethod
    def _added_status(outcome: ModelLifecycleDecision, version: str) -> str:
        for item in outcome.status_changes:
            if item.version == version and adds_version(item):
                return item.to_status
        raise PlannerUnavailableError(f"the planner's model lifecycle answer does not add {version}")


__all__ = [
    "InvalidModelVersion",
    "ModelKindSource",
    "ModelLifecycleRefused",
    "ModelRegistry",
    "ModelStatusConflict",
    "ModelStore",
    "ModelVersionConflict",
    "ModelVersionExists",
    "RegistrationResult",
    "UnknownModelKind",
    "UnknownModelVersion",
    "adds_version",
    "check_limit",
    "check_new_model",
    "check_version_name",
    "json_copy",
]
