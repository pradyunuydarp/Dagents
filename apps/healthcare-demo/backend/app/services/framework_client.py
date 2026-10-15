"""Talking to a running Dagents deployment, and tracing records through the planners.

The pilot in :mod:`app.services.consortium` composes the framework in-process,
which is what makes the demo run with one command. This module covers two
other things:

- :meth:`FrameworkClient.service_status` checks which Dagents services answer
  over HTTP, as a production consumer would reach them. A service that does not
  answer is reported as ``unavailable``; the check never raises.
- :meth:`FrameworkClient.planner_trace` runs one hospital's records through the
  framework's planners step by step. It runs at the hospital, over the
  hospital's own records.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

from agents.common.infrastructure.dagents_runner import (
    evaluate_dataset_quality,
    plan_restrictions,
    run_dagentsc,
    validate_dataset_schema,
    validate_dataset_source,
)

from app.core.config import Settings
from app.services.remote import RemoteServiceUnavailable
from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FIELD_DOMAINS,
)
from app.models import ENTRY_REQUIRED_FIELDS

#: Seconds a service check is reused. The status endpoint is called on every
#: page load, and each check can wait for a timeout.
STATUS_CACHE_SECONDS = 15.0

#: Longest wait for core-service's health check.
CORE_HEALTH_TIMEOUT_SECONDS = 2.0

CORE_SERVICE_NAME = "The Dagents core service"


class CoreServiceClient:
    """The Dagents core service, which renders the Compose file of a hospital that joins.

    The app never writes Compose itself. It sends a workload spec to
    ``POST /api/v1/manifests/compose``, where the OCaml manifest compiler
    renders it, and applies the file it gets back.

    Params:
    - `base_url`: core-service, ``CORE_SERVICE_PUBLIC_URL``.
    - `timeout`: seconds to wait for a rendered file.
    - `client`: an ``httpx.Client`` to send through, for tests.
    """

    def __init__(self, base_url: str, *, timeout: float, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _request(self, method: str, path: str, *, timeout: float, json: Any = None) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            if self._client is not None:
                return self._client.request(method, url, json=json, timeout=timeout)
            with httpx.Client(timeout=timeout) as client:
                return client.request(method, url, json=json)
        except httpx.HTTPError as error:
            raise RemoteServiceUnavailable(
                f"{CORE_SERVICE_NAME} cannot be reached right now.", f"{method} {url}: {error}"
            ) from error

    def render_compose(self, request: dict[str, Any]) -> dict[str, Any]:
        """Render a Compose file: ``{services, issues, compose_yaml}``, as core-service answers.

        Any status but 200, or an answer that is not a JSON object, raises
        :class:`RemoteServiceUnavailable`. A file the planner refuses is a 200
        with ``issues`` and no ``compose_yaml``.
        """
        path = "/api/v1/manifests/compose"
        response = self._request("POST", path, timeout=self._timeout, json=request)
        if response.status_code != 200:
            raise RemoteServiceUnavailable(
                f"{CORE_SERVICE_NAME} could not render the Compose file.",
                f"POST {self.base_url}{path} answered {response.status_code}: {response.text[:200]}",
            )
        try:
            answer = response.json()
        except ValueError as error:
            raise RemoteServiceUnavailable(f"{CORE_SERVICE_NAME} sent an answer the app cannot read.", str(error)) from error
        if not isinstance(answer, dict):
            raise RemoteServiceUnavailable(f"{CORE_SERVICE_NAME} sent an answer the app cannot read.", repr(answer)[:200])
        return answer

    def health(self) -> bool:
        """Whether core-service answers its health check. Never raises."""
        try:
            response = self._request(
                "GET", "/api/v1/health", timeout=min(self._timeout, CORE_HEALTH_TIMEOUT_SECONDS)
            )
        except RemoteServiceUnavailable:
            return False
        return response.status_code == 200


class FrameworkClient:
    """Reaches the Dagents services and planners this app depends on."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._status_lock = threading.Lock()
        self._status: tuple[float, list[dict[str, str]]] | None = None

    def _services(self) -> list[tuple[str, str]]:
        return [
            ("core-service", f"{self._settings.core_service_url}/api/v1/health"),
            ("pipeline-service", f"{self._settings.pipeline_service_url}/api/v1/health"),
            ("model-service", f"{self._settings.model_service_url}/api/v1/health"),
            ("lma", f"{self._settings.lma_url}/api/v1/health"),
            ("gma", f"{self._settings.gma_url}/api/v1/health"),
        ]

    def service_status(self) -> list[dict[str, str]]:
        """Which framework services answer: ``[{name, status, detail}]``.

        ``status`` is ``ok`` or ``unavailable``. The checks run at the same
        time, and the result is reused for :data:`STATUS_CACHE_SECONDS`. The
        services' addresses are not returned.
        """
        with self._status_lock:
            if self._status is not None and time.monotonic() - self._status[0] < STATUS_CACHE_SECONDS:
                return [dict(entry) for entry in self._status[1]]
        timeout = self._settings.request_timeout_seconds

        def check(service: tuple[str, str]) -> dict[str, str]:
            name, url = service
            try:
                with httpx.Client(timeout=timeout) as client:
                    client.get(url).raise_for_status()
                return {"name": name, "status": "ok", "detail": "answered its health check"}
            except Exception as exc:  # noqa: BLE001 - the status is the output
                return {"name": name, "status": "unavailable", "detail": f"did not answer ({type(exc).__name__})"}

        services = self._services()
        with ThreadPoolExecutor(max_workers=len(services)) as pool:
            statuses = list(pool.map(check, services))
        with self._status_lock:
            self._status = (time.monotonic(), statuses)
        return [dict(entry) for entry in statuses]

    def planner_trace(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run records through the framework's planners, step by step.

        None of these steps knows what a stroke is. They validate a source,
        check records against the contract, apply quality rules, and decide
        what protection an egress needs, all from what the extension declared.

        Returns ``[{name, status, detail, payload}]``; ``status`` is ``ok`` or
        ``warning``, and a step that fails is reported, not raised.
        """
        trace: list[dict[str, Any]] = []
        source = {
            "source_id": "stroke-triage-site-records",
            "kind": "inline",
            "selection": {"records": records},
            "format": "rows",
            "batching": {"batch_size": 500, "max_records": max(len(records), 1)},
            "options": {"consumer": "healthcare-stroke-demo"},
        }

        self._step(trace, "Source spec validation", lambda: validate_dataset_source(source))
        self._step(
            trace,
            "Feature contract validation",
            lambda: validate_dataset_schema(records, STROKE_FEATURE_CONTRACT.as_schema_contract()),
        )
        self._step(
            trace,
            "Clinical data-quality rules",
            lambda: evaluate_dataset_quality(records, quality_rules()),
        )
        self._step(
            trace,
            "Pipeline DAG planning",
            lambda: run_dagentsc(
                ["pipeline", "compile", "--input", "-", "--output", "json"],
                {
                    "pipeline_id": "stroke-triage-flow",
                    "steps": [
                        {"step_id": "profile_cohort", "kind": "profile_dataset", "depends_on": []},
                        {"step_id": "score_worklist", "kind": "run_model_job", "depends_on": ["profile_cohort"]},
                        {"step_id": "project_basis", "kind": "project_fields", "depends_on": ["score_worklist"]},
                    ],
                },
            ),
        )
        self._step(
            trace,
            "Model route planning",
            lambda: run_dagentsc(["model", "route", "--task", "classification", "--output", "json"], {}),
        )
        self._step(
            trace,
            "Ethical-Restriction Rails (egress)",
            lambda: plan_restrictions(
                {
                    "request_id": "trace-egress",
                    "boundary": "before_send",
                    "requester": {
                        "requester_id": "stroke-consortium-gma",
                        "requester_kind": "coordinator",
                        "compliance_history": 0.9,
                        "attributes": [
                            {"attribute_id": "verified_identity", "weight": 1.0, "verified": True},
                            {"attribute_id": "signed_dua", "weight": 1.0, "verified": True},
                        ],
                    },
                    "classification": STROKE_CLASSIFICATION.model_dump(mode="json"),
                    "requested_fields": ["model_update"],
                    "granularity": "model_update",
                    "cohort_size": len(records),
                    "declared_purpose": "suspected_stroke",
                    "approved_purposes": list(STROKE_APPROVED_PURPOSES),
                }
            ),
        )
        return trace

    @staticmethod
    def _step(trace: list[dict[str, Any]], name: str, action: Callable[[], Any]) -> None:
        try:
            trace.append({"name": name, "status": "ok", "detail": "completed", "payload": action()})
        except Exception as exc:  # noqa: BLE001 - a failed step is a reportable result
            trace.append({"name": name, "status": "warning", "detail": str(exc), "payload": None})


def quality_rules() -> list[dict[str, Any]]:
    """The stroke records' quality rules, in the framework's generic vocabulary.

    The rules are stroke-specific; the evaluator is not. They are built from
    the field domains, so they cannot disagree with the entry form:

    - every required contract field must be present. A field every record has
      (see ``ENTRY_REQUIRED_FIELDS``) is an error when missing; an examination
      finding that may be left empty is a warning;
    - every number must lie in its domain's range.
    """
    rules: list[dict[str, Any]] = []
    for name in STROKE_FEATURE_CONTRACT.required_fields():
        always_present = name in ENTRY_REQUIRED_FIELDS or name == "age_band"
        rules.append(
            {
                "rule_id": f"{name}_present",
                "field": name,
                "operator": "non_null",
                "severity": "error" if always_present else "warning",
            }
        )
    for name in STROKE_FEATURE_CONTRACT.field_names():
        domain = STROKE_FIELD_DOMAINS.get(name)
        if domain is None:
            continue
        if domain.minimum is not None:
            rules.append(
                {
                    "rule_id": f"{name}_at_least_{domain.minimum:g}",
                    "field": name,
                    "operator": {"kind": "min_value", "value": domain.minimum},
                    "severity": "error",
                }
            )
        if domain.maximum is not None:
            rules.append(
                {
                    "rule_id": f"{name}_at_most_{domain.maximum:g}",
                    "field": name,
                    "operator": {"kind": "max_value", "value": domain.maximum},
                    "severity": "error",
                }
            )
    return rules
