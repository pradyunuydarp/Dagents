"""The commands the package installs: ``python -m dagents``, ``dagents-lma`` and ``dagents-gma``."""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import dagents
from dagents import __main__ as package_main
from dagents.common import serve as serve_module
from dagents.gma import __main__ as gma_main
from dagents.lma import __main__ as lma_main


class VersionTests(unittest.TestCase):
    def test_the_version_is_a_pep_440_release_or_pre_release(self) -> None:
        self.assertRegex(dagents.__version__, r"^\d+\.\d+\.\d+((a|b|rc)\d+)?$")


class PlannerReportTests(unittest.TestCase):
    """``python -m dagents`` says which planner the runner would start."""

    def run_main(self, found: tuple[str | None, str]) -> tuple[int, str]:
        out = io.StringIO()
        with patch.object(package_main, "find_planner", return_value=found), contextlib.redirect_stdout(out):
            status = package_main.main()
        return status, out.getvalue()

    def test_a_found_planner_is_named_with_its_rule(self) -> None:
        status, text = self.run_main(("/site-packages/dagents/_bin/dagentsc", "bundled"))
        self.assertEqual(0, status)
        self.assertIn(f"dagents {dagents.__version__}", text)
        self.assertIn("/site-packages/dagents/_bin/dagentsc (the planner bundled with this package)", text)

    def test_no_planner_exits_1_and_says_the_guard_denies(self) -> None:
        status, text = self.run_main((None, "none"))
        self.assertEqual(1, status)
        self.assertIn("none found", text)
        self.assertIn("denies every request", text)

    def test_a_setting_that_names_no_runnable_file_exits_1(self) -> None:
        status, text = self.run_main(("/nonexistent/dagentsc", "DAGENTSC_BIN"))
        self.assertEqual(1, status)
        self.assertIn("DAGENTSC_BIN names /nonexistent/dagentsc, which is not a file this process can run", text)
        self.assertIn("denies every request", text)

    def test_the_limits_of_each_call_are_shown(self) -> None:
        with patch.dict(os.environ, {"DAGENTSC_TIMEOUT_SECONDS": "2.5", "DAGENTSC_MAX_INPUT_BYTES": "1000"}):
            status, text = self.run_main(("/site-packages/dagents/_bin/dagentsc", "bundled"))
        self.assertEqual(0, status)
        self.assertIn("planner limits: 2.5 seconds per call, 1000 bytes of input", text)

    def test_a_limit_that_makes_no_sense_exits_1(self) -> None:
        with patch.dict(os.environ, {"DAGENTSC_TIMEOUT_SECONDS": "0"}):
            status, text = self.run_main(("/site-packages/dagents/_bin/dagentsc", "bundled"))
        self.assertEqual(1, status)
        self.assertIn("DAGENTSC_TIMEOUT_SECONDS must be a number of seconds above 0", text)
        self.assertIn("denies every request", text)


class PlannerHashReportTests(unittest.TestCase):
    """``python -m dagents`` shows the planner's SHA-256 and checks it against ``DAGENTSC_SHA256``."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.planner = os.path.join(folder.name, "dagentsc")
        with open(self.planner, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.planner, 0o755)
        with open(self.planner, "rb") as handle:
            self.sha256 = hashlib.sha256(handle.read()).hexdigest()
        environ = {key: value for key, value in os.environ.items() if key != "DAGENTSC_SHA256"}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self) -> tuple[int, str]:
        out = io.StringIO()
        found = (self.planner, "DAGENTSC_BIN")
        with patch.object(package_main, "find_planner", return_value=found), contextlib.redirect_stdout(out):
            status = package_main.main()
        return status, out.getvalue()

    def test_the_hash_is_shown(self) -> None:
        status, text = self.run_main()
        self.assertEqual(0, status)
        self.assertIn(f"planner: {self.planner} (the DAGENTSC_BIN setting)", text)
        self.assertIn(f"planner sha256: {self.sha256}\n", text)

    def test_a_pinned_hash_that_matches_is_confirmed(self) -> None:
        os.environ["DAGENTSC_SHA256"] = self.sha256.upper()
        status, text = self.run_main()
        self.assertEqual(0, status)
        self.assertIn(f"planner sha256: {self.sha256}, as DAGENTSC_SHA256 requires", text)

    def test_a_pinned_hash_that_differs_exits_1(self) -> None:
        os.environ["DAGENTSC_SHA256"] = "0" * 64
        status, text = self.run_main()
        self.assertEqual(1, status)
        for words in (self.sha256, "0" * 64, "DAGENTSC_SHA256", "denies every request"):
            self.assertIn(words, text)
        self.assertNotIn("planner limits:", text)

    def test_a_pinned_hash_that_is_not_a_sha256_exits_1(self) -> None:
        os.environ["DAGENTSC_SHA256"] = "not-a-hash"
        status, text = self.run_main()
        self.assertEqual(1, status)
        self.assertIn("DAGENTSC_SHA256 must be", text)
        self.assertIn("denies every request", text)


class PlannerLimitStartupTests(unittest.TestCase):
    """An LMA or GMA refuses to start with a planner limit that makes no sense."""

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"DAGENTSC_MAX_OUTPUT_BYTES": "-1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lma_refuses_to_build(self) -> None:
        from dagents.lma.app_factory import create_app
        from dagents.lma.config import Settings

        with self.assertRaises(ValueError) as raised:
            create_app(Settings())
        self.assertIn("DAGENTSC_MAX_OUTPUT_BYTES must be a whole number of bytes above 0", str(raised.exception))

    def test_the_gma_refuses_to_build(self) -> None:
        from dagents.gma.app_factory import create_app
        from dagents.gma.config import Settings

        with self.assertRaises(ValueError) as raised:
            create_app(Settings())
        self.assertIn("DAGENTSC_MAX_OUTPUT_BYTES must be a whole number of bytes above 0", str(raised.exception))

    def test_the_agent_commands_stop_with_the_message_before_uvicorn_starts(self) -> None:
        for main in (lma_main.main, gma_main.main):
            with self.subTest(main=main.__module__), patch("uvicorn.run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    main([])
                self.assertIn("DAGENTSC_MAX_OUTPUT_BYTES must be", str(stopped.exception))
                run.assert_not_called()


class PinnedHashStartupTests(unittest.TestCase):
    """A ``DAGENTSC_SHA256`` that is not a SHA-256 stops an agent; one that differs makes it warn."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        planner = os.path.join(folder.name, "dagentsc")
        with open(planner, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(planner, 0o755)
        patcher = patch.dict(os.environ, {"DAGENTSC_BIN": planner})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_agents_refuse_to_build_with_a_pin_that_is_not_a_sha256(self) -> None:
        from dagents.gma.app_factory import create_app as create_gma
        from dagents.gma.config import Settings as GmaSettings
        from dagents.lma.app_factory import create_app as create_lma
        from dagents.lma.config import Settings as LmaSettings

        os.environ["DAGENTSC_SHA256"] = "abc"
        for create, settings in ((create_lma, LmaSettings), (create_gma, GmaSettings)):
            with self.subTest(agent=create.__module__), self.assertRaises(ValueError) as raised:
                create(settings())
            self.assertIn("DAGENTSC_SHA256 must be", str(raised.exception))

    def test_the_agent_commands_stop_before_uvicorn_starts(self) -> None:
        os.environ["DAGENTSC_SHA256"] = "abc"
        for main in (lma_main.main, gma_main.main):
            with self.subTest(main=main.__module__), patch("uvicorn.run") as run:
                with self.assertRaises(SystemExit) as stopped:
                    main([])
                self.assertIn("DAGENTSC_SHA256 must be", str(stopped.exception))
                run.assert_not_called()

    def test_the_agents_warn_when_the_planner_has_another_hash(self) -> None:
        from dagents.gma.app_factory import create_app as create_gma
        from dagents.gma.config import Settings as GmaSettings
        from dagents.lma.app_factory import create_app as create_lma
        from dagents.lma.config import Settings as LmaSettings

        os.environ["DAGENTSC_SHA256"] = "0" * 64
        for create, settings, logger, state in (
            (create_lma, LmaSettings, "dagents.lma.app_factory", "lma"),
            (create_gma, GmaSettings, "dagents.gma.app_factory", "gma"),
        ):
            with self.subTest(agent=logger), self.assertLogs(logger, level="WARNING") as captured:
                app = create(settings())
            self.addCleanup(getattr(app.state, state).close)
            warning = "\n".join(captured.output)
            self.assertIn("has no planner", warning)
            self.assertIn("DAGENTSC_SHA256", warning)


class StartupWarningTests(unittest.TestCase):
    """An LMA or GMA started without a planner logs why its Guard will deny every request."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(folder.name, "no-dagentsc")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lma_warns_when_it_is_built(self) -> None:
        from dagents.lma.app_factory import create_app
        from dagents.lma.config import Settings

        with self.assertLogs("dagents.lma.app_factory", level="WARNING") as captured:
            app = create_app(Settings())
        self.addCleanup(app.state.lma.close)
        self.assertTrue(any("dagents-lma has no planner" in line for line in captured.output), captured.output)

    def test_the_gma_warns_when_it_is_built(self) -> None:
        from dagents.gma.app_factory import create_app
        from dagents.gma.config import Settings

        with self.assertLogs("dagents.gma.app_factory", level="WARNING") as captured:
            app = create_app(Settings())
        self.addCleanup(app.state.gma.close)
        self.assertTrue(any("has no planner" in line for line in captured.output), captured.output)


class ServeTests(unittest.TestCase):
    """``dagents-lma`` and ``dagents-gma`` run uvicorn on the agent's app."""

    def setUp(self) -> None:
        environ = {k: v for k, v in os.environ.items() if not re.match(r"^(LMA|GMA)_API_(HOST|PORT)$", k)}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lma_defaults_to_localhost_8010(self) -> None:
        with patch("uvicorn.run") as run:
            self.assertEqual(0, lma_main.main([]))
        run.assert_called_once_with("dagents.lma.main:app", host="127.0.0.1", port=8010, log_level="info")

    def test_the_gma_defaults_to_localhost_8020(self) -> None:
        with patch("uvicorn.run") as run:
            self.assertEqual(0, gma_main.main([]))
        run.assert_called_once_with("dagents.gma.main:app", host="127.0.0.1", port=8020, log_level="info")

    def test_the_variables_set_the_host_and_port(self) -> None:
        os.environ["LMA_API_HOST"] = "0.0.0.0"
        os.environ["LMA_API_PORT"] = "9010"
        with patch("uvicorn.run") as run:
            lma_main.main([])
        run.assert_called_once_with("dagents.lma.main:app", host="0.0.0.0", port=9010, log_level="info")

    def test_the_flags_win_over_the_variables(self) -> None:
        os.environ["GMA_API_PORT"] = "9020"
        with patch("uvicorn.run") as run:
            gma_main.main(["--host", "127.0.0.2", "--port", "9021", "--log-level", "warning"])
        run.assert_called_once_with("dagents.gma.main:app", host="127.0.0.2", port=9021, log_level="warning")

    def test_a_port_that_is_not_a_number_stops_with_a_message(self) -> None:
        os.environ["LMA_API_PORT"] = "eighty"
        with self.assertRaises(SystemExit) as stopped:
            lma_main.main([])
        self.assertIn("whole number", str(stopped.exception))

    def test_without_uvicorn_it_names_the_extra(self) -> None:
        real_import = builtins.__import__

        def no_uvicorn(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202 - import hook
            if name == "uvicorn":
                raise ImportError("No module named 'uvicorn'")
            return real_import(name, *args, **kwargs)

        err = io.StringIO()
        with patch.object(builtins, "__import__", no_uvicorn), contextlib.redirect_stderr(err):
            status = serve_module.serve(agent="LMA", app_path="x:app", env_prefix="LMA", default_port=8010, argv=[])
        self.assertEqual(2, status)
        self.assertIn("pip install 'dagents[lma]'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
