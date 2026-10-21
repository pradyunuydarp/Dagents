"""An LMA process killed mid-study keeps its job log and audit chain in Postgres and goes on.

Two LMA processes keep their audit chain, its anchor and their job log in the
tables that ``dagents/lma/README.md`` ("Postgres tables") tells an operator to
create. The test reads the SQL from the README, creates the tables as each
site's role in a scratch database, and starts the GMA (in memory) and the two
LMAs as uvicorn processes with ``start_over_http`` and the ``LMA_DATABASE_URL``,
``LMA_SITE_ROLE``, ``LMA_AUDIT_TABLE``, ``LMA_AUDIT_ANCHOR_TABLE`` and
``LMA_JOB_LOG_TABLE`` settings, as the README names them.

The study runs an evaluation round and a Guard probe at each site. Then site
A's LMA is killed with SIGKILL, so nothing in it runs again, and started on the
same port with the same settings, because the GMA reaches an LMA only at the
address its settings list. After the restart:

- the job log still lists the job from before the kill;
- the audit chain verifies, with the same length, and one more record after a
  new probe;
- a new evaluation round completes with both sites;
- the GMA reports the site unreachable while it is down, and reachable, with
  its audit chain, after the restart.

The LMA is killed between rounds, not while it runs a job. The GMA keeps its
state in memory, and both rounds are evaluation rounds, so no update is
uploaded after the restart. A GMA crash and its recovery are tested in
``test_gma_restart.py`` and ``test_gma_process_restart.py``.

The tests need the real ``dagentsc`` and a PostgreSQL 16 server: set
``DAGENTS_TEST_DATABASE_URL`` to a superuser connection string. Without it
they skip with a message that starts with ``database test skipped``.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dagents.common.domain.federation import RoundRecord
from dagents.tests.fixtures.gma_fixtures import ADMIN_TOKEN, THRESHOLD_VERSION, register_models, toy_manifest
from dagents.tests.test_agent_failures_over_http import probe_request
from dagents.tests.test_agent_network_flow import REPO_ROOT, STAFF_TOKENS, start_over_http
from dagents.tests.test_governance import dagentsc_available
from dagents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip

LMA_README = REPO_ROOT / "dagents" / "lma" / "README.md"

#: The site role and schema the README's example uses.
README_ROLE = "site_north"

#: The two sites with an LMA. The first one is killed.
SITES = ("site-a", "site-b")
KILLED, KEPT = SITES

#: The settings that name the tables, in the order the README lists them.
TABLE_SETTINGS = ("LMA_AUDIT_TABLE", "LMA_AUDIT_ANCHOR_TABLE", "LMA_JOB_LOG_TABLE")


def readme_tables_section() -> str:
    """The README's "Postgres tables" section, up to the next heading of the same level."""
    text = LMA_README.read_text(encoding="utf-8")
    parts = text.split("\n## Postgres tables\n", 1)
    if len(parts) != 2:
        raise AssertionError(f"{LMA_README.relative_to(REPO_ROOT)} has no '## Postgres tables' section")
    return parts[1].split("\n## ", 1)[0]


def readme_table_settings(role: str) -> dict[str, str]:
    """The table settings the README gives for its example role, for ``role`` instead."""
    section = readme_tables_section()
    settings = {}
    for name in TABLE_SETTINGS:
        found = re.search(rf"`{name}=([\w.]+)`", section)
        if found is None:
            raise AssertionError(f"the README's Postgres section does not give {name}")
        settings[name] = re.sub(rf"\b{README_ROLE}\b", role, found.group(1))
    return settings


def readme_table_sql(role: str) -> str:
    """Every ``sql`` block of the README's "Postgres tables" section, for ``role`` instead of its example."""
    blocks = re.findall(r"```sql\n(.*?)```", readme_tables_section(), re.DOTALL)
    if not blocks:
        raise AssertionError("the README's Postgres section has no sql block")
    return re.sub(rf"\b{README_ROLE}\b", role, "\n".join(blocks))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class LmaCrashPostgresTests(unittest.TestCase):
    """Site A's LMA is killed with SIGKILL after a round and started again on the same tables."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        import psycopg  # admin_dsn_or_skip skips when it is missing

        cls.addClassCleanup(cls.db.stop)
        cls.db.start()
        cls.roles = {KILLED: cls.db.alpha, KEPT: cls.db.beta}
        cls.tables = {site: readme_table_settings(role) for site, role in cls.roles.items()}
        with psycopg.connect(cls.db.dsn, autocommit=True) as conn:
            for role in cls.roles.values():
                conn.execute(readme_table_sql(role))

        folder = tempfile.TemporaryDirectory(prefix="dagents-lma-restart-")
        cls.addClassCleanup(folder.cleanup)
        cls.agents = start_over_http(
            Path(folder.name),
            sites=SITES,
            site_settings={
                site: {"LMA_DATABASE_URL": cls.db.dsn, "LMA_SITE_ROLE": role, **cls.tables[site]}
                for site, role in cls.roles.items()
            },
        )
        cls.addClassCleanup(cls.agents.stop)
        cls.run_study()

    # --- The study ------------------------------------------------------------------

    @classmethod
    def staff(cls, site: str) -> Any:
        return cls.agents.network.lma(site, STAFF_TOKENS[site])

    @classmethod
    def evaluation_round(cls, round_id: str) -> RoundRecord:
        gma = cls.agents.network.gma(ADMIN_TOKEN)
        manifest = toy_manifest(
            round_id, "evaluation", THRESHOLD_VERSION, cls.registered[THRESHOLD_VERSION], invited=SITES
        )
        record = gma.dispatch_round(manifest)
        cls.readiness[round_id] = gma.readiness(round_id)
        gma.aggregate(round_id)
        return record

    @classmethod
    def audit(cls) -> dict[str, tuple[bool, int]]:
        return {site: (s.chain_intact, s.record_count) for site in SITES for s in [cls.staff(site).audit_summary()]}

    @classmethod
    def run_study(cls) -> None:
        gma = cls.agents.network.gma(ADMIN_TOKEN)
        cls.registered = register_models(gma)
        cls.readiness: dict[str, Any] = {}
        gma.refresh_sites()

        cls.first = cls.evaluation_round("before-kill-evaluation")
        for site in SITES:
            cls.staff(site).probe(probe_request())
        cls.jobs_before = [e.round_id for e in cls.staff(KILLED).job_log(limit=50)]
        cls.audit_before = cls.audit()

        cls.agents.kill_lma(KILLED)
        cls.directory_while_down = {e.site_id: e.reachable for e in gma.site_summaries()}
        cls.agents.start_lma(KILLED)

        cls.jobs_after_restart = [e.round_id for e in cls.staff(KILLED).job_log(limit=50)]
        cls.audit_after_restart = cls.audit()
        cls.staff(KILLED).probe(probe_request())
        cls.audit_after_probe = cls.audit()

        cls.enrolled_after = [e.site_id for e in gma.refresh_sites()]
        cls.second = cls.evaluation_round("after-restart-evaluation")
        cls.jobs_final = [e.round_id for e in cls.staff(KILLED).job_log(limit=50)]
        cls.audit_final = cls.audit()
        cls.directory_after = gma.site_summaries(include_audit=True)
        cls.rows = cls.count_rows()

    @classmethod
    def count_rows(cls) -> dict[str, dict[str, int]]:
        """How many rows each LMA's tables hold, read with the scratch database's superuser login."""
        import psycopg
        from psycopg import sql

        counts: dict[str, dict[str, int]] = {}
        with psycopg.connect(cls.db.admin_dsn, dbname=cls.db.name, autocommit=True) as conn:
            for site, tables in cls.tables.items():
                counts[site] = {}
                for name, table in tables.items():
                    schema, relation = table.split(".")
                    query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, relation))
                    counts[site][name] = conn.execute(query).fetchone()[0]
        return counts

    # --- What it must show ----------------------------------------------------------

    def test_the_readme_names_three_tables_in_the_role_schema(self) -> None:
        for site, role in self.roles.items():
            with self.subTest(site=site):
                self.assertEqual(
                    self.tables[site],
                    {
                        "LMA_AUDIT_TABLE": f"{role}.lma_audit",
                        "LMA_AUDIT_ANCHOR_TABLE": f"{role}.lma_audit_anchor",
                        "LMA_JOB_LOG_TABLE": f"{role}.lma_jobs",
                    },
                )

    def test_each_lma_keeps_its_job_log_and_audit_chain_in_those_tables(self) -> None:
        for site in SITES:
            with self.subTest(site=site):
                rows = self.rows[site]
                self.assertEqual(rows["LMA_AUDIT_TABLE"], self.audit_final[site][1])
                self.assertEqual(rows["LMA_AUDIT_ANCHOR_TABLE"], 1)
                self.assertEqual(rows["LMA_JOB_LOG_TABLE"], 2)

    def test_the_job_log_lists_the_job_from_before_the_kill(self) -> None:
        self.assertEqual(self.jobs_before, ["before-kill-evaluation"])
        self.assertEqual(self.jobs_after_restart, self.jobs_before)
        self.assertEqual(self.jobs_final, ["after-restart-evaluation", *self.jobs_before])

    def test_the_audit_chain_verifies_after_the_restart_and_grows_from_where_it_stopped(self) -> None:
        intact, before = self.audit_before[KILLED]
        self.assertTrue(intact)
        self.assertGreater(before, 0)
        self.assertEqual(self.audit_after_restart[KILLED], (True, before))
        self.assertEqual(self.audit_after_probe[KILLED], (True, before + 1))
        # The new round added as many records at the restarted site as at the site that kept running.
        added_kept = self.audit_final[KEPT][1] - self.audit_after_probe[KEPT][1]
        self.assertGreater(added_kept, 0)
        self.assertEqual(self.audit_final[KILLED], (True, before + 1 + added_kept))

    def test_a_round_after_the_restart_completes_with_both_sites(self) -> None:
        for record in (self.first, self.second):
            round_id = record.manifest.round_id
            with self.subTest(round=round_id):
                self.assertEqual(record.plan.selected_sites, list(SITES))
                self.assertEqual({r.site_id: r.participation for r in record.results}, {site: "completed" for site in SITES})
                self.assertTrue(self.readiness[round_id].aggregation_permitted)
                self.assertEqual(sorted(self.readiness[round_id].site_weights), list(SITES))

    def test_the_gma_reports_the_site_while_it_is_down_and_again_after_the_restart(self) -> None:
        self.assertEqual(self.directory_while_down, {KILLED: False, KEPT: True})
        self.assertEqual(self.enrolled_after, list(SITES))
        entries = {entry.site_id: entry for entry in self.directory_after}
        self.assertEqual(sorted(entries), list(SITES))
        for site, entry in entries.items():
            with self.subTest(site=site):
                self.assertTrue(entry.reachable)
                assert entry.audit is not None
                self.assertEqual((entry.audit.chain_intact, entry.audit.record_count), self.audit_final[site])


if __name__ == "__main__":
    unittest.main()
