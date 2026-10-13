"""Every setting the app reads is listed where a deployer looks for it.

A setting that exists only in ``config.py`` gets its default on the host,
whatever that default suits. These checks read the files as text: the Render
blueprint and the env file must name every setting, and neither may hold a
credential.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG = APP_ROOT / "backend" / "app" / "core" / "config.py"
RENDER = APP_ROOT / "render.yaml"
ENV_FILE = APP_ROOT / "env" / ".env.healthcare-demo"
EXAMPLE = APP_ROOT / "env" / ".env.healthcare-supabase.example"

SITE_URLS = tuple(
    f"HEALTHCARE_DEMO_SITE_{site}_DATABASE_URL" for site in ("UK_134", "IT_002", "CH_245")
)
#: A connection string with a password in it: ``scheme://user:password@``.
CREDENTIAL = re.compile(r"[a-z]+://[^\s:/@<>]+:[^\s@<>]+@")


#: Set by the platform itself, never by a deployer.
PLATFORM_VARIABLES = frozenset({"RENDER"})


def settings_read() -> set[str]:
    source = CONFIG.read_text(encoding="utf-8")
    return set(re.findall(r'(?:getenv|_bool_env)\(\s*"([A-Z][A-Z0-9_]+)"', source)) - PLATFORM_VARIABLES


def render_env() -> dict[str, dict[str, str]]:
    """``envVars`` entries of the blueprint: key to its other fields, as text.

    Read with a pattern rather than a YAML parser, so the suite needs nothing
    beyond the app's own requirements.
    """
    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in RENDER.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        key = re.match(r"^\s+- key: (\S+)\s*$", line)
        if key:
            current = entries.setdefault(key.group(1), {})
            continue
        field = re.match(r"^\s+(sync|value): (.+?)\s*$", line)
        if field and current is not None:
            current[field.group(1)] = field.group(2).strip('"')
    return entries


class SettingsDocumentedTests(unittest.TestCase):
    def test_the_config_reads_the_settings_this_test_expects(self) -> None:
        names = settings_read()
        self.assertGreaterEqual(len(names), 20, names)
        for name in (
            "HEALTHCARE_DEMO_TRUST_FORWARDED_FOR",
            "HEALTHCARE_DEMO_FORWARDED_FOR_HOPS",
            "HEALTHCARE_DEMO_MAX_BODY_BYTES",
            "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS",
        ):
            self.assertIn(name, names)

    def test_the_render_blueprint_names_every_setting(self) -> None:
        text = RENDER.read_text(encoding="utf-8")
        for name in sorted(settings_read() | set(SITE_URLS)):
            with self.subTest(name=name):
                self.assertTrue(name in text, f"{name} is not listed")

    def test_the_env_file_names_every_setting(self) -> None:
        text = ENV_FILE.read_text(encoding="utf-8")
        for name in sorted(settings_read() | set(SITE_URLS)):
            with self.subTest(name=name):
                self.assertTrue(name in text, f"{name} is not listed")

    def test_credentials_are_prompted_for_never_written(self) -> None:
        env = render_env()
        for name in ("HEALTHCARE_DEMO_DATABASE_URL", "HEALTHCARE_DEMO_CORS_ORIGINS", *SITE_URLS):
            with self.subTest(name=name):
                self.assertEqual("false", env[name].get("sync"))
                self.assertNotIn("value", env[name])

    def test_render_trusts_the_entry_its_edge_wrote(self) -> None:
        env = render_env()
        self.assertEqual("true", env["HEALTHCARE_DEMO_TRUST_FORWARDED_FOR"]["value"])
        self.assertEqual("3", env["HEALTHCARE_DEMO_FORWARDED_FOR_HOPS"]["value"])

    def test_the_env_file_leaves_the_host_dependent_settings_to_the_host(self) -> None:
        """The image copies this file, so an active value would override the RENDER defaults."""
        active = {
            line.split("=", 1)[0].strip()
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        for name in (
            "HEALTHCARE_DEMO_TRUST_FORWARDED_FOR",
            "HEALTHCARE_DEMO_FORWARDED_FOR_HOPS",
            "HEALTHCARE_DEMO_DATABASE_URL",
            "HEALTHCARE_DEMO_CORS_ORIGINS",
            *SITE_URLS,
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, active)

    def test_no_file_holds_a_credential(self) -> None:
        for path in (RENDER, ENV_FILE, EXAMPLE):
            with self.subTest(path=path.name):
                self.assertIsNone(CREDENTIAL.search(path.read_text(encoding="utf-8").replace("<password>", "")))


if __name__ == "__main__":
    unittest.main()
