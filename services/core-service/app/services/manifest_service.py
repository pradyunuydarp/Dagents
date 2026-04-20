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
    """Store compiled workload plans in memory for development and tests."""

    def __init__(self) -> None:
        self._plans: dict[str, WorkloadPlanResponse] = {}

    def save(self, plan: WorkloadPlanResponse) -> WorkloadPlanResponse:
        """Persist one plan and return it unchanged."""
        self._plans[plan.plan_id] = plan
        return plan

    def get(self, plan_id: str) -> WorkloadPlanResponse | None:
        """Load one plan by id if it exists."""
        return self._plans.get(plan_id)


class ManifestService:
    """Compile workload requests into concrete Kubernetes manifest bundles.

    Params:
    - `plans`: repository used to persist compiled workload plans for later lookup.

    What it does:
    - Converts high-level workload requests into rendered YAML strings.
    - Persists compiled plans so callers can retrieve them later by `plan_id`.

    Returns:
    - The service instance is used through `generate`, `compile`, and `get_plan`.
    """

    def __init__(self, plans: WorkloadPlanRepository | None = None) -> None:
        self._plans = plans or InMemoryWorkloadPlanRepository()

    def generate(self, request: WorkloadManifestRequest) -> WorkloadManifestResponse:
        """Render a one-shot manifest response without exposing plan lookup.

        Params:
        - `request`: high-level manifest request with namespace, components, and
          global generation flags.

        What it does:
        - Delegates to `compile` so both endpoints share the same rendering logic.
        - Drops the `plan_id` and returns only the manifest payload.

        Returns:
        - `WorkloadManifestResponse`.
        """
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
        """Compile a workload request into a persisted plan.

        Params:
        - `request`: full workload compilation request, including optional
          component-level generated resources.

        What it does:
        - Generates the primary workload YAML for each component.
        - Optionally adds generated `Service`, `ConfigMap`, and `ServiceAccount`
          resources.
        - Concatenates all rendered sections into `combined_yaml`.
        - Persists the result in the configured plan repository.

        Returns:
        - `WorkloadPlanResponse`.
        """
        manifests: list[WorkloadManifest] = []
        rendered_sections: list[str] = []
        plan_id = request.plan_id or f"workload-plan-{int(time.time() * 1000)}"

        for component in request.components:
            # Each component yields one primary workload plus zero or more
            # generated companion resources.
            deployment_yaml = self._workload_yaml(request.namespace, component)
            service_yaml = self._service_yaml(request.namespace, component) if self._generate_service(component, request.include_services) else None
            config_map_yaml = (
                self._config_map_yaml(request.namespace, component)
                if self._generate_config_map(component, request.include_config_maps)
                else None
            )
            service_account_yaml = (
                self._service_account_yaml(request.namespace, component)
                if self._generate_service_account(component)
                else None
            )
            manifests.append(
                WorkloadManifest(
                    component_name=component.name,
                    kind=component.kind,
                    deployment_yaml=deployment_yaml,
                    service_yaml=service_yaml,
                    config_map_yaml=config_map_yaml,
                    service_account_yaml=service_account_yaml,
                )
            )
            rendered_sections.append(deployment_yaml)
            if service_yaml:
                rendered_sections.append(service_yaml)
            if config_map_yaml:
                rendered_sections.append(config_map_yaml)
            if service_account_yaml:
                rendered_sections.append(service_account_yaml)

        plan = WorkloadPlanResponse(
            plan_id=plan_id,
            namespace=request.namespace,
            manifests=manifests,
            combined_yaml="\n---\n".join(rendered_sections),
        )
        return self._plans.save(plan)

    def get_plan(self, plan_id: str) -> WorkloadPlanResponse | None:
        """Return a previously compiled workload plan by id."""
        return self._plans.get(plan_id)

    def _workload_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        """Dispatch to the correct workload renderer based on `component.kind`."""
        if component.kind == "Job":
            return self._job_yaml(namespace, component)
        if component.kind == "CronJob":
            return self._cron_job_yaml(namespace, component)
        return self._deployment_yaml(namespace, component)

    def _deployment_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        """Render one `Deployment` manifest for a workload component."""
        env_lines = self._env_lines(component)
        port_lines = self._port_lines(component)
        arg_lines = self._arg_lines(component)
        service_account_lines = self._service_account_name_lines(component, indent="      ")
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
                *service_account_lines,
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
        """Render one batch `Job` manifest for a workload component."""
        env_lines = self._env_lines(component)
        arg_lines = self._arg_lines(component)
        service_account_lines = self._service_account_name_lines(component, indent="      ")
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
                *service_account_lines,
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
        """Render one `CronJob` manifest for a scheduled workload component."""
        env_lines = self._env_lines(component)
        arg_lines = self._arg_lines(component)
        service_account_lines = self._service_account_name_lines(component, indent="          ")
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
                *service_account_lines,
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
        """Render a companion `Service` when the component exposes ports.

        Params:
        - `namespace`: Kubernetes namespace for the generated resource.
        - `component`: workload definition containing port and service settings.

        What it does:
        - Skips generation when no ports are defined.
        - Uses `component.service_type` to control the service exposure mode.

        Returns:
        - YAML string for a `Service`, or `None`.
        """
        if not component.ports:
            return None
        lines = [
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {component.name}",
            f"  namespace: {namespace}",
            "spec:",
            f"  type: {component.service_type}",
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
        """Render a companion `ConfigMap` for component metadata or custom data."""
        data = component.config_map_data or {
            "component-kind": component.kind,
            "image": component.image,
        }
        return "\n".join(
            [
                "apiVersion: v1",
                "kind: ConfigMap",
                "metadata:",
                f"  name: {component.name}-config",
                f"  namespace: {namespace}",
                "data:",
                *[f"  {key}: \"{value}\"" for key, value in data.items()],
            ]
        )

    def _service_account_yaml(self, namespace: str, component: WorkloadComponent) -> str:
        """Render a companion `ServiceAccount` for one workload component."""
        return "\n".join(
            [
                "apiVersion: v1",
                "kind: ServiceAccount",
                "metadata:",
                f"  name: {self._service_account_name(component)}",
                f"  namespace: {namespace}",
            ]
        )

    def _service_account_name_lines(self, component: WorkloadComponent, *, indent: str) -> list[str]:
        """Return the pod-spec line that binds a workload to a service account."""
        service_account_name = self._service_account_name(component)
        if not service_account_name:
            return []
        return [f"{indent}serviceAccountName: {service_account_name}"]

    def _service_account_name(self, component: WorkloadComponent) -> str | None:
        """Resolve the effective service account name for one component."""
        if component.service_account_name:
            return component.service_account_name
        if "ServiceAccount" in component.generated_resources:
            return component.name
        return None

    def _generate_service(self, component: WorkloadComponent, include_services: bool) -> bool:
        """Decide whether to generate a companion `Service` resource."""
        return "Service" in component.generated_resources or include_services

    def _generate_config_map(self, component: WorkloadComponent, include_config_maps: bool) -> bool:
        """Decide whether to generate a companion `ConfigMap` resource."""
        return "ConfigMap" in component.generated_resources or include_config_maps

    def _generate_service_account(self, component: WorkloadComponent) -> bool:
        """Decide whether to generate a companion `ServiceAccount` resource."""
        return self._service_account_name(component) is not None

    def _env_lines(self, component: WorkloadComponent) -> list[str]:
        """Render environment variables into container YAML lines."""
        if not component.env:
            return []
        lines = ["        env:"]
        for env in component.env:
            lines.extend([f"        - name: {env.name}", f"          value: \"{env.value}\""])
        return lines

    def _port_lines(self, component: WorkloadComponent) -> list[str]:
        """Render container ports into YAML lines for pod specs."""
        if not component.ports:
            return []
        lines = ["        ports:"]
        for port in component.ports:
            lines.extend([f"        - name: {port.name}", f"          containerPort: {port.container_port}"])
        return lines

    def _arg_lines(self, component: WorkloadComponent) -> list[str]:
        """Render container args into YAML lines for pod specs."""
        if not component.args:
            return []
        lines = ["        args:"]
        for arg in component.args:
            lines.append(f"        - \"{arg}\"")
        return lines


manifest_service = ManifestService()
