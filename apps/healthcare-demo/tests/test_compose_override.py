"""The opt-in Compose override that lets Hospital 290 join as a container, and the settings ``setup`` writes.

``docker-compose.deploy.yml`` mounts the Docker socket into the API's
container. :class:`ComposeOverrideTests` checks what it publishes and what it
sets, from the merged file ``docker compose config`` prints. That reads the
files only: no Docker daemon is needed, and nothing is built or started.

:class:`SettingsWriterTests` runs ``scripts/write_agent_settings.py``, which
``setup`` runs, into a scratch folder, and checks the files it writes with and
without the override: who holds which token, that the GMA accepts the address
the API uses, and that the API's settings pass its start-up checks. It needs
neither Docker nor a database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1]).resolve()
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

import yaml  # noqa: E402

from agents.common.infrastructure.compose_apply import parse_env_file  # noqa: E402

from app.domain.dataset import JOINING_SITES, SITES  # noqa: E402
from app.services.deployment import COMPOSE_SERVICES  # noqa: E402

BASE = "docker-compose.yml"
OVERRIDE = "docker-compose.deploy.yml"
ENV_FILE = "env/.env.healthcare-demo"
BACKEND = "healthcare-demo-backend"
FRONTEND = "healthcare-demo-frontend"
GID_MESSAGE = "set DOCKER_GID to the group id of /var/run/docker.sock"
WRITER = APP_ROOT / "scripts" / "write_agent_settings.py"
JOINING = JOINING_SITES[0]

#: The oldest Docker Compose that reads ``additional_contexts: service:<name>``.
#: Older versions take it for a folder without a warning, so ``config`` passes
#: and only the build fails.
MINIMUM_COMPOSE = (2, 33)


def compose_version(text: str) -> tuple[int, int] | None:
    """``(major, minor)`` from ``docker compose version --short``, such as ``2.33.1`` or ``v2.24.6``."""
    match = re.match(r"v?(\d+)\.(\d+)", text.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def env_file_values() -> dict[str, str]:
    """The active ``NAME=value`` lines of ``env/.env.healthcare-demo``."""
    values = {}
    for line in (APP_ROOT / ENV_FILE).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def compose_config(*files: str, gid: str | None = "999") -> subprocess.CompletedProcess[str]:
    args = ["docker", "compose"]
    for name in files:
        args += ["-f", name]
    env = {key: value for key, value in os.environ.items() if key != "DOCKER_GID" and not key.startswith("HEALTHCARE_")}
    if gid is not None:
        env["DOCKER_GID"] = gid
    return subprocess.run(
        [*args, "--env-file", ENV_FILE, "config", "--format", "json"],
        cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def published(service: dict) -> list[tuple[str | None, str, int]]:
    return [(port.get("host_ip"), port.get("published"), port["target"]) for port in service.get("ports") or []]


def sourced(path: Path) -> dict[str, str]:
    """A settings file as ``sh`` reads it with ``set -a && . file``: ``NAME=<shell word>`` lines."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        (word,) = shlex.split(line)
        name, value = word.split("=", 1)
        values[name] = value
    return values


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's ``!reset`` and ``!override`` tags as the values they hold."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: None)
_ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node),
)


def override_document() -> dict:
    return yaml.load((APP_ROOT / OVERRIDE).read_text(encoding="utf-8"), Loader=_ComposeLoader)


def interpolated(value: str, values: dict[str, str]) -> str:
    """``${NAME}`` and ``${NAME:?message}`` replaced from ``values``, as Compose does."""
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::\?[^}]*)?\}", lambda match: values[match.group(1)], value)


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
        short = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, text=True, timeout=60)
        version = compose_version(short.stdout)
        if version is None or version < MINIMUM_COMPOSE:
            raise AssertionError(
                f"{OVERRIDE} needs Docker Compose {MINIMUM_COMPOSE[0]}.{MINIMUM_COMPOSE[1]} or later, which reads "
                f"'additional_contexts: service:lma-uk-134'. This machine has {short.stdout.strip() or 'no version'}."
            )
        merged = compose_config(BASE, OVERRIDE)
        assert merged.returncode == 0, merged.stderr
        base = compose_config(BASE, gid=None)
        assert base.returncode == 0, base.stderr
        cls.merged = json.loads(merged.stdout)
        cls.base = json.loads(base.stdout)
        cls.ports = env_file_values()

    def service(self, name: str) -> dict:
        return self.merged["services"][name]

    def test_only_this_machine_reaches_the_api_and_the_ui(self) -> None:
        api, ui = self.ports["HEALTHCARE_DEMO_API_PORT"], self.ports["HEALTHCARE_DEMO_UI_PORT"]
        self.assertEqual([("127.0.0.1", api, int(api))], published(self.service(BACKEND)))
        self.assertEqual([("127.0.0.1", ui, 5174)], published(self.service(FRONTEND)))
        self.assertEqual([], published(self.service("core-service")))
        for name in ("gma", *(f"lma-{site.site_id}" for site in SITES), "postgres"):
            with self.subTest(service=name):
                self.assertEqual([], published(self.service(name)))

    def test_only_the_api_gets_the_docker_socket_and_its_group(self) -> None:
        backend = self.service(BACKEND)
        self.assertEqual(["999"], backend["group_add"])
        self.assertIn(
            ("bind", "/var/run/docker.sock", "/var/run/docker.sock"),
            [(volume["type"], volume["source"], volume["target"]) for volume in backend["volumes"]],
        )
        self.assertIn("/run/agent-settings", [volume["target"] for volume in backend["volumes"]])
        for name, service in self.merged["services"].items():
            if name != BACKEND:
                with self.subTest(service=name):
                    self.assertNotIn("/var/run/docker.sock", json.dumps(service.get("volumes") or []))

    def test_without_the_group_id_compose_stops_with_the_message(self) -> None:
        result = compose_config(BASE, OVERRIDE, gid=None)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(GID_MESSAGE, result.stderr)

    def test_the_api_image_adds_the_pinned_docker_cli(self) -> None:
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
        self.assertEqual(
            "dagents/healthcare-demo-backend:local", self.service(BACKEND)["environment"]["HEALTHCARE_DEMO_DEPLOY_IMAGE"]
        )

    def test_the_api_runs_the_compose_deployer_against_core_service(self) -> None:
        environment = self.service(BACKEND)["environment"]
        self.assertEqual("compose", environment["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertEqual(
            f"http://core-service:{self.ports['HEALTHCARE_DEMO_CORE_SERVICE_PORT']}",
            environment["HEALTHCARE_DEMO_CORE_SERVICE_URL"],
        )
        self.assertEqual("/run/agent-settings", environment["HEALTHCARE_DEMO_DEPLOY_ENV_DIR"])
        self.assertEqual("healthcare-demo_default", environment["HEALTHCARE_DEMO_DEPLOY_NETWORK"])
        self.assertEqual("healthcare-demo", self.merged["name"])
        depends_on = self.service(BACKEND)["depends_on"]
        self.assertEqual("service_healthy", depends_on["core-service"]["condition"])
        self.assertEqual("service_healthy", depends_on["gma"]["condition"])
        setup = self.service("setup")["environment"]
        self.assertEqual("compose", setup["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertEqual(self.ports[f"HEALTHCARE_DEMO_LMA_PL_290_PORT"], setup["HEALTHCARE_DEMO_LMA_PL_290_PORT"])

    def test_core_service_runs_on_the_demo_network_with_its_health_check(self) -> None:
        core = self.service("core-service")
        self.assertEqual("dagents/core-service:local", core["image"])
        self.assertEqual("services/core-service/Dockerfile", core["build"]["dockerfile"])
        self.assertEqual(self.ports["HEALTHCARE_DEMO_CORE_SERVICE_PORT"], core["environment"]["API_PORT"])
        self.assertEqual("CMD-SHELL", core["healthcheck"]["test"][0])
        self.assertEqual("unless-stopped", core["restart"])

    def test_every_service_has_a_name_the_new_hospital_does_not_use(self) -> None:
        self.assertEqual(set(COMPOSE_SERVICES), set(self.merged["services"]))
        self.assertNotIn(f"lma-{JOINING.site_id}", self.merged["services"])

    def test_the_other_services_and_the_base_file_are_unchanged(self) -> None:
        changed = {"core-service", "setup", BACKEND, FRONTEND}
        for name, service in self.base["services"].items():
            if name not in changed:
                with self.subTest(service=name):
                    self.assertEqual(service, self.service(name))
        # Without the override: no core-service, no socket, the deployer off.
        self.assertNotIn("core-service", self.base["services"])
        base_backend = self.base["services"][BACKEND]
        self.assertEqual("off", base_backend["environment"]["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertNotIn("/var/run/docker.sock", json.dumps(base_backend.get("volumes") or []))
        self.assertIsNone(base_backend.get("group_add"))
        self.assertEqual("off", self.base["services"]["setup"]["environment"]["HEALTHCARE_DEMO_DEPLOYER"])


class ComposeVersionTests(unittest.TestCase):
    """The version check in ``ComposeOverrideTests``, which needs no Docker."""

    def test_versions_are_read_and_compared(self) -> None:
        cases = {"2.24.6": False, "v2.24.6": False, "2.32.4": False, "2.33.0": True, "2.38.2\n": True, "5.6.0": True}
        for text, supported in cases.items():
            with self.subTest(version=text):
                version = compose_version(text)
                self.assertIsNotNone(version)
                self.assertEqual(supported, version >= MINIMUM_COMPOSE)
        self.assertIsNone(compose_version(""))


class SettingsWriterTests(unittest.TestCase):
    """What ``setup`` writes to the ``agent-settings`` volume, with and without the override."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="agent-settings-")
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)
        self.values = env_file_values()

    def write(self, deploy: bool) -> subprocess.CompletedProcess[str]:
        env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
        env.update({name: value for name, value in self.values.items() if name.endswith("_PORT")})
        env.update(
            HEALTHCARE_DEMO_DATABASE_URL="postgresql://postgres@postgres:5432/healthcare_demo",
            HEALTHCARE_DEMO_DEPLOYER="compose" if deploy else "off",
            DAGENTS_HOME=str(FRAMEWORK_ROOT),
        )
        result = subprocess.run(
            [sys.executable, str(WRITER), "compose", str(self.out)],
            cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_the_base_file_runs_the_writer(self) -> None:
        document = yaml.safe_load((APP_ROOT / BASE).read_text(encoding="utf-8"))
        command = "\n".join(document["services"]["setup"]["command"])
        self.assertIn("python scripts/write_agent_settings.py compose /run/agent-settings --owner appuser", command)

    def test_without_the_override_each_service_gets_its_own_file_and_nothing_joins(self) -> None:
        result = self.write(deploy=False)
        expected = {"gma.env", "api.env", "tokens.json", *(f"lma-{site.site_id}.env" for site in SITES)}
        self.assertEqual(expected, {path.name for path in self.out.iterdir()})
        for path in self.out.iterdir():
            with self.subTest(file=path.name):
                self.assertEqual(0o400, stat.S_IMODE(path.stat().st_mode))
        gma = sourced(self.out / "gma.env")
        self.assertNotIn("GMA_JOINABLE_SITE_ENDPOINTS", gma)
        self.assertEqual("3", gma["GMA_DISPATCH_CONCURRENCY"])
        api = sourced(self.out / "api.env")
        self.assertNotIn(f"HEALTHCARE_DEMO_LMA_{JOINING.site_id.upper().replace('-', '_')}_URL", api)
        tokens = json.loads((self.out / "tokens.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(site.site_id for site in SITES), sorted(tokens["lma_site"]))
        for token in tokens["lma_site"].values():
            self.assertNotIn(token, result.stdout + result.stderr)

    def test_with_the_override_hospital_290_has_its_tokens_address_and_env_file(self) -> None:
        self.write(deploy=True)
        gma = sourced(self.out / "gma.env")
        api = sourced(self.out / "api.env")
        joining = parse_env_file((self.out / f"lma-{JOINING.site_id}.env").read_text(encoding="utf-8"))
        key = JOINING.site_id.upper().replace("-", "_")
        url = f"http://lma-{JOINING.site_id}:{self.values[f'HEALTHCARE_DEMO_LMA_{key}_PORT']}"
        # The GMA accepts exactly the address the API gives the new LMA.
        self.assertEqual(f"{JOINING.site_id}={url}", gma["GMA_JOINABLE_SITE_ENDPOINTS"])
        self.assertEqual(url, api[f"HEALTHCARE_DEMO_LMA_{key}_URL"])
        self.assertNotIn(JOINING.site_id, gma["GMA_SITE_ENDPOINTS"])
        self.assertEqual(str(len(SITES) + 1), gma["GMA_DISPATCH_CONCURRENCY"])
        lma_tokens = dict(item.split("=", 1) for item in gma["GMA_LMA_TOKENS"].split(","))
        site_tokens = dict(item.split("=", 1) for item in gma["GMA_SITE_TOKENS"].split(","))
        self.assertEqual(
            {
                "LMA_DATABASE_URL": "postgresql://postgres@postgres:5432/healthcare_demo",
                "LMA_GMA_ENDPOINT": f"http://gma:{self.values['HEALTHCARE_DEMO_GMA_PORT']}",
                "LMA_GMA_TOKEN": site_tokens[JOINING.site_id],
                "LMA_SITE_TOKEN": api[f"HEALTHCARE_DEMO_LMA_{key}_TOKEN"],
                "LMA_COORDINATOR_TOKEN": lma_tokens[JOINING.site_id],
            },
            joining,
        )
        self.assertEqual(0o400, stat.S_IMODE((self.out / f"lma-{JOINING.site_id}.env").stat().st_mode))
        # Each file holds only its own service's tokens.
        joining_tokens = [joining[name] for name in ("LMA_GMA_TOKEN", "LMA_SITE_TOKEN", "LMA_COORDINATOR_TOKEN")]
        for site in SITES:
            other = sourced(self.out / f"lma-{site.site_id}.env")
            for token in joining_tokens:
                self.assertNotIn(token, other.values())
        self.assertNotIn(joining["LMA_COORDINATOR_TOKEN"], api.values())

    def test_the_tokens_are_kept_and_made_again_when_the_hospitals_change(self) -> None:
        self.write(deploy=False)
        first = json.loads((self.out / "tokens.json").read_text(encoding="utf-8"))
        self.assertIn("kept", self.write(deploy=False).stdout)
        self.assertEqual(first, json.loads((self.out / "tokens.json").read_text(encoding="utf-8")))
        self.assertIn("new", self.write(deploy=True).stdout)
        self.assertIn(JOINING.site_id, json.loads((self.out / "tokens.json").read_text(encoding="utf-8"))["lma_site"])

    def test_one_env_file_from_the_environment_never_from_the_arguments(self) -> None:
        """``run_local_demo.sh --deploy`` writes the new LMA's env file this way."""
        path = self.out / "lma-pl-290.env"
        env = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
        env.update(DAGENTS_HOME=str(FRAMEWORK_ROOT), LMA_SITE_TOKEN="a-token", LMA_GMA_ENDPOINT="http://127.0.0.1:1")
        result = subprocess.run(
            [sys.executable, str(WRITER), "env-file", str(path), "LMA_SITE_TOKEN", "LMA_GMA_ENDPOINT"],
            cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("a-token", result.stdout + result.stderr)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(
            {"LMA_SITE_TOKEN": "a-token", "LMA_GMA_ENDPOINT": "http://127.0.0.1:1"},
            parse_env_file(path.read_text(encoding="utf-8")),
        )
        # A value an env file cannot hold is refused, and the message never holds it.
        env["LMA_SITE_TOKEN"] = "it's-secret"
        result = subprocess.run(
            [sys.executable, str(WRITER), "env-file", str(self.out / "other.env"), "LMA_SITE_TOKEN"],
            cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("LMA_SITE_TOKEN", result.stderr)
        self.assertNotIn("secret", result.stdout + result.stderr)
        self.assertFalse((self.out / "other.env").exists())

    def test_the_apis_settings_with_the_override_pass_its_start_up_checks(self) -> None:
        self.write(deploy=True)
        document = override_document()
        backend = {
            name: interpolated(str(value), self.values)
            for name, value in document["services"][BACKEND]["environment"].items()
        }
        environment = {**self.values, **backend, **sourced(self.out / "api.env")}
        # The env folder is the volume in the container; here it is the scratch folder.
        environment["HEALTHCARE_DEMO_DEPLOY_ENV_DIR"] = str(self.out)
        environment["PATH"] = os.environ.get("PATH", "")
        environment["PYTHONPATH"] = os.pathsep.join([str(FRAMEWORK_ROOT), str(APP_ROOT / "backend")])
        # The settings are read when the app imports them, as in the container.
        check = (
            "from app.core.config import settings; from app.services.deployment import check_deployer; "
            "check_deployer(settings); print(settings.deployer, settings.agents_mode)"
        )
        result = subprocess.run(
            [sys.executable, "-c", check], cwd=APP_ROOT, env=environment, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(0, result.returncode, result.stderr[-2000:])
        self.assertEqual("compose network", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
