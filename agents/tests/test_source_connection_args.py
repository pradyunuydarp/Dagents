"""What a resolved connection is allowed to tell the Postgres driver.

Pure translation, so these tests need no database and never skip — which
matters, because the Postgres suites that do need one skip in CI, and an
allowlist nothing exercises is an allowlist that quietly stops matching.
"""

from __future__ import annotations

import unittest

from agents.common.infrastructure.sources import _postgres_connection_args


class PostgresConnectionArgsTests(unittest.TestCase):
    def test_it_maps_the_common_aliases(self) -> None:
        args = _postgres_connection_args(
            {"database": "healthcare", "username": "dagents", "password": "x", "host": "db", "port": "6543"}
        )
        self.assertEqual(
            {"dbname": "healthcare", "user": "dagents", "password": "x", "host": "db", "port": 6543},
            args,
        )

    def test_a_connect_timeout_reaches_the_driver(self) -> None:
        """Without this, a host that drops packets holds the caller forever.

        It is the failure mode of a remote managed database: the connection is
        accepted and nothing ever answers. A service whose source adapter can
        hang indefinitely can itself hang indefinitely, which is how a request
        ends with no response rather than with an error.
        """
        args = _postgres_connection_args({"host": "db", "connect_timeout": "10"})
        self.assertEqual(10, args["connect_timeout"])

    def test_unknown_keywords_are_not_passed_through(self) -> None:
        """The allowlist is the point: a payload must not steer the driver."""
        args = _postgres_connection_args(
            {"host": "db", "options": "-c statement_timeout=0", "passfile": "/etc/passwd"}
        )
        self.assertEqual({"host": "db"}, args)

    def test_empty_values_are_dropped_rather_than_sent_as_blank(self) -> None:
        """A blank host is not a host; libpq would read it as "use the default"."""
        self.assertEqual({}, _postgres_connection_args({"host": "", "port": None, "user": ""}))


if __name__ == "__main__":
    unittest.main()
