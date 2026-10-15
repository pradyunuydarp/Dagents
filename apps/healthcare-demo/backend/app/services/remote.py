"""Clients for the parts of the demo that run in other processes.

In a split run (``HEALTHCARE_DEMO_ROLE`` ``lma`` or ``app``) each hospital is
its own LMA process, the Dagents GMA service runs the rounds, and the app
process keeps the model registry and the update store. These classes stand in
for the in-process objects, so :class:`~app.services.consortium.Consortium` and
:class:`~app.services.hospital.Hospital` run the same code in both layouts:

- :class:`RemoteModelRegistry`: a hospital's view of the app's model registry.
  It runs the same digest checks as :class:`~app.services.runtime.ModelRegistry`.
- :class:`RemoteHospital`: the app's view of one hospital's LMA. It reads what
  the hospital reports (``GET /api/v1/site/report``), never a record.
- :class:`GmaRoundClient`: the round controller, over the Dagents GMA's
  federation API. The GMA's planner decides eligibility, quorum, aggregation
  and the release gates. The app averages the stored updates.
- :class:`GmaReleaseGuard`: the release Guard, run by the Dagents GMA.

Every client takes an optional ``client`` (an ``httpx.Client``, or a FastAPI
``TestClient`` in tests). A service that cannot be reached, or answers with a
server error, raises :class:`RemoteServiceUnavailable`, which the app answers
with a 503. Its message names the service, never its address.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from agents.common.domain.federation import (
    AggregationReadiness,
    CandidateModel,
    ReleaseDecision,
    ReleaseGate,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    SiteRegistration,
)
from agents.common.domain.governance import DataClassification, GuardDecision, RestrictionRequest
from agents.common.infrastructure.federation_http import DEFAULT_TIMEOUT_SECONDS, HttpSiteWorker

from app.domain.dataset import Site
from app.services.runtime import (
    ModelVerificationError,
    ParameterAveragingRuntime,
    RegisteredModel,
    verify_model,
)

logger = logging.getLogger(__name__)

#: Where a hospital reads the model in use, and one model with its parameters.
MODEL_IN_USE_PATH = "/api/v1/registry/model-in-use"
MODEL_PATH = "/api/v1/registry/models/{version}"

#: Where the app reads what a hospital reports.
SITE_REPORT_PATH = "/api/v1/site/report"

#: The engine name a pilot report shows when the Dagents GMA runs the rounds.
GMA_ENGINE_NAME = "dagents-gma"

#: Model versions a hospital keeps after loading them. A version's parameters
#: never change, so a kept copy stays valid; its digests are checked on every load.
MODEL_CACHE_SIZE = 64

COORDINATOR_NAME = "The study coordinator"
GMA_NAME = "The Dagents GMA"


class RemoteServiceUnavailable(RuntimeError):
    """Another process of the demo cannot be reached, or answered with a server error.

    ``public_message`` is safe to show a visitor: it names the service, never
    its address. The full error, with the address, is in ``str(error)`` and
    goes to the log only.
    """

    def __init__(self, public_message: str, detail: str = "") -> None:
        super().__init__(f"{public_message} ({detail})" if detail else public_message)
        self.public_message = public_message


def _unavailable(name: str) -> str:
    return f"{name} cannot be reached right now. Try again in a minute."


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, Mapping) and "detail" in body:
        return str(body["detail"])[:200]
    return str(body)[:200]


class _Endpoint:
    """One service's base address, with an optional client to send through."""

    def __init__(self, name: str, base_url: str, *, timeout: float, client: httpx.Client | None) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send one request. ``timeout`` replaces the endpoint's for this request."""
        url = f"{self.base_url}{path}"
        wait = self._timeout if timeout is None else timeout
        try:
            if self._client is not None:
                return self._client.request(method, url, json=json, params=params, timeout=wait)
            with httpx.Client(timeout=wait) as client:
                return client.request(method, url, json=json, params=params)
        except httpx.HTTPError as error:
            raise RemoteServiceUnavailable(_unavailable(self.name), f"{method} {url}: {error}") from error

    def server_error(self, method: str, path: str, response: httpx.Response) -> RemoteServiceUnavailable:
        return RemoteServiceUnavailable(
            f"{self.name} could not complete the request. Try again in a minute.",
            f"{method} {self.base_url}{path} answered {response.status_code}: {_detail(response)}",
        )


# --- The hospital's side ---------------------------------------------------------------------


class RemoteModelRegistry:
    """The app's model registry, as a hospital in its own process reads it.

    It implements what :class:`~app.services.hospital.Hospital` uses of
    :class:`~app.services.runtime.ModelRegistry`:

    - :meth:`load` reads one version from the app and runs
      :func:`~app.services.runtime.verify_model`, the same checks
      ``ModelRegistry.load`` runs. A version the app does not hold, or a
      registry that cannot be reached, raises ``ModelVerificationError``, so
      the hospital refuses the job.
    - :meth:`model_in_use` asks the app every time, because an approval can
      change it.

    Params:
    - `coordinator_url`: the app process, ``HEALTHCARE_DEMO_COORDINATOR_URL``.
    - `timeout`: seconds to wait for each call.
    - `client`: an ``httpx.Client`` to send through, for tests.
    """

    def __init__(
        self,
        coordinator_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = _Endpoint(COORDINATOR_NAME, coordinator_url, timeout=timeout, client=client)
        self._cache: OrderedDict[str, RegisteredModel] = OrderedDict()
        self._lock = threading.Lock()

    def _model(self, path: str) -> RegisteredModel | None:
        response = self._endpoint.request("GET", path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._endpoint.server_error("GET", path, response)
        try:
            return RegisteredModel.from_row(response.json())
        except (KeyError, TypeError, ValueError) as error:
            raise RemoteServiceUnavailable(
                f"{COORDINATOR_NAME} sent a model this hospital cannot read.", f"GET {path}: {error}"
            ) from error

    def get(self, version: str) -> RegisteredModel | None:
        """One version with its parameters, or ``None`` when the app does not hold it."""
        with self._lock:
            cached = self._cache.get(version)
            if cached is not None:
                self._cache.move_to_end(version)
                return cached
        model = self._model(MODEL_PATH.format(version=quote(version, safe="")))
        if model is None:
            return None
        if model.version != version:
            raise RemoteServiceUnavailable(
                f"{COORDINATOR_NAME} sent a different model from the one asked for.",
                f"asked for {version}, got {model.version}",
            )
        with self._lock:
            self._cache[version] = model
            while len(self._cache) > MODEL_CACHE_SIZE:
                self._cache.popitem(last=False)
        return model

    def load(self, version: str, expected_digest: str) -> RegisteredModel:
        """Load a model and check it against the digest a manifest names.

        Raises ``ModelVerificationError`` when the version is not registered,
        when the registry cannot be reached, or when the parameters do not
        match the stored digest or ``expected_digest``.
        """
        try:
            model = self.get(version)
        except RemoteServiceUnavailable as error:
            logger.warning("model %s could not be loaded: %s", version, error)
            raise ModelVerificationError(f"the model registry could not be reached to load {version}") from error
        if model is None:
            raise ModelVerificationError(f"model {version} is not in the registry")
        return verify_model(model, expected_digest)

    def model_in_use(self) -> RegisteredModel:
        """The released model, read from the app now.

        Raises :class:`RemoteServiceUnavailable` when the app cannot be
        reached, or when the model's parameters do not match its digest.
        """
        model = self._model(MODEL_IN_USE_PATH)
        if model is None:
            raise RemoteServiceUnavailable(_unavailable(COORDINATOR_NAME), f"GET {MODEL_IN_USE_PATH} answered 404")
        try:
            return verify_model(model, model.digest)
        except ModelVerificationError as error:
            raise RemoteServiceUnavailable(
                f"{COORDINATOR_NAME} sent a model in use that does not match its digest.", str(error)
            ) from error


# --- The app's side ----------------------------------------------------------------------------


class _RemoteStoreKind:
    """What the app knows of a remote hospital's store: its kind."""

    def __init__(self, hospital: "RemoteHospital") -> None:
        self._hospital = hospital

    @property
    def kind(self) -> str:
        return self._hospital.store_kind()


class RemoteHospital:
    """One hospital's LMA, as the app process sees it.

    It implements what :class:`~app.services.consortium.Consortium` uses of a
    hospital. Every number comes from the hospital's report
    (``GET /api/v1/site/report``): its summary, registration, counts, store
    kind and audit summary. The report holds no record. :meth:`worker` returns
    the transport the Dagents GMA uses to send this hospital a job.

    Params:
    - `site`: the hospital's identity.
    - `base_url`: the LMA's address, from ``HEALTHCARE_DEMO_LMA_URLS``.
    - `series_slot`: the hospital's chart colour slot.
    - `timeout`: seconds to wait for a report.
    - `federation_timeout`: seconds a round job may take.
    - `client`: an ``httpx.Client`` to send through, for tests.
    """

    def __init__(
        self,
        site: Site,
        base_url: str,
        *,
        series_slot: int | None = None,
        timeout: float = 30.0,
        federation_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.site = site
        self.site_id = site.site_id
        self.display_name = site.display_name
        self.country = site.country
        self.series_slot = series_slot
        self._endpoint = _Endpoint(site.display_name, base_url, timeout=timeout, client=client)
        self._federation_timeout = federation_timeout
        self._client = client
        self._store_kind: str | None = None
        self.store = _RemoteStoreKind(self)

    def report(self) -> dict[str, Any]:
        """``{site_id, summary, registration, counts, store_kind, audit_summary}``, read now."""
        response = self._endpoint.request("GET", SITE_REPORT_PATH)
        if response.status_code != 200:
            raise self._endpoint.server_error("GET", SITE_REPORT_PATH, response)
        report = response.json()
        if not isinstance(report, Mapping) or report.get("site_id") != self.site_id:
            raise RemoteServiceUnavailable(
                f"{self.display_name} sent a report for a different hospital.",
                f"expected {self.site_id}, got {report.get('site_id') if isinstance(report, Mapping) else report!r}",
            )
        self._store_kind = str(report["store_kind"])
        return dict(report)

    def summary(self) -> dict[str, Any]:
        return dict(self.report()["summary"])

    def registration(self, conditions: list[str] | None = None) -> SiteRegistration:
        registration = SiteRegistration.model_validate(self.report()["registration"])
        if conditions:
            registration = registration.model_copy(update={"approved_conditions": list(conditions)})
        return registration

    def counts(self) -> dict[str, int]:
        return dict(self.report()["counts"])

    def record_count(self) -> int:
        return int(self.counts()["total"])

    def audit_summary(self) -> dict[str, Any]:
        return dict(self.report()["audit_summary"])

    def store_kind(self) -> str:
        """The kind of the hospital's store, ``unknown`` while it has never answered."""
        if self._store_kind is None:
            try:
                self.report()
            except RemoteServiceUnavailable as error:
                logger.warning("%s", error)
                return "unknown"
        return self._store_kind or "unknown"

    def worker(self, expected_digest: str | None = None, update_channel: Any = None) -> HttpSiteWorker:
        """The transport a coordinator in this process would send jobs through.

        The hospital computes the digest it expects itself, and sends its
        update to the app on its own, so both parameters are ignored.
        """
        return HttpSiteWorker(
            self.site_id, self._endpoint.base_url, timeout=self._federation_timeout, client=self._client
        )

    def close(self) -> None:
        """Nothing to release: each call opens and closes its own connection."""


class _GmaEndpoint(_Endpoint):
    def call(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        """Send one request and return the JSON answer.

        A 404 raises ``KeyError`` and a 409 ``PermissionError``, as the
        in-process controller does. Any other error status raises
        :class:`RemoteServiceUnavailable`.
        """
        response = self.request(method, path, json=body, timeout=timeout)
        if response.status_code == 404:
            raise KeyError(_detail(response))
        if response.status_code == 409:
            raise PermissionError(_detail(response))
        if response.status_code >= 400:
            raise self.server_error(method, path, response)
        try:
            return response.json()
        except ValueError as error:
            raise RemoteServiceUnavailable(
                f"{self.name} sent an answer the app cannot read.", f"{method} {path}: {error}"
            ) from error


def _round_path(round_id: str, suffix: str = "") -> str:
    return f"/api/v1/federation/rounds/{quote(round_id, safe='')}{suffix}"


class GmaRoundClient:
    """The round controller, run by the Dagents GMA service over its HTTP API.

    It implements what :class:`~app.services.consortium.Consortium` uses of
    ``FederatedRoundController``. The GMA's planner decides which sites are
    eligible, whether quorum holds, whether a round may be aggregated and
    whether a candidate passes its gates. The GMA sends each job to the
    hospitals' LMAs. The app keeps what the framework leaves to an
    application: the model registry and the stored updates, which
    :meth:`aggregate` averages with the app's ``ParameterAveragingRuntime``
    once the GMA permits it.

    Params:
    - `gma_url`: the Dagents GMA, ``GMA_PUBLIC_URL``.
    - `runtime`: the app's runtime, which holds the updates the hospitals sent.
    - `timeout`: seconds to wait for each call but a dispatch.
    - `round_timeout`: seconds to wait for a dispatch, which answers after the
      GMA has offered and run the job at every hospital, one after another.
      Defaults to `timeout`. See :func:`app.services.roles.round_timeout`.
    - `client`: an ``httpx.Client`` to send through, for tests.
    """

    def __init__(
        self,
        gma_url: str,
        runtime: ParameterAveragingRuntime,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        round_timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._gma = _GmaEndpoint(GMA_NAME, gma_url, timeout=timeout, client=client)
        self._round_timeout = timeout if round_timeout is None else round_timeout
        self._runtime = runtime

    @property
    def engine_name(self) -> str:
        return GMA_ENGINE_NAME

    def register_site(self, registration: SiteRegistration) -> SiteRegistration:
        path = f"/api/v1/federation/sites/{quote(registration.site_id, safe='')}"
        return SiteRegistration.model_validate(self._gma.call("PUT", path, registration.model_dump(mode="json")))

    def list_sites(self) -> list[SiteRegistration]:
        return [SiteRegistration.model_validate(row) for row in self._gma.call("GET", "/api/v1/federation/sites")]

    def plan_round(self, manifest: RoundManifest) -> RoundPlan:
        return RoundPlan.model_validate(
            self._gma.call("POST", "/api/v1/federation/rounds:plan", manifest.model_dump(mode="json"))
        )

    def digest(self, manifest: RoundManifest) -> str:
        """The manifest's digest, as the GMA's planner computes it."""
        return self.plan_round(manifest).round_digest

    def dispatch_round(self, manifest: RoundManifest) -> RoundRecord:
        return RoundRecord.model_validate(
            self._gma.call(
                "POST", "/api/v1/federation/rounds", manifest.model_dump(mode="json"), timeout=self._round_timeout
            )
        )

    def get_round(self, round_id: str) -> RoundRecord | None:
        try:
            return RoundRecord.model_validate(self._gma.call("GET", _round_path(round_id)))
        except KeyError:
            return None

    def evaluate_readiness(self, round_id: str) -> AggregationReadiness:
        return AggregationReadiness.model_validate(self._gma.call("GET", _round_path(round_id, "/readiness")))

    def aggregate(self, round_id: str) -> CandidateModel:
        """Ask the GMA to aggregate, then average the stored updates in the app.

        The GMA's planner permits the round or refuses it with a 409, which
        raises ``PermissionError`` as the in-process controller does. The GMA
        saves its readiness check with the round; the app's runtime aggregates
        with that check and the round's results, so the weights are the
        planner's.
        """
        self._gma.call("POST", _round_path(round_id, ":aggregate"))
        record = self.get_round(round_id)
        if record is None or record.readiness is None:
            raise RemoteServiceUnavailable(
                f"{GMA_NAME} did not keep the round it aggregated.", f"round {round_id} has no readiness"
            )
        return self._runtime.aggregate(record.manifest, record.readiness, list(record.results))

    def evaluate_release(
        self,
        round_id: str,
        gates: list[ReleaseGate],
        candidate: CandidateModel,
        baseline_metrics: dict[str, float] | None = None,
        rollback_version: str | None = None,
    ) -> ReleaseDecision:
        body = {
            "gates": [gate.model_dump(mode="json") for gate in gates],
            "candidate_version": candidate.candidate_version,
            "candidate_metrics": dict(candidate.metrics),
            "baseline_metrics": dict(baseline_metrics or {}),
            "rollback_version": rollback_version or candidate.parent_version,
        }
        return ReleaseDecision.model_validate(
            self._gma.call("POST", _round_path(round_id, ":evaluate-release"), body)
        )


class GmaReleaseGuard:
    """The release Guard, run by the Dagents GMA's governance API.

    It implements what the consortium uses of ``GovernanceService`` at the
    release boundary: :meth:`enforce`, :meth:`audit_chain_intact` and
    :meth:`audit_record_count`. The GMA keeps the audit chain. The
    classification is registered with the GMA before the first decision.

    The Guard fails closed: when the GMA cannot be reached, :meth:`enforce`
    raises :class:`RemoteServiceUnavailable` and nothing is released.
    """

    def __init__(
        self,
        gma_url: str,
        classification: DataClassification,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._gma = _GmaEndpoint(GMA_NAME, gma_url, timeout=timeout, client=client)
        self._classification = classification
        self._registered = False
        self._lock = threading.Lock()

    def _ensure_classification(self) -> None:
        with self._lock:
            if self._registered:
                return
            path = f"/api/v1/governance/classifications/{quote(self._classification.classification_id, safe='')}"
            try:
                self._gma.call("PUT", path, self._classification.model_dump(mode="json"))
            except (KeyError, PermissionError) as error:
                raise RemoteServiceUnavailable(
                    f"{GMA_NAME} did not accept the data classification.", str(error)
                ) from error
            self._registered = True

    def enforce(
        self,
        request: RestrictionRequest,
        payload: list[dict[str, Any]] | None = None,
        correlation_id: str | None = None,
    ) -> GuardDecision:
        self._ensure_classification()
        body = {
            "request": request.model_dump(mode="json"),
            "payload": list(payload or []),
            "correlation_id": correlation_id,
        }
        try:
            answer = self._gma.call("POST", "/api/v1/governance/restrictions:enforce", body)
        except (KeyError, PermissionError) as error:
            raise RemoteServiceUnavailable(f"{GMA_NAME} did not run the Guard.", str(error)) from error
        return GuardDecision.model_validate(answer)

    def _audit(self, limit: int) -> dict[str, Any]:
        try:
            return dict(self._gma.call("GET", f"/api/v1/governance/audit?limit={limit}"))
        except (KeyError, PermissionError) as error:
            raise RemoteServiceUnavailable(f"{GMA_NAME} did not return its audit log.", str(error)) from error

    def audit_chain_intact(self) -> bool:
        return bool(self._audit(1)["chain_intact"])

    def audit_record_count(self) -> int:
        """How many decisions the GMA's chain holds. ``limit=0`` returns every record."""
        return len(self._audit(0)["records"])
