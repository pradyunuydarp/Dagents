import sys
sys.path.append('services/core-service')

from app.models import WorkloadComponent, WorkloadManifestRequest
from app.services.manifest_service import manifest_service

components = [    WorkloadComponent(
        name="gma", image="dagents-gma:latest", ports=[{"name": "http", "container_port": 8020}],
        env=[{"name": "GMA_API_HOST", "value": "0.0.0.0"}, {"name": "GMA_API_PORT", "value": "8020"}]
    ),
    WorkloadComponent(
        name="lma", image="dagents-lma:latest", ports=[{"name": "http", "container_port": 8010}],
        env=[{"name": "LMA_API_HOST", "value": "0.0.0.0"}, {"name": "LMA_API_PORT", "value": "8010"}]
    ),
    WorkloadComponent(
        name="model-service", image="dagents-model-service:latest", ports=[{"name": "http", "container_port": 8000}],
        env=[{"name": "API_HOST", "value": "0.0.0.0"}, {"name": "API_PORT", "value": "8000"}, {"name": "APP_ENV", "value": "cloud"}]
    ),
    WorkloadComponent(
        name="pipeline-service", image="dagents-pipeline-service:latest", ports=[{"name": "http", "container_port": 8030}],
        env=[{"name": "API_HOST", "value": "0.0.0.0"}, {"name": "API_PORT", "value": "8030"}, {"name": "APP_ENV", "value": "cloud"}]
    ),
    WorkloadComponent(
        name="core-service", image="dagents-core-service:latest", ports=[{"name": "http", "container_port": 8040}],
        env=[{"name": "API_HOST", "value": "0.0.0.0"}, {"name": "API_PORT", "value": "8040"}, {"name": "APP_ENV", "value": "cloud"}]
    ),
    WorkloadComponent(
        name="spring-control-service", image="dagents-spring-control-service:latest", ports=[{"name": "http", "container_port": 8050}],
        env=[{"name": "SERVER_PORT", "value": "8050"}, {"name": "DAGENTS_CONTROL_ENVIRONMENT", "value": "cloud"}]
    ),
    WorkloadComponent(
        name="spring-core-service", image="dagents-spring-core-service:latest", ports=[{"name": "http", "container_port": 8060}],
        env=[{"name": "SERVER_PORT", "value": "8060"}, {"name": "DAGENTS_CORE_ENVIRONMENT", "value": "cloud"}]
    )
]

req = WorkloadManifestRequest(namespace="dagents-dev", components=components, include_services=True)
resp = manifest_service.generate(req)

with open('dagents-workloads.yaml', 'w') as f:
    f.write(resp.combined_yaml)

print("Generated dagents-workloads.yaml locally bypassing docker!")
