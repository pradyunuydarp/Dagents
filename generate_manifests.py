import json
import urllib.request

components = [
    {
        "name": "gma",
        "image": "dagents-gma:latest",
        "ports": [{"name": "http", "container_port": 8020}],
    },
    {
        "name": "lma",
        "image": "dagents-lma:latest",
        "ports": [{"name": "http", "container_port": 8010}],
    },
    {
        "name": "model-service",
        "image": "dagents-model-service:latest",
        "ports": [{"name": "http", "container_port": 8000}],
    },
    {
        "name": "pipeline-service",
        "image": "dagents-pipeline-service:latest",
        "ports": [{"name": "http", "container_port": 8030}],
    },
    {
        "name": "core-service",
        "image": "dagents-core-service:latest",
        "ports": [{"name": "http", "container_port": 8040}],
    }
]

payload = {
    "namespace": "dagents-dev",
    "components": components,
    "include_services": True
}

req = urllib.request.Request(
    'http://127.0.0.1:8040/api/v1/manifests/pods',
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)

try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))
        with open('dagents-workloads.yaml', 'w') as f:
            f.write(data['combined_yaml'])
        print("Successfully generated dagents-workloads.yaml")
except Exception as e:
    print(f"Failed to generate manifests: {e}")
