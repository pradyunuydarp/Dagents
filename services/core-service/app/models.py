"""Pydantic API models for the core service.

The field descriptions appear in the service's OpenAPI document (`/docs`) and
in the API reference on the framework site, which is generated from it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


# Docker Compose files.
#
# These models check the shape of a request only: types, the allowed words and
# the fields that must be present. Every rule about values (a valid service
# name, a port from 1 to 65535, a dependency that exists) is decided by the
# planner, which reports a broken rule as an issue in a 200 response. Checking a
# value here as well would make a second copy of a planner rule.


class ComposeEnvironmentVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "The variable's name: a letter or `_`, then letters, digits or `_`. The planner reports any other "
            "name as an issue."
        )
    )
    value: str = Field(
        description=(
            "Its value. The file quotes it and writes each `$` as `$$`, so Docker Compose passes the value to the "
            "container unchanged."
        )
    )


class ComposePort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_port: int = Field(description="The port the container listens on, from 1 to 65535.")
    host_port: int | None = Field(
        default=None,
        description=(
            "The port on the host that forwards to `container_port`, written under `ports`. Null writes the port "
            "under `expose` instead: it is not published on the host, and the other services on the network "
            "reach it."
        ),
    )
    host_ip: str = Field(
        default="127.0.0.1",
        description=(
            "The IPv4 address on the host that `host_port` listens on. The default accepts connections from this "
            "machine only, because the LMA and the GMA have no authentication. Read only when `host_port` is set."
        ),
    )


class ComposeHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str] | None = Field(
        default=None,
        description=(
            "The check as a program and its arguments, run without a shell (`CMD`). Give this or `shell`, not both."
        ),
    )
    shell: str | None = Field(
        default=None,
        description=(
            "The check as one command line, run by the container's shell (`CMD-SHELL`). Give this or `command`, "
            "not both."
        ),
    )
    interval_seconds: int = Field(
        default=30,
        description="Seconds between two checks. From 1 to 9223372036, the longest duration Docker Compose reads.",
    )
    timeout_seconds: int = Field(
        default=5,
        description="Seconds one check may take before it counts as failed. From 1 to 9223372036.",
    )
    retries: int = Field(
        default=3, description="Failed checks in a row after which the container is unhealthy. At least 1."
    )
    start_period_seconds: int = Field(
        default=10,
        description=(
            "Seconds after the container starts during which a failed check does not count. From 0 to 9223372036."
        ),
    )

    @model_validator(mode="after")
    def exactly_one_test(self) -> "ComposeHealthCheck":
        """Refuse a health check with both or neither of `command` and `shell`."""
        if (self.command is None) == (self.shell is None):
            raise ValueError("give exactly one of command and shell")
        return self


class ComposeDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(
        description=(
            "A service this one waits for: another component in the request or, when `placement` is `merge`, one "
            "of `existing_services`."
        )
    )
    condition: Literal["service_started", "service_healthy", "service_completed_successfully"] = Field(
        default="service_started",
        description=(
            "What Docker Compose waits for: `service_started` (the container has started), `service_healthy` "
            "(its health check passes) or `service_completed_successfully` (it has exited with status 0, as a "
            "`Job` does)."
        ),
    )


class ComposeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "The Compose service name: lower-case letters, digits, `-`, `_` and `.`, starting with a letter or a "
            "digit."
        )
    )
    image: str = Field(description="The container image to run.")
    kind: Literal["Deployment", "Job"] = Field(
        default="Deployment",
        description=(
            "`Deployment` keeps the container running (`restart: unless-stopped`). `Job` runs it once "
            "(`restart: \"no\"`)."
        ),
    )
    args: list[str] = Field(
        default_factory=list,
        description=(
            "The command that replaces the image's default command, written as `command`. Empty keeps the image's "
            "command."
        ),
    )
    env_files: list[str] = Field(
        default_factory=list,
        description=(
            "Files of `NAME=value` lines that Docker Compose reads into the container's environment, written as "
            "`env_file`. A relative path is relative to the directory of the first Compose file. Put secrets here, "
            "so the rendered file does not contain them."
        ),
    )
    env: list[ComposeEnvironmentVariable] = Field(
        default_factory=list,
        description="Environment variables written into the file. A name here wins over the same name in `env_files`.",
    )
    ports: list[ComposePort] = Field(
        default_factory=list,
        description="Ports the container listens on. Only a port with a `host_port` is published on the host.",
    )
    health_check: ComposeHealthCheck | None = Field(
        default=None,
        description=(
            "How Docker Compose checks that the container is healthy. Null keeps the image's own `HEALTHCHECK`."
        ),
    )
    depends_on: list[ComposeDependency] = Field(
        default_factory=list,
        description="The services that must reach a condition before this one starts.",
    )


class ComposeManifestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement: Literal["own", "merge"] = Field(
        default="own",
        description=(
            "`own`: the file is a whole Compose project. `merge`: the file is passed with `-f` after the files of "
            "the project that runs `existing_services`, so its services can depend on them."
        ),
    )
    network: str = Field(
        default="default",
        description=(
            "The network every service joins. `default` is the project's default network. The name follows the "
            "rule for service names."
        ),
    )
    external_network: bool = Field(
        default=False,
        description=(
            "True when `network` exists already and the file only joins it; give its full Docker name, such as "
            "`healthcare-demo_default`. False declares the network in the file."
        ),
    )
    existing_services: list[str] = Field(
        default_factory=list,
        description=(
            "The services the project already runs. A component may not take one of these names. With `merge`, a "
            "component may depend on them; with `own`, such a dependency is an issue."
        ),
    )
    components: list[ComposeComponent] = Field(
        default_factory=list, description="The services to render, in the order the file lists them."
    )


# Every reason the planner gives for refusing a Compose file: the codes of
# `compose_issue_code_and_value` in bindings/ocaml/lib/common_ir/dagents_common_ir.ml,
# in the same order. services/core-service/tests/test_compose_manifest.py
# compares the two lists.
ComposeIssueCode = Literal[
    "no_services",
    "invalid_network_name",
    "invalid_service_name",
    "duplicate_service_name",
    "service_name_in_use",
    "empty_image",
    "no_compose_equivalent",
    "unsupported_replicas",
    "invalid_environment_name",
    "duplicate_environment_name",
    "invalid_port",
    "invalid_host_ip",
    "host_port_conflict",
    "empty_health_check",
    "invalid_health_check_timing",
    "self_dependency",
    "duplicate_dependency",
    "dependency_outside_project",
    "unknown_dependency",
    "dependency_cycle",
]


class ComposeIssue(BaseModel):
    service: str | None = Field(
        description="The component the issue is about, or null for an issue with the whole request."
    )
    code: ComposeIssueCode = Field(
        description="What is wrong. The `dagentsc manifest compile` reference explains each code."
    )
    value: str | None = Field(
        description="The name or number the issue is about, as a string, or null when the code needs none."
    )


class ComposeManifestResponse(BaseModel):
    services: list[str] = Field(
        description="The name of every component, in request order, including the ones with issues."
    )
    issues: list[ComposeIssue] = Field(
        description=(
            "Every reason the request cannot become a Compose file, in a fixed order. Empty when the file is "
            "rendered."
        )
    )
    compose_yaml: str | None = Field(
        description=(
            "The Compose file, with no newline at the end, or null when `issues` is not empty. Docker Compose "
            "reads it with `-f`."
        )
    )
