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
#: Network mode: each hospital's LMA address and the app's ``site`` token for it.
#: The config builds these names from the site id, so the pattern above cannot find them.
LMA_URLS = tuple(f"HEALTHCARE_DEMO_LMA_{site}_URL" for site in ("UK_134", "IT_002", "CH_245"))
LMA_TOKENS = tuple(f"HEALTHCARE_DEMO_LMA_{site}_TOKEN" for site in ("UK_134", "IT_002", "CH_245"))
#: Settings that hold a secret: they are set in the environment, never written down.
TOKENS = ("HEALTHCARE_DEMO_GMA_TOKEN", *LMA_TOKENS)
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

    def test_the_per_hospital_agent_names_match_the_config(self) -> None:
        from app.core.config import lma_token_variable, lma_url_variable
        from app.domain.dataset import SITES

        self.assertEqual(LMA_URLS, tuple(lma_url_variable(site.site_id) for site in SITES))
        self.assertEqual(LMA_TOKENS, tuple(lma_token_variable(site.site_id) for site in SITES))

    def test_the_env_file_names_every_agent_address_and_token(self) -> None:
        text = ENV_FILE.read_text(encoding="utf-8")
        for name in ("HEALTHCARE_DEMO_AGENTS", "HEALTHCARE_DEMO_GMA_URL", *LMA_URLS, *TOKENS):
            with self.subTest(name=name):
                self.assertTrue(name in text, f"{name} is not listed")

    def test_no_token_is_written_down(self) -> None:
        """Network-mode tokens come from the environment; in process the app makes its own at start."""
        active = {
            line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        env = render_env()
        for name in TOKENS:
            with self.subTest(name=name):
                self.assertNotIn(name, active)
                if name in env:
                    self.assertEqual("false", env[name].get("sync"))
                    self.assertNotIn("value", env[name])

    def test_the_published_demo_runs_the_agents_in_process(self) -> None:
        """One Render service: the three LMAs and the GMA run inside it and need no address or secret."""
        env = render_env()
        self.assertEqual("in-process", env["HEALTHCARE_DEMO_AGENTS"]["value"])
        for name in ("HEALTHCARE_DEMO_GMA_URL", *LMA_URLS):
            with self.subTest(name=name):
                self.assertNotIn("value", env.get(name, {}))

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
