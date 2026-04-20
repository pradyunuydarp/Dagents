"""Workload planning and manifest contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel


class WorkloadEnvironmentVariable(DagentsModel):
    name: str
    value: str


class WorkloadPort(DagentsModel):
    name: str = "http"
    container_port: int


class WorkloadResources(DagentsModel):
    cpu_request: str = "250m"
    cpu_limit: str = "1"
    memory_request: str = "256Mi"
    memory_limit: str = "1Gi"


class WorkloadComponent(DagentsModel):
    name: str
    image: str
    kind: Literal["Deployment", "Job", "CronJob"] = "Deployment"
    replicas: int = Field(default=1, ge=1)
    schedule: str | None = None
    ports: list[WorkloadPort] = Field(default_factory=list)
    env: list[WorkloadEnvironmentVariable] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    resources: WorkloadResources = Field(default_factory=WorkloadResources)
    generated_resources: list[Literal["Service", "ConfigMap", "ServiceAccount"]] = Field(default_factory=list)
    service_account_name: str | None = None
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = "ClusterIP"
    config_map_data: dict[str, str] = Field(default_factory=dict)


class WorkloadSpec(DagentsModel):
    plan_id: str
    namespace: str = "dagents"
    components: list[WorkloadComponent] = Field(default_factory=list)
    include_services: bool = True
    include_config_maps: bool = False


class WorkloadManifest(DagentsModel):
    component_name: str
    kind: str
    deployment_yaml: str
    service_yaml: str | None = None
    config_map_yaml: str | None = None
    service_account_yaml: str | None = None


class WorkloadPlan(DagentsModel):
    plan_id: str
    namespace: str
    manifests: list[WorkloadManifest] = Field(default_factory=list)
    combined_yaml: str


class WorkloadManifestRequest(DagentsModel):
    namespace: str = "dagents"
    components: list[WorkloadComponent] = Field(default_factory=list)
    include_services: bool = True


class WorkloadManifestResponse(DagentsModel):
    namespace: str
    manifests: list[WorkloadManifest] = Field(default_factory=list)
    combined_yaml: str
