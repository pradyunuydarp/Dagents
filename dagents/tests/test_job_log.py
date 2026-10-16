"""The LMA's job log: what a site recorded about each federated job.

Two implementations run the same assertions:

- ``InMemoryJobLog``, which never skips;
- ``PostgresJobLog``, against a real PostgreSQL 16 server, with the scratch
  database and roles of ``test_postgres_roles.py``. The table has the columns,
  grants and row-level security of the healthcare demo's
  ``coordinator.site_contributions`` (migration ``0002``), so each site role
  reads and writes only its own rows. The class skips with
  ``database test skipped`` when ``DAGENTS_TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

from datetime import datetime
import threading
import unittest

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:  # pragma: no cover - the agents' requirements install it
    psycopg = None

from dagents.common.domain.errors import StoreNotSetUpError
from dagents.common.domain.site import JobLogEntry
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.lma.infrastructure.job_log import InMemoryJobLog, PostgresJobLog
from dagents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip


def entry(round_id: str = "study-training-1", **overrides) -> JobLogEntry:
    values = {
        "round_id": round_id,
        "phase": "training",
        "participation": "completed",
        "contributed_examples": 24,
        "update_norm": 0.75,
        "update_digest": "sha256:" + "ab" * 32,
        "job_digest": "sha256:" + "cd" * 32,
        "metrics": {"train_loss": 0.51, "record_count": 24.0},
    }
    values.update(overrides)
    return JobLogEntry(**values)


class JobLogContract:
    """What every job log does. A subclass sets ``self.log``."""

    log: InMemoryJobLog | PostgresJobLog

    def test_append_sets_the_time_and_keeps_every_field(self) -> None:
        sent = entry(reason=None)
        stored = self.log.append(sent)
        self.assertIsNotNone(stored.submitted_at)
        datetime.fromisoformat(stored.submitted_at)
        self.assertEqual(sent.model_dump(exclude={"submitted_at"}), stored.model_dump(exclude={"submitted_at"}))
        [read] = self.log.recent(5)
        self.assertEqual(stored, read)

    def test_a_refusal_keeps_its_reason_and_no_update(self) -> None:
        refused = entry(
            "study-evaluation-1",
            phase="evaluation",
            participation="rejected",
            contributed_examples=0,
            update_norm=None,
            update_digest=None,
            metrics={},
            reason="the round's code digest is not the code this site runs",
        )
        self.log.append(refused)
        [read] = self.log.recent(1)
        self.assertEqual("rejected", read.participation)
        self.assertEqual("the round's code digest is not the code this site runs", read.reason)
        self.assertIsNone(read.update_norm)
        self.assertIsNone(read.update_digest)
        self.assertEqual({}, read.metrics)

    def test_recent_is_newest_first_and_at_most_the_limit(self) -> None:
        for index in range(5):
            self.log.append(entry(f"round-{index}"))
        self.assertEqual(["round-4", "round-3", "round-2"], [item.round_id for item in self.log.recent(3)])
        self.assertEqual(5, len(self.log.recent(20)))

    def test_an_empty_log_reads_as_an_empty_list(self) -> None:
        self.assertEqual([], self.log.recent(20))

    def test_a_limit_below_one_is_refused(self) -> None:
        for limit in (0, -1, True, 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.log.recent(limit)  # type: ignore[arg-type]

    def test_returned_entries_are_copies(self) -> None:
        self.log.append(entry())
        self.log.recent(1)[0].metrics["train_loss"] = 99.0
        self.assertEqual(0.51, self.log.recent(1)[0].metrics["train_loss"])

    def test_concurrent_appends_keep_every_entry(self) -> None:
        threads = [threading.Thread(target=self.log.append, args=(entry(f"round-{index}"),)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            sorted(f"round-{index}" for index in range(20)), sorted(item.round_id for item in self.log.recent(100))
        )


class InMemoryJobLogTests(JobLogContract, unittest.TestCase):
    def setUp(self) -> None:
        self.log = InMemoryJobLog()

    def test_the_log_keeps_at_most_its_capacity(self) -> None:
        log = InMemoryJobLog(capacity=3)
        for index in range(5):
            log.append(entry(f"round-{index}"))
        self.assertEqual(["round-4", "round-3", "round-2"], [item.round_id for item in log.recent(20)])


#: The table the healthcare demo's LMAs write to, without the coordinator's
#: own grants: ``coordinator.site_contributions`` from migration ``0002``.
JOB_TABLE = """
CREATE TABLE {table} (
    contribution_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id        text NOT NULL CHECK (length(round_id) BETWEEN 1 AND 200),
    phase           text NOT NULL CHECK (length(phase) BETWEEN 1 AND 64),
    site_role       name NOT NULL DEFAULT current_user,
    payload         jsonb NOT NULL,
    submitted_at    timestamptz NOT NULL DEFAULT now()
)
"""


class PostgresJobLogTestCase(unittest.TestCase):
    """A job table owned by tenant alpha, which two site roles share under row-level security.

    ``beta`` and ``gamma`` are the two sites. ``delta`` has no grant on the
    table.
    """

    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        cls.extra_roles: list[str] = []
        try:
            cls.db.start()
            cls.gamma = cls.db.alpha.replace("_alpha", "_gamma")
            cls.delta = cls.db.alpha.replace("_alpha", "_delta")
            cls.table = f"{cls.db.alpha}.site_jobs"
            identifier = sql.Identifier(cls.db.alpha, "site_jobs")
            sites = sql.SQL(", ").join(sql.Identifier(role) for role in (cls.db.beta, cls.gamma))
            with psycopg.connect(cls.db.dsn, autocommit=True) as conn:
                for role in (cls.gamma, cls.delta):
                    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))
                    cls.extra_roles.append(role)
                    conn.execute(
                        sql.SQL("GRANT {} TO current_user WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(role))
                    )
                with conn.transaction():
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(cls.db.alpha)))
                    conn.execute(sql.SQL(JOB_TABLE).format(table=identifier))
                    conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(identifier))
                    conn.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(cls.db.alpha), sites)
                    )
                    conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(identifier, sites))
                    conn.execute(
                        sql.SQL("GRANT INSERT (round_id, phase, payload) ON {} TO {}").format(identifier, sites)
                    )
                    conn.execute(
                        sql.SQL(
                            "CREATE POLICY site_jobs_insert_own ON {} FOR INSERT TO {} WITH CHECK (site_role = current_user)"
                        ).format(identifier, sites)
                    )
                    conn.execute(
                        sql.SQL(
                            "CREATE POLICY site_jobs_select_own ON {} FOR SELECT TO {} USING (site_role = current_user)"
                        ).format(identifier, sites)
                    )
        except BaseException:
            cls._drop()
            raise

    @classmethod
    def _drop(cls) -> None:
        cls.db.stop()
        if cls.extra_roles:
            with psycopg.connect(cls.db.admin_dsn, autocommit=True) as admin:
                for role in cls.extra_roles:
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._drop()

    def setUp(self) -> None:
        self.lease = KeptConnection(self.db.dsn, name="site one")
        self.addCleanup(self.lease.close)
        with self.lease.acting_as(self.db.alpha) as conn:
            conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(self.db.alpha, "site_jobs")))
        self.log = PostgresJobLog(self.lease, self.db.beta, self.table)


class PostgresJobLogTests(JobLogContract, PostgresJobLogTestCase):
    def test_rows_hold_the_payload_keys_the_demo_stores(self) -> None:
        """The payload has the keys the healthcare demo writes today, and ``reason`` only when there is one."""
        self.log.append(entry("round-a"))
        self.log.append(entry("round-b", participation="rejected", reason="no"))
        with self.lease.acting_as(self.db.alpha) as conn:
            rows = conn.execute(
                sql.SQL("SELECT round_id, phase, site_role, payload FROM {} ORDER BY contribution_id").format(
                    sql.Identifier(self.db.alpha, "site_jobs")
                )
            ).fetchall()
        keys = {"participation", "contributed_examples", "update_norm", "update_digest", "metrics", "job_digest"}
        self.assertEqual(keys, set(rows[0]["payload"]))
        self.assertEqual(keys | {"reason"}, set(rows[1]["payload"]))
        self.assertEqual(["training", "training"], [row["phase"] for row in rows])
        self.assertEqual({self.db.beta}, {row["site_role"] for row in rows})

    def test_each_site_reads_only_its_own_rows(self) -> None:
        other = PostgresJobLog(self.lease, self.gamma, self.table)
        self.log.append(entry("beta-round"))
        other.append(entry("gamma-round"))
        self.assertEqual(["beta-round"], [item.round_id for item in self.log.recent(20)])
        self.assertEqual(["gamma-round"], [item.round_id for item in other.recent(20)])

    def test_rows_written_by_the_demo_are_read(self) -> None:
        """A row the healthcare demo wrote before the LMA had a job log reads as an entry."""
        from psycopg.types.json import Jsonb

        payload = {
            "participation": "completed",
            "contributed_examples": 12,
            "update_norm": None,
            "update_digest": None,
            "metrics": {"record_count": 12.0, "completeness": 1.0},
            "job_digest": "sha256:" + "ee" * 32,
        }
        with self.lease.acting_as(self.db.beta) as conn:
            conn.execute(
                sql.SQL("INSERT INTO {} (round_id, phase, payload) VALUES (%s, %s, %s)").format(
                    sql.Identifier(self.db.alpha, "site_jobs")
                ),
                ("older-analytics", "analytics", Jsonb(payload)),
            )
        [read] = self.log.recent(5)
        self.assertEqual("older-analytics", read.round_id)
        self.assertEqual("analytics", read.phase)
        self.assertEqual(12, read.contributed_examples)
        self.assertIsNone(read.reason)

    def test_a_row_that_cannot_be_read_is_left_out(self) -> None:
        from psycopg.types.json import Jsonb

        self.log.append(entry("good"))
        with self.lease.acting_as(self.db.beta) as conn:
            conn.execute(
                sql.SQL("INSERT INTO {} (round_id, phase, payload) VALUES (%s, %s, %s)").format(
                    sql.Identifier(self.db.alpha, "site_jobs")
                ),
                ("bad", "not-a-phase", Jsonb({"participation": "completed"})),
            )
        self.assertEqual(["good"], [item.round_id for item in self.log.recent(5)])

    def test_a_role_without_a_grant_gets_a_store_error_without_connection_details(self) -> None:
        stranger = PostgresJobLog(self.lease, self.delta, self.table)
        with self.assertRaises(StoreNotSetUpError) as raised:
            stranger.recent(5)
        self.assertNotIn(self.db.dsn, str(raised.exception))
        with self.assertRaises(StoreNotSetUpError):
            stranger.append(entry())

    def test_a_table_name_that_is_not_a_plain_identifier_is_refused(self) -> None:
        for table in ("jobs; DROP TABLE x", "A.jobs", "a.b.c", ""):
            with self.subTest(table=table), self.assertRaises(ValueError):
                PostgresJobLog(self.lease, self.db.beta, table)

    def test_a_role_name_that_is_not_valid_is_refused(self) -> None:
        with self.assertRaises(RoleScopeError):
            PostgresJobLog(self.lease, "beta; DROP TABLE x", self.table)


if __name__ == "__main__":
    unittest.main()
