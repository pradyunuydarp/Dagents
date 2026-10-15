"""The opt-in Compose override that lets the backend add a hospital as a container.

``docker-compose.deploy.yml`` mounts the Docker socket into the backend, so
these tests check what it publishes and what it sets, from the merged file that
``docker compose config`` prints. That reads the files only; no Docker daemon
is needed, and nothing is built or started. Docker is not in CI, so a person
checks the running stack by hand (``docs/deployment.md``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services.deployment import COMPOSE_SERVICES, check_deployer  # noqa: E402

BASE = "docker-compose.yml"
OVERRIDE = "docker-compose.deploy.yml"
BACKEND = "healthcare-demo-backend"
FRONTEND = "healthcare-demo-frontend"
GID_MESSAGE = "set DOCKER_GID to the group id of /var/run/docker.sock"

#: What the override sets on the backend.
BACKEND_ENVIRONMENT = {
    "HEALTHCARE_DEMO_DEPLOYER": "compose",
    "HEALTHCARE_DEMO_JOINING_LMA_URLS": "pl-290=http://lma-pl-290:8085",
    "HEALTHCARE_DEMO_DEPLOY_IMAGE": "dagents/healthcare-demo-backend:local",
    "HEALTHCARE_DEMO_DEPLOY_NETWORK": "healthcare-demo_default",
    "HEALTHCARE_DEMO_DEPLOY_DIR": "/tmp/healthcare-demo-deploy",
    "HEALTHCARE_DEMO_COORDINATOR_URL": "http://healthcare-demo-backend:8081",
}


def compose_config(*files: str, gid: str | None = "999") -> subprocess.CompletedProcess[str]:
    args = ["docker", "compose"]
    for name in files:
        args += ["-f", name]
    env = {key: value for key, value in os.environ.items() if key != "DOCKER_GID"}
    if gid is not None:
        env["DOCKER_GID"] = gid
    return subprocess.run(
        [*args, "config", "--format", "json"], cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120
    )


def published(service: dict) -> list[tuple[str | None, str, int]]:
    return [(port.get("host_ip"), port.get("published"), port["target"]) for port in service.get("ports") or []]


class ComposeOverrideTests(unittest.TestCase):
    merged: dict
    base: dict

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not installed, so docker compose cannot check the override")
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            raise unittest.SkipTest("docker compose is not available, so it cannot check the override")
        merged = compose_config(BASE, OVERRIDE)
        assert merged.returncode == 0, merged.stderr
        base = compose_config(BASE, gid=None)
        assert base.returncode == 0, base.stderr
        cls.merged = json.loads(merged.stdout)
        cls.base = json.loads(base.stdout)

    def service(self, name: str) -> dict:
        return self.merged["services"][name]

    def test_only_this_machine_reaches_the_backend_and_the_ui(self) -> None:
        self.assertEqual([("127.0.0.1", "8080", 8080)], published(self.service(BACKEND)))
        self.assertEqual([("127.0.0.1", "5174", 5174)], published(self.service(FRONTEND)))
        self.assertEqual([], published(self.service("core-service")))
        # The internal listener stays unpublished.
        self.assertNotIn(8081, [target for _, _, target in published(self.service(BACKEND))])

    def test_the_backend_gets_the_docker_socket_and_its_group(self) -> None:
        backend = self.service(BACKEND)
        self.assertEqual(["999"], backend["group_add"])
        self.assertIn(
            ("bind", "/var/run/docker.sock", "/var/run/docker.sock"),
            [(volume["type"], volume["source"], volume["target"]) for volume in backend["volumes"]],
        )
        for name, service in self.merged["services"].items():
            if name != BACKEND:
                with self.subTest(service=name):
                    self.assertNotIn("/var/run/docker.sock", json.dumps(service.get("volumes") or []))

    def test_without_the_group_id_compose_stops_with_the_message(self) -> None:
        result = compose_config(BASE, OVERRIDE, gid=None)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(GID_MESSAGE, result.stderr)

    def test_the_backend_image_adds_the_pinned_docker_cli(self) -> None:
        build = self.service(BACKEND)["build"]
        self.assertNotIn("dockerfile", build)
        self.assertEqual({"backend-image": "service:lma-uk-134"}, build["additional_contexts"])
        lines = build["dockerfile_inline"].splitlines()
        self.assertEqual("FROM docker:29.8.1-cli AS docker-cli", lines[0])
        self.assertEqual("FROM backend-image", lines[1])
        self.assertEqual("USER appuser", lines[-1])
        self.assertEqual("dagents/healthcare-demo-backend-deploy:local", self.service(BACKEND)["image"])
        # The new hospital runs the image the other hospitals run, not the one with Docker in it.
        self.assertEqual("dagents/healthcare-demo-backend:local", self.service("lma-uk-134")["image"])

    def test_the_backend_settings_add_hospital_290_and_pass_the_start_up_checks(self) -> None:
        environment = self.service(BACKEND)["environment"]
        self.assertEqual(BACKEND_ENVIRONMENT, {key: environment[key] for key in BACKEND_ENVIRONMENT})
        self.assertEqual("app", environment["HEALTHCARE_DEMO_ROLE"])
        self.assertEqual("http://core-service:8040", environment["CORE_SERVICE_PUBLIC_URL"])
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = Settings()
        check_deployer(settings, environment)
        self.assertEqual("compose", settings.deployer)
        depends_on = self.service(BACKEND)["depends_on"]
        self.assertEqual("service_healthy", depends_on["core-service"]["condition"])
        self.assertEqual("service_healthy", depends_on["gma"]["condition"])

    def test_the_gma_accepts_the_address_the_backend_will_use(self) -> None:
        gma = self.service("gma")["environment"]
        backend = self.service(BACKEND)["environment"]
        self.assertEqual(backend["HEALTHCARE_DEMO_JOINING_LMA_URLS"], gma["GMA_FEDERATION_JOINABLE_SITES"])
        self.assertEqual(
            self.base["services"]["gma"]["environment"]["GMA_FEDERATION_SITES"], gma["GMA_FEDERATION_SITES"]
        )

    def test_core_service_runs_on_the_demo_network_with_its_health_check(self) -> None:
        core = self.service("core-service")
        self.assertEqual("dagents/core-service:local", core["image"])
        self.assertEqual("services/core-service/Dockerfile", core["build"]["dockerfile"])
        self.assertEqual("8040", core["environment"]["API_PORT"])
        self.assertEqual("CMD-SHELL", core["healthcheck"]["test"][0])
        self.assertEqual("unless-stopped", core["restart"])
        self.assertEqual("healthcare-demo", self.merged["name"])

    def test_every_service_has_a_name_the_new_hospital_does_not_use(self) -> None:
        self.assertEqual(set(COMPOSE_SERVICES), set(self.merged["services"]))
        self.assertNotIn("lma-pl-290", self.merged["services"])

    def test_the_other_services_and_the_base_file_are_unchanged(self) -> None:
        changed = {"core-service", "gma", BACKEND, FRONTEND}
        for name, service in self.base["services"].items():
            if name not in changed:
                with self.subTest(service=name):
                    self.assertEqual(service, self.service(name))
        base_gma = dict(self.base["services"]["gma"]["environment"])
        merged_gma = dict(self.service("gma")["environment"])
        # The base file's GMA accepts no address at run time (env/.env.gma leaves it empty).
        self.assertEqual("", base_gma.pop("GMA_FEDERATION_JOINABLE_SITES", ""))
        merged_gma.pop("GMA_FEDERATION_JOINABLE_SITES")
        self.assertEqual(base_gma, merged_gma)
        # Without the override: no core-service, no socket, the deployer off, ports as before.
        self.assertNotIn("core-service", self.base["services"])
        base_backend = self.base["services"][BACKEND]
        self.assertEqual("off", base_backend["environment"]["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertIsNone(base_backend.get("volumes"))
        self.assertIsNone(base_backend.get("group_add"))
        self.assertEqual([(None, "8080", 8080)], published(base_backend))
        self.assertEqual([(None, "5174", 5174)], published(self.base["services"][FRONTEND]))


if __name__ == "__main__":
    unittest.main()
