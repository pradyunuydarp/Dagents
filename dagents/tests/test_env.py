"""``dagents.common.env``: env files are read from a source tree, never from an installed package.

In a checkout, or an image that copies ``dagents/`` and ``env/`` side by side,
the agents and services read ``env/.env.*`` next to the ``dagents`` folder.
An installed package lives in ``site-packages``, where any other distribution
could install an ``env/`` folder, so there the settings come only from the
process environment.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dagents.common import env

VARIABLE = "DAGENTS_ENV_FILE_TEST_VALUE"


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        environ = {key: value for key, value in os.environ.items() if key != VARIABLE}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tree(self, name: str) -> Path:
        """A folder holding ``dagents/`` and ``env/.env.test``, as a checkout or an image does."""
        root = self.base / name
        (root / "dagents").mkdir(parents=True)
        (root / "env").mkdir()
        (root / "env" / ".env.test").write_text(f"{VARIABLE}=from-the-env-file\n", encoding="utf-8")
        return root

    def test_a_source_tree_loads_its_env_files(self) -> None:
        root = self.tree("checkout")
        self.assertTrue(env.reads_env_files(root))
        env.load_env_files("env/.env.test", root=root)
        self.assertEqual("from-the-env-file", os.environ[VARIABLE])

    def test_a_variable_already_set_is_kept(self) -> None:
        root = self.tree("checkout")
        os.environ[VARIABLE] = "from-the-process"
        env.load_env_files("env/.env.test", root=root)
        self.assertEqual("from-the-process", os.environ[VARIABLE])

    def test_an_installed_package_reads_no_env_file(self) -> None:
        for name in ("lib/python3.12/site-packages", "lib/python3/dist-packages"):
            with self.subTest(folder=name):
                root = self.tree(name)
                self.assertFalse(env.reads_env_files(root))
                env.load_env_files("env/.env.test", root=root)
                self.assertNotIn(VARIABLE, os.environ)

    def test_a_dist_info_folder_beside_the_package_means_it_was_installed(self) -> None:
        """An installer writes ``dagents-<version>.dist-info`` next to the package, whatever the folder is called."""
        root = self.tree("target")
        (root / "dagents-0.1.0a1.dist-info").mkdir()
        self.assertFalse(env.reads_env_files(root))
        env.load_env_files("env/.env.test", root=root)
        self.assertNotIn(VARIABLE, os.environ)

    def test_this_checkout_reads_its_env_files(self) -> None:
        self.assertTrue(env.reads_env_files())
        self.assertTrue((env.REPO_ROOT / "dagents" / "common" / "env.py").is_file())


if __name__ == "__main__":
    unittest.main()
