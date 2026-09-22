"""Talking to a running Dagents deployment over HTTP.

The pilot in :mod:`app.services.consortium` composes the framework in-process,
which is what makes the demo runnable with one command. This module is the other
integration shape: the same app against Dagents services running as real
services, which is how a production consumer would use them.

Every call degrades to a reported "unavailable" rather than raising. A demo that
falls over because an optional service is not running teaches nothing, and the
status itself is the useful output.
"""

from __future__ import annotations

from typing import Any

import httpx

from agents.common.infrastructure.dagents_runner import (
    evaluate_dataset_quality,
    plan_restrictions,
    run_dagentsc,
    validate_dataset_schema,
    validate_dataset_source,
)

from app.core.config import Settings
from app.domain.conditions import STROKE_CLASSIFICATION, STROKE_FEATURE_CONTRACT


class FrameworkClient:
    """Reaches the Dagents services and planners this app depends on."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def service_status(self) -> list[dict[str, str]]:
        """Report which framework services are reachable right now."""
        services = [
            ("core-service", f"{self._settings.core_service_url}/api/v1/health"),
            ("pipeline-service", f"{self._settings.pipeline_service_url}/api/v1/health"),
            ("model-service", f"{self._settings.model_service_url}/api/v1/health"),
            ("lma", f"{self._settings.lma_url}/api/v1/health"),
            ("gma", f"{self._settings.gma_url}/api/v1/health"),
        ]
        statuses: list[dict[str, str]] = []
        with httpx.Client(timeout=self._settings.request_timeout_seconds) as client:
            for name, url in services:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    statuses.append({"name": name, "url": url, "status": "ok", "detail": "reachable"})
                except Exception as exc:  # noqa: BLE001 - the status is the output
                    statuses.append({"name": name, "url": url, "status": "unavailable", "detail": str(exc)})
        return statuses

    def planner_trace(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run stroke records through the framework's planners, step by step.

        This is the part worth watching: none of these steps know anything about
        stroke. They validate a source, check records against a contract, apply
        quality rules, and decide what protection an egress needs, all from the
        declarations the extension supplied.
        """
        trace: list[dict[str, Any]] = []
        source = {
            "source_id": "stroke-triage-cohort",
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
            lambda: evaluate_dataset_quality(records, self._quality_rules()),
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
                    "approved_purposes": ["suspected_stroke", "stroke_triage_research"],
                }
            ),
        )
        return trace

    @staticmethod
    def _quality_rules() -> list[dict[str, Any]]:
        """Clinical quality rules, expressed in the framework's generic vocabulary.

        The rules are stroke-specific; the evaluator is not. That split is the
        whole point: the app says an NIHSS total must sit between 0 and 42, and
        the framework decides whether the batch is blocking without knowing what
        NIHSS is.
        """
        return [
            {"rule_id": "nihss_present", "field": "nihss_total", "operator": "non_null", "severity": "error"},
            {
                "rule_id": "nihss_in_range_low",
                "field": "nihss_total",
                "operator": {"kind": "min_value", "value": 0},
                "severity": "error",
            },
            {
                "rule_id": "nihss_in_range_high",
                "field": "nihss_total",
                "operator": {"kind": "max_value", "value": 42},
                "severity": "error",
            },
            {
                "rule_id": "last_known_well_present",
                "field": "last_known_well_minutes",
                "operator": "non_null",
                "severity": "error",
            },
            {
                "rule_id": "age_band_present",
                "field": "age_band",
                "operator": "non_null",
                "severity": "warning",
            },
        ]

    @staticmethod
    def _step(trace: list[dict[str, Any]], name: str, action) -> None:
        try:
            trace.append({"name": name, "status": "ok", "detail": "completed", "payload": action()})
        except Exception as exc:  # noqa: BLE001 - a failed step is a reportable result
            trace.append({"name": name, "status": "warning", "detail": str(exc), "payload": None})
