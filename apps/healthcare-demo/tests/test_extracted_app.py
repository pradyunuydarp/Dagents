"""The app still runs after ``scripts/extract_repo.sh`` moves it into its own repository.

After extraction the framework is not two directories up any more. The README
and the extract script tell the user to set ``DAGENTS_HOME`` to a Dagents
checkout. These tests copy the app to a temporary directory, outside the
Dagents tree, and run the parts that find the framework there: the startup
tests and both data scripts. None of them needs the planner or a database.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1]).resolve()

#: Nothing listens on port 1, so a script that gets as far as connecting fails at once.
UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/healthcare"


class ExtractedAppTests(unittest.TestCase):
    copy: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.copy = Path(cls._tmp.name) / "dagents-healthcare-demo"
        shutil.copytree(
            APP_ROOT,
            cls.copy,
            ignore=shutil.ignore_patterns("node_modules", "dist", "__pycache__", ".venv", "frontend", "*.pyc"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_in_copy(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "DAGENTS_HOME")}
        env["DAGENTS_HOME"] = str(FRAMEWORK_ROOT)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, *args], cwd=self.copy, env=env, capture_output=True, text=True, timeout=300
        )

    def test_the_copy_is_outside_the_framework(self) -> None:
        self.assertNotIn(FRAMEWORK_ROOT, self.copy.resolve().parents)

    def test_the_startup_tests_pass_in_the_copy(self) -> None:
        result = self.run_in_copy(
            "-m", "unittest", "tests.test_startup.ImportTimeTests",
            extra_env={"PYTHONPATH": os.pathsep.join([str(FRAMEWORK_ROOT), "backend"])},
        )
        self.assertEqual(0, result.returncode, result.stderr[-3000:])

    def test_the_seed_script_finds_the_app_and_the_framework(self) -> None:
        result = self.run_in_copy("scripts/seed_supabase.py", extra_env={"SUPABASE_DB_URL": UNREACHABLE})
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertIn("could not connect to the database", result.stderr)

    def test_the_migration_script_finds_the_app_and_the_framework(self) -> None:
        result = self.run_in_copy(
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); import apply_migrations; "
            "from app.services.db_hints import connection_hint; "
            "print(apply_migrations.connection_hint is connection_hint)",
        )
        self.assertEqual(0, result.returncode, result.stderr[-3000:])
        self.assertEqual("True", result.stdout.strip())
        result = self.run_in_copy("scripts/apply_migrations.py", extra_env={"SUPABASE_DB_URL": UNREACHABLE})
        self.assertIn("Could not connect", result.stderr)


if __name__ == "__main__":
    unittest.main()
