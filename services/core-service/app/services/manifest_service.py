"""Kubernetes manifest generation for Dagents workloads."""

from __future__ import annotations

import time
from typing import Protocol

from app.models import (
    WorkloadCompileRequest,
    WorkloadComponent,
    WorkloadManifest,
    WorkloadManifestRequest,
    WorkloadManifestResponse,
    WorkloadPlanResponse,
)


class WorkloadPlanRepository(Protocol):
    def save(self, plan: WorkloadPlanResponse) -> WorkloadPlanResponse:
        """Persist one workload plan."""

    def get(self, plan_id: str) -> WorkloadPlanResponse | None:
        """Load one workload plan."""


class InMemoryWorkloadPlanRepository:
    def __init__(self) -> None:
        self._plans: dict[str, WorkloadPlanResponse] = {}

    def save(self, plan: WorkloadPlanResponse) -> WorkloadPlanResponse:
        self._plans[plan.plan_id] = plan
        return plan

    def get(self, plan_id: str) -> WorkloadPlanResponse | None:
        return self._plans.get(plan_id)


class ManifestService:
    def __init__(self, plans: WorkloadPlanRepository | None = None) -> None:
        self._plans = plans or InMemoryWorkloadPlanRepository()

    def generate(self, request: WorkloadManifestRequest) -> WorkloadManifestResponse:
        compiled = self.compile(
            WorkloadCompileRequest(
                namespace=request.namespace,
                components=request.components,
                include_services=request.include_services,
            )
        )
        return WorkloadManifestResponse(
            namespace=compiled.namespace,
            manifests=compiled.manifests,
            combined_yaml=compiled.combined_yaml,
        )

    def compile(self, request: WorkloadCompileRequest) -> WorkloadPlanResponse:
        manifests: list[WorkloadManifest] = []
        rendered_sections: list[str] = []
        plan_id = request.plan_id or f"workload-plan-{int(time.time() * 1000)}"

        for component in request.components:
            deployment_yaml = self._workload_yaml(request.namespace, component)
            service_yaml = self._service_yaml(request.namespace, component) if request.include_services else None
            config_map_yaml = self._config_map_yaml(request.namespace, component) if request.include_config_maps else None
            manifests.append(
                WorkloadManifest(
                    component_name=component.name,
                    kind=component.kind,
                    deployment_yaml=deployment_yaml,
                    service_yaml=service_yaml,
                    config_map_yaml=config_map_yaml,
                )
            )
            rendered_sections.append(deployment_yaml)
            if service_yaml:
                rendered_sections.append(service_yaml)
            if config_map_yaml:
                rendered_sections.append(config_map_yaml)

        plan = WorkloadPlanResponse(
            plan_id=plan_id,
            namespace=request.namespace,
            manifests=manifests,
            combined_yaml="\n---\n".join(rendered_sections),
        )
        return self._plans.save(plan)

    def get_plan(self, plan_id: str) -> WorkloadPlanResponse | None:
        return self._plans.get(plan_id)

    def _workload_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        if component.kind == "Job":
            return self._job_yaml(namespace, component)
        if component.kind == "CronJob":
            return self._cron_job_yaml(namespace, component)
        return self._deployment_yaml(namespace, component)

    def _deployment_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        env_lines = self._env_lines(component)
        port_lines = self._port_lines(component)
        arg_lines = self._arg_lines(component)
        return "\n".join(
            [
                "apiVersion: apps/v1",
                "kind: Deployment",
                "metadata:",
                f"  name: {component.name}",
                f"  namespace: {namespace}",
                "spec:",
                f"  replicas: {component.replicas}",
                "  selector:",
                "    matchLabels:",
                f"      app: {component.name}",
                "  template:",
                "    metadata:",
                "      labels:",
                f"        app: {component.name}",
                "    spec:",
                "      containers:",
                "      - name: main",
                f"        image: {component.image}",
                *arg_lines,
                *port_lines,
                *env_lines,
                "        resources:",
                "          requests:",
                f"            cpu: {component.resources.cpu_request}",
                f"            memory: {component.resources.memory_request}",
                "          limits:",
                f"            cpu: {component.resources.cpu_limit}",
                f"            memory: {component.resources.memory_limit}",
            ]
        )

    def _job_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        env_lines = self._env_lines(component)
        arg_lines = self._arg_lines(component)
        return "\n".join(
            [
                "apiVersion: batch/v1",
                "kind: Job",
                "metadata:",
                f"  name: {component.name}",
                f"  namespace: {namespace}",
                "spec:",
                "  template:",
                "    metadata:",
                "      labels:",
                f"        app: {component.name}",
                "    spec:",
                "      restartPolicy: Never",
                "      containers:",
                "      - name: main",
                f"        image: {component.image}",
                *arg_lines,
                *env_lines,
                "        resources:",
                "          requests:",
                f"            cpu: {component.resources.cpu_request}",
                f"            memory: {component.resources.memory_request}",
                "          limits:",
                f"            cpu: {component.resources.cpu_limit}",
                f"            memory: {component.resources.memory_limit}",
            ]
        )

    def _cron_job_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        env_lines = self._env_lines(component)
        arg_lines = self._arg_lines(component)
        schedule = component.schedule or "0 * * * *"
        return "\n".join(
            [
                "apiVersion: batch/v1",
                "kind: CronJob",
                "metadata:",
                f"  name: {component.name}",
                f"  namespace: {namespace}",
                "spec:",
                f"  schedule: \"{schedule}\"",
                "  jobTemplate:",
                "    spec:",
                "      template:",
                "        metadata:",
                "          labels:",
                f"            app: {component.name}",
                "        spec:",
                "          restartPolicy: Never",
                "          containers:",
                "          - name: main",
                f"            image: {component.image}",
                *[line.replace("        ", "            ") for line in arg_lines],
                *[line.replace("        ", "            ") for line in env_lines],
                "            resources:",
                "              requests:",
                f"                cpu: {component.resources.cpu_request}",
                f"                memory: {component.resources.memory_request}",
                "              limits:",
                f"                cpu: {component.resources.cpu_limit}",
                f"                memory: {component.resources.memory_limit}",
            ]
        )

    def _service_yaml(self, namespace: str, component: WorkloadComponent) -> str | None:
        if not component.ports:
            return None
        lines = [
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {component.name}",
            f"  namespace: {namespace}",
            "spec:",
            "  selector:",
            f"    app: {component.name}",
            "  ports:",
        ]
        for port in component.ports:
            lines.extend(
                [
                    f"  - name: {port.name}",
                    f"    port: {port.container_port}",
                    f"    targetPort: {port.container_port}",
                ]
            )
        return "\n".join(lines)

    def _config_map_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        return "\n".join(
            [
                "apiVersion: v1",
                "kind: ConfigMap",
                "metadata:",
                f"  name: {component.name}-config",
                f"  namespace: {namespace}",
                "data:",
                f"  component-kind: \"{component.kind}\"",
            ]
        )

    def _env_lines(self, component: WorkloadComponent) -> list[str]:
        if not component.env:
            return []
        lines = ["        env:"]
        for env in component.env:
            lines.extend([f"        - name: {env.name}", f"          value: \"{env.value}\""])
        return lines

    def _port_lines(self, component: WorkloadComponent) -> list[str]:
        if not component.ports:
            return []
        lines = ["        ports:"]
        for port in component.ports:
            lines.extend([f"        - name: {port.name}", f"          containerPort: {port.container_port}"])
        return lines

    def _arg_lines(self, component: WorkloadComponent) -> list[str]:
        if not component.args:
            return []
        lines = ["        args:"]
        for arg in component.args:
            lines.append(f"        - \"{arg}\"")
        return lines


manifest_service = ManifestService()
