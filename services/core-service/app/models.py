"""Pydantic API models for the core service.

The field descriptions appear in the service's OpenAPI document (`/docs`) and
in the API reference on the framework site, which is generated from it.
"""

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(description="Always `ok` when the service answers.")
    service: str = Field(description="The service name, from `APP_NAME`. The default is `dagents-core-service`.")
    environment: str = Field(description="The deployment environment, from `APP_ENV`. The default is `development`.")
    transport: str = Field(description="How the service is reached. Always `http`.")


class ErrorResponse(BaseModel):
    detail: str = Field(description="What went wrong, in one sentence.")


class ServiceDescriptor(BaseModel):
    name: str = Field(description="The service: `lma`, `gma`, `model-service` or `pipeline-service`.")
    kind: str = Field(description="`agent` for the LMA and GMA, `service` for the framework services.")
    base_url: str = Field(
        description=(
            "The address the core service is configured with for this service, from `LMA_URL`, `GMA_URL`, "
            "`MODEL_SERVICE_URL` or `PIPELINE_SERVICE_URL`. The core service does not check that it answers."
        )
    )


class ServiceCatalogResponse(BaseModel):
    services: list[ServiceDescriptor] = Field(
        description="The four services, always in this order: lma, gma, model-service, pipeline-service."
    )


class TopologyResponse(BaseModel):
    framework: str = Field(description="Always `dagents`.")
    services: list[ServiceDescriptor] = Field(description="The same list `GET /api/v1/services` returns.")


class WorkloadEnvironmentVariable(BaseModel):
    name: str = Field(description="The variable's name.")
    value: str = Field(description="Its value. The YAML quotes it, so a number stays a string.")


class WorkloadPort(BaseModel):
    name: str = Field(default="http", description="The port's name in the container and in the Service.")
    container_port: int = Field(
        description="The port the container listens on. A generated Service exposes the same number."
    )


class WorkloadResources(BaseModel):
    cpu_request: str = Field(default="250m", description="CPU the scheduler reserves for the container.")
    cpu_limit: str = Field(default="1", description="Most CPU the container may use.")
    memory_request: str = Field(default="256Mi", description="Memory the scheduler reserves for the container.")
    memory_limit: str = Field(default="1Gi", description="Most memory the container may use.")


class WorkloadComponent(BaseModel):
    name: str = Field(
        description="The workload's name. It is the Kubernetes object name and the `app` label its pods carry."
    )
    image: str = Field(description="The container image to run.")
    kind: Literal["Deployment", "Job", "CronJob"] = Field(
        default="Deployment",
        description=(
            "The main Kubernetes object: `Deployment` keeps pods running, `Job` runs once, `CronJob` runs on a "
            "schedule."
        ),
    )
    replicas: int = Field(
        default=1, ge=1, description="Number of pods for a `Deployment`. A `Job` or `CronJob` ignores it."
    )
    schedule: str | None = Field(
        default=None,
        description="Cron schedule for a `CronJob`. Empty means `0 * * * *`, once an hour. Other kinds ignore it.",
    )
    ports: list[WorkloadPort] = Field(
        default_factory=list, description="Ports the container listens on. A component without ports gets no Service."
    )
    env: list[WorkloadEnvironmentVariable] = Field(
        default_factory=list, description="Environment variables for the container."
    )
    args: list[str] = Field(default_factory=list, description="Arguments passed to the container's entry point.")
    resources: WorkloadResources = Field(
        default_factory=WorkloadResources, description="CPU and memory requests and limits."
    )
    generated_resources: list[Literal["Service", "ConfigMap", "ServiceAccount"]] = Field(
        default_factory=list,
        description=(
            "Extra objects this component gets even when the request-wide flags are off. `ServiceAccount` is only "
            "generated this way."
        ),
    )
    service_account_name: str | None = Field(
        default=None,
        description=(
            "The service account the pods run as. When it is empty and `generated_resources` lists "
            "`ServiceAccount`, the component's name is used."
        ),
    )
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = Field(
        default="ClusterIP", description="The type of the generated Service."
    )
    config_map_data: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Entries for the generated ConfigMap. Empty means two entries: `component-kind` and `image`."
        ),
    )


class WorkloadManifestRequest(BaseModel):
    namespace: str = Field(default="dagents", description="The Kubernetes namespace every object is placed in.")
    components: list[WorkloadComponent] = Field(default_factory=list, description="The workloads to render.")
    include_services: bool = Field(default=True, description="Give every component that has ports a Service.")


class WorkloadManifest(BaseModel):
    component_name: str = Field(description="The component these objects were rendered for.")
    kind: str = Field(default="Deployment", description="The kind of the main object: `Deployment`, `Job` or `CronJob`.")
    deployment_yaml: str = Field(
        description="The main object as YAML. Despite the name, it holds a Job or CronJob when `kind` says so."
    )
    service_yaml: str | None = Field(default=None, description="The Service, or null when none was generated.")
    config_map_yaml: str | None = Field(default=None, description="The ConfigMap, or null when none was generated.")
    service_account_yaml: str | None = Field(
        default=None, description="The ServiceAccount, or null when none was generated."
    )


class WorkloadManifestResponse(BaseModel):
    namespace: str = Field(description="The namespace from the request.")
    manifests: list[WorkloadManifest] = Field(
        default_factory=list, description="One entry per component, in request order."
    )
    combined_yaml: str = Field(
        description="Every object in one YAML document, separated by `---`, ready for `kubectl apply -f`."
    )


class WorkloadCompileRequest(BaseModel):
    plan_id: str | None = Field(
        default=None,
        description=(
            "The id to store the plan under. Empty means the service makes one from the clock: "
            "`workload-plan-<milliseconds since 1970>`. A plan with the same id replaces the stored one."
        ),
    )
    namespace: str = Field(default="dagents", description="The Kubernetes namespace every object is placed in.")
    components: list[WorkloadComponent] = Field(default_factory=list, description="The workloads to render.")
    include_services: bool = Field(default=True, description="Give every component that has ports a Service.")
    include_config_maps: bool = Field(default=False, description="Give every component a ConfigMap.")


class WorkloadPlanResponse(BaseModel):
    plan_id: str = Field(description="The id the plan is stored under. Fetch it again with this id.")
    namespace: str = Field(description="The namespace from the request.")
    manifests: list[WorkloadManifest] = Field(
        default_factory=list, description="One entry per component, in request order."
    )
    combined_yaml: str = Field(
        description="Every object in one YAML document, separated by `---`, ready for `kubectl apply -f`."
    )
