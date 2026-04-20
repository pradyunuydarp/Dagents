"""Pydantic API models for the core service."""

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    transport: str


class ServiceDescriptor(BaseModel):
    name: str
    kind: str
    base_url: str


class ServiceCatalogResponse(BaseModel):
    services: list[ServiceDescriptor]


class TopologyResponse(BaseModel):
    framework: str
    services: list[ServiceDescriptor]


class WorkloadEnvironmentVariable(BaseModel):
    name: str
    value: str


class WorkloadPort(BaseModel):
    name: str = "http"
    container_port: int


class WorkloadResources(BaseModel):
    cpu_request: str = "250m"
    cpu_limit: str = "1"
    memory_request: str = "256Mi"
    memory_limit: str = "1Gi"


class WorkloadComponent(BaseModel):
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


class WorkloadManifestRequest(BaseModel):
    namespace: str = "dagents"
    components: list[WorkloadComponent] = Field(default_factory=list)
    include_services: bool = True


class WorkloadManifest(BaseModel):
    component_name: str
    kind: str = "Deployment"
    deployment_yaml: str
    service_yaml: str | None = None
    config_map_yaml: str | None = None
    service_account_yaml: str | None = None


class WorkloadManifestResponse(BaseModel):
    namespace: str
    manifests: list[WorkloadManifest] = Field(default_factory=list)
    combined_yaml: str


class WorkloadCompileRequest(BaseModel):
    plan_id: str | None = None
    namespace: str = "dagents"
    components: list[WorkloadComponent] = Field(default_factory=list)
    include_services: bool = True
    include_config_maps: bool = False


class WorkloadPlanResponse(BaseModel):
    plan_id: str
    namespace: str
    manifests: list[WorkloadManifest] = Field(default_factory=list)
    combined_yaml: str
