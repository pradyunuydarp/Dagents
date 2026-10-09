"""Tests for the guards that stand between a capture and a published demo.

Two generated artifacts now reach users directly: the shared design system, and
the recordings the published demos replay. Both have a guard, and a guard that
stops matching is worse than no guard — it restores the false green while
looking like protection. So each is tested against the shape it is meant to
catch.

The recording checker matters most. `capture_demo_recordings.py` can exit zero
and still produce a capture that publishes three governance levers which change
nothing — that is exactly what a capture made without the planner looks like,
because the Guard fails closed and denies everything. These tests build that
capture deliberately and assert the checker refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_demo_recordings as guard  # noqa: E402  (needs the sys.path entry)
import sync_design_system  # noqa: E402


def probe(*, permitted: bool, strategy: str, cohort: int, granularity: str = "row") -> dict:
    """One captured guard probe, shaped as the backend returns it."""
    return {
        "method": "POST",
        "path": "/api/v1/governance:probe",
        "request": {
            "verified": True,
            "granularity": granularity,
            "cohort_size": cohort,
            "boundary": "before_read",
        },
        "status": 200,
        "response": {
            "permitted": permitted,
            "plan": {"field_restrictions": [{"field": "nihss_total", "strategy": strategy}]},
        },
    }


def healthy_healthcare_capture() -> dict:
    """A capture that is worth publishing, used as the baseline to break."""
    strategies = ["generalize:1", "redact", "aggregate_only:20", "clip_contribution:1"]
    probes = [
        probe(permitted=True, strategy=strategies[index % len(strategies)], cohort=25)
        for index in range(24)
    ]
    probes += [probe(permitted=False, strategy="deny", cohort=5) for _ in range(4)]
    return {
        "captured_at": "2026-09-29T10:00:00Z",
        "commit": "abc123",
        "planner": "dagentsc",
        "entries": [
            {
                "method": "GET",
                "path": "/api/v1/overview",
                "status": 200,
                "response": {"hospitals": [{"site_id": "riverside"}]},
            },
            {
                "method": "GET",
                "path": "/api/v1/hospitals/riverside/worklist?limit=12",
                "status": 200,
                "response": {"worklist": []},
            },
            {
                "method": "POST",
                "path": "/api/v1/pilot:run",
                "request": {"round_prefix": "ui"},
                "status": 200,
                "response": {
                    "release": {"gates": {"gate_results": [{"gate_id": "fairness"}]}}
                },
            },
            *probes,
        ],
    }


def healthy_nl2sql_capture() -> dict:
    """The NL2SQL equivalent."""
    return {
        "captured_at": "2026-09-29T10:00:00Z",
        "commit": "abc123",
        "planner": "dagentsc",
        "entries": [
            {
                "method": "GET",
                "path": "/api/v1/samples",
                "status": 200,
                "response": [{"sample_id": "invoices"}],
            },
            {
                "method": "GET",
                "path": "/api/v1/dagents/status",
                "status": 200,
                "response": {"services": [{"name": "core-service", "status": "ok"}]},
            },
            {
                "method": "POST",
                "path": "/api/v1/generate",
                "request": {"question": "how many"},
                "status": 200,
                "response": {
                    "sql": "SELECT COUNT(*) FROM invoices",
                    "dagents_trace": [{"name": "Dagents SourceSpec validation", "status": "ok"}],
                },
            },
        ],
    }


class HealthcareCaptureTests(unittest.TestCase):
    """What the checker must refuse to publish."""

    def test_a_good_capture_passes(self) -> None:
        self.assertEqual([], guard.check_healthcare(healthy_healthcare_capture()))

    def test_an_all_denied_capture_is_refused(self) -> None:
        """The signature of a capture taken without the planner.

        The Guard fails closed, so with `dagentsc` unreachable every probe comes
        back denied and the published demo would show three levers that change
        nothing — while looking perfectly fine.
        """
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = False
                entry["response"]["plan"]["field_restrictions"][0]["strategy"] = "deny"
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("every captured guard probe was denied" in p for p in problems))

    def test_a_capture_with_no_denial_is_refused(self) -> None:
        """If nothing is ever denied, the cohort floor appears not to exist."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = True
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("no captured guard probe was denied" in p for p in problems))

    def test_uniform_strategies_are_refused(self) -> None:
        """Levers that do not change the strategy are decorative."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["plan"]["field_restrictions"][0]["strategy"] = "generalize:1"
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("distinct field strategies" in p for p in problems))

    def test_a_thin_matrix_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        capture["entries"] = [
            entry for entry in capture["entries"] if entry["path"] != "/api/v1/governance:probe"
        ] + [probe(permitted=True, strategy="generalize:1", cohort=25)]
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("lever matrix is incomplete" in p for p in problems))

    def test_a_pilot_that_never_reached_its_gates_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/pilot:run":
                entry["response"]["release"] = None
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("never reached its release gates" in p for p in problems))


class Nl2sqlCaptureTests(unittest.TestCase):
    def test_a_good_capture_passes(self) -> None:
        self.assertEqual([], guard.check_nl2sql(healthy_nl2sql_capture()))

    def test_a_generation_with_no_sql_is_refused(self) -> None:
        capture = healthy_nl2sql_capture()
        capture["entries"][2]["response"]["sql"] = "   "
        self.assertTrue(any("no SQL" in p for p in guard.check_nl2sql(capture)))

    def test_an_empty_trace_is_refused(self) -> None:
        """The trace is the proof that the app consumes the framework."""
        capture = healthy_nl2sql_capture()
        capture["entries"][2]["response"]["dagents_trace"] = []
        self.assertTrue(any("empty Dagents trace" in p for p in guard.check_nl2sql(capture)))

    def test_a_capture_with_no_reachable_service_is_refused(self) -> None:
        capture = healthy_nl2sql_capture()
        capture["entries"][1]["response"]["services"][0]["status"] = "unavailable"
        problems = guard.check_nl2sql(capture)
        self.assertTrue(any("no framework service was reachable" in p for p in problems))


class CaptureFileTests(unittest.TestCase):
    """The loader's own failure modes."""

    def test_a_missing_capture_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            guard.load(REPO_ROOT / "does-not-exist.json")

    def test_an_empty_capture_is_an_error(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.json"
            path.write_text(json.dumps({"entries": []}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                guard.load(path)

    def test_main_reports_the_demo_it_refused(self) -> None:
        """A failing capture must fail the deploy, not warn and continue."""
        broken = healthy_healthcare_capture()
        for entry in broken["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = False
        with mock.patch.object(guard, "load", return_value=broken):
            self.assertEqual(1, guard.main(["healthcare"]))


class DesignSystemTests(unittest.TestCase):
    """The three frontends must not drift apart visually."""

    def test_every_copy_matches_the_source(self) -> None:
        """Editing a copy instead of the source is the mistake this catches."""
        self.assertEqual(
            0,
            sync_design_system.main(["--check"]),
            "A design-system copy is stale. Run: python scripts/sync_design_system.py --write",
        )

    def test_every_frontend_has_a_copy(self) -> None:
        for relative in sync_design_system.COPIES:
            with self.subTest(copy=str(relative)):
                self.assertTrue((REPO_ROOT / relative).is_file())

    def test_a_copy_carries_the_do_not_edit_banner(self) -> None:
        for relative in sync_design_system.COPIES:
            with self.subTest(copy=str(relative)):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("GENERATED FILE", text)

    def test_the_source_defines_every_token_on_bare_root(self) -> None:
        """A token defined only inside a dark block is the classic theme bug.

        It leaves one theme's text on the other theme's background, and it is
        invisible until someone opens the page in the theme nobody tested.
        """
        source = sync_design_system.SOURCE.read_text(encoding="utf-8")
        base, _, rest = source.partition("@media (prefers-color-scheme: dark)")
        self.assertTrue(rest, "the design system has no dark theme block")
        import re

        defined_in_base = set(re.findall(r"(--ds-[a-z0-9-]+):", base))
        defined_in_dark = set(re.findall(r"(--ds-[a-z0-9-]+):", rest))
        missing = sorted(defined_in_dark - defined_in_base)
        self.assertEqual(
            [],
            missing,
            f"these tokens are only defined in a dark block: {missing}",
        )


def workflow_step_script(text: str, name: str) -> str:
    """The shell body of one `run:` step, lifted out of a workflow.

    Deliberately not a YAML parse: PyYAML is not a declared dependency of this
    repository, and a guard that skips itself when an import is missing is
    green without having run. The slice is strict instead — a step that stops
    matching raises here rather than quietly testing nothing.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:] if line.strip() else "")
    if not any(body):
        raise AssertionError(f"found no script under {name!r}")
    return "\n".join(body)


class ApiAddressResolutionTests(unittest.TestCase):
    """What the published demo is pointed at, and what it refuses to be.

    This runs the workflow's own resolution step rather than a copy of it. The
    case that matters is the third: a deploy hook pasted into the override
    variable instead of the service URL. Using it would publish a demo whose
    every request is rejected, and — because repository variables are not
    masked — print its key in a public log.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    STEP = "Resolve the deployed healthcare API"

    def resolve(self, override: str) -> tuple[str, str]:
        """Run the step with that override; return (resolved address, stdout)."""
        import subprocess
        import tempfile

        script = workflow_step_script(self.WORKFLOW.read_text(encoding="utf-8"), self.STEP)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            summary = Path(tmp) / "summary"
            env_file.touch()
            summary.touch()
            proc = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "OVERRIDE": override,
                    "GITHUB_ENV": str(env_file),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            written = env_file.read_text(encoding="utf-8").strip()
        prefix = "HEALTHCARE_API_BASE="
        self.assertTrue(written.startswith(prefix), written)
        return written[len(prefix) :], proc.stdout

    def test_without_an_override_it_uses_the_committed_address(self) -> None:
        resolved, out = self.resolve("")
        self.assertTrue(resolved.startswith("https://"), resolved)
        self.assertIn("env/.env.published", out)

    def test_a_plain_override_wins(self) -> None:
        resolved, out = self.resolve("https://staging-api.example.com")
        self.assertEqual("https://staging-api.example.com", resolved)
        self.assertIn("repository variable", out)

    def test_an_override_carrying_a_key_is_refused_and_not_used(self) -> None:
        committed, _ = self.resolve("")
        resolved, out = self.resolve("https://api.render.com/sync/exs-abc?key=secret")
        self.assertEqual(committed, resolved, "a deploy hook was used as the API base")
        self.assertIn("::warning::", out)
        self.assertIn("rotate", out)

    def test_an_override_carrying_credentials_is_refused(self) -> None:
        committed, _ = self.resolve("")
        resolved, _ = self.resolve("https://user:pass@api.example.com")
        self.assertEqual(committed, resolved)


class PublishedEndpointTests(unittest.TestCase):
    """`env/.env.published` is where a deployed address lives, and only that.

    The healthcare demo is published calling a real API, so its address has to
    be in the repository somewhere. This file is that place — one copy, next to
    the other env files — and these tests hold the two properties that make it
    safe: it stays free of credentials, and the workflow still reads the key it
    defines. A rename in one of the two files and not the other would publish a
    demo that silently falls back to its recording.
    """

    PUBLISHED = REPO_ROOT / "env" / ".env.published"
    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"

    def values(self) -> dict[str, str]:
        text = self.PUBLISHED.read_text(encoding="utf-8")
        pairs = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
        return pairs

    def test_the_healthcare_api_is_declared(self) -> None:
        url = self.values().get("HEALTHCARE_DEMO_PUBLIC_API_URL", "")
        self.assertTrue(url.startswith("https://"), f"expected an https URL, got {url!r}")
        self.assertFalse(
            url.endswith("/"),
            "a trailing slash doubles the separator in every URL the workflow builds",
        )

    def test_nothing_in_it_looks_like_a_credential(self) -> None:
        """The file is committed, so a secret here is a secret in every clone."""
        for key, value in self.values().items():
            with self.subTest(key=key):
                lowered = key.lower()
                self.assertFalse(
                    any(word in lowered for word in ("secret", "password", "token", "_key")),
                    f"{key} belongs in Actions secrets, not in a committed file",
                )
                self.assertNotIn("@", value, f"{key} looks like it carries credentials")

    def test_the_pages_workflow_reads_the_api_address(self) -> None:
        """The build has to resolve the one key it needs from this file."""
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "HEALTHCARE_DEMO_PUBLIC_API_URL",
            workflow,
            "the API address is declared but the Pages build does not read it, so the "
            "published demo would fall back to its recording",
        )

    def test_every_published_address_is_linked_from_the_readme(self) -> None:
        """A moved site should fail here, not rot as a dead link in the docs.

        The `*_SITE_URL` values are not read by any build — the workflow derives
        the base path from `configure-pages`. They are declared so that the links
        in the README have a source of truth, which is only worth anything if the
        two are checked against each other.
        """
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        published = {
            key: value
            for key, value in self.values().items()
            if key.endswith("_SITE_URL") or key.endswith("_API_URL")
        }
        self.assertTrue(published, "no published addresses are declared")
        for key, value in published.items():
            with self.subTest(key=key):
                self.assertIn(
                    value,
                    readme,
                    f"{key} is {value} and the README does not link it",
                )

    def test_the_healthcare_build_uses_the_resolved_value(self) -> None:
        """The probe clears the address when the API will not wake.

        It does that by rewriting `HEALTHCARE_API_BASE` in the job environment,
        which only works if the build step reads `env.` and not `vars.`. Reading
        the repository variable directly would publish a page pointing at a
        backend the same workflow just found to be down.
        """
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("VITE_HEALTHCARE_API_BASE: ${{ env.HEALTHCARE_API_BASE }}", workflow)


if __name__ == "__main__":
    unittest.main()
