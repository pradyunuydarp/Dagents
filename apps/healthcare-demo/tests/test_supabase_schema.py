"""The per-hospital schema matches the feature contract, the stores and the RLS rules.

These checks read the migration as text, so they need no database. The
database tests in ``test_site_isolation.py`` apply the same file to a real
server. Things that are easy to break here and hard to notice:

- A contract field with no column. The site would never store it, and a round
  would measure less than it claims.
- The store's rules and the table's check constraints disagreeing. The
  in-memory store would then accept a record the database refuses.
- A policy, grant or missing revoke that lets Supabase's Data API roles
  (``anon``, ``authenticated``, ``service_role``) read patient-level rows. The
  browser must never reach the database; the API does, and the Ethical Guard
  decides what leaves.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))
sys.path.insert(0, str(APP_ROOT / "scripts"))

from dagents.common.infrastructure.postgres_audit import ANCHOR_COLUMNS  # noqa: E402
from dagents.common.infrastructure.postgres_audit import COLUMNS as AUDIT_COLUMNS  # noqa: E402
from app.domain.conditions import STROKE_FEATURE_CONTRACT  # noqa: E402
from app.domain.dataset import JOINING_SITES, SITES  # noqa: E402
from app.services import site_store  # noqa: E402

MIGRATIONS = APP_ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "0002_site_isolation.sql"
SEED = APP_ROOT / "scripts" / "seed_supabase.py"
APPLY = APP_ROOT / "scripts" / "apply_migrations.py"

#: The v3 contract fields from the design spec. Used only while conditions.py
#: still holds v2; once it holds v3 or later, the contract itself is read.
V3_FIELDS = (
    "age_band",
    "sex",
    "conscious_level",
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "hours_since_onset",
    "systolic_bp",
    "visual_field_loss",
    "atrial_fibrillation",
    "onset_on_waking",
    "stroke_subtype",
    "poor_outcome_6m",
)


def contract_fields() -> tuple[str, ...]:
    if STROKE_FEATURE_CONTRACT.version == "stroke-triage-features-v2":
        return V3_FIELDS
    return tuple(field.name for field in STROKE_FEATURE_CONTRACT.fields)


def migration_sql() -> str:
    if not MIGRATION.is_file():
        raise AssertionError(f"No migration at {MIGRATION}")
    return MIGRATION.read_text(encoding="utf-8")


def normalised(text: str) -> str:
    """Lower case, comments removed, whitespace collapsed."""
    without_comments = re.sub(r"--[^\n]*", "", text)
    return " ".join(without_comments.lower().split())


def table_block(sql: str, table: str) -> str:
    """The body of one create-table statement in the per-site loop."""
    match = re.search(
        rf"create table if not exists %I\.{table} \((.*?)\)\$ddl\$", sql, re.DOTALL | re.IGNORECASE
    )
    if match is None:
        raise AssertionError(f"Could not find the {table} table in the migration")
    return re.sub(r"--[^\n]*", "", match.group(1))


def top_level_items(block: str) -> list[str]:
    """Split a create-table body at commas outside parentheses and quotes."""
    items, depth, quoted, current = [], 0, False, []
    for char in block:
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        if char == "," and depth == 0 and not quoted:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return [" ".join(item.split()) for item in items if item.strip()]


def columns_of(block: str) -> dict[str, str]:
    """Column name to its definition."""
    columns = {}
    for item in top_level_items(block):
        name = item.split()[0]
        if name.lower() in {"constraint", "check", "unique", "primary", "foreign"}:
            continue
        columns[name] = item
    return columns


class SitesMatchTheDatasetTests(unittest.TestCase):
    def test_the_migration_creates_one_schema_per_demo_hospital(self) -> None:
        match = re.search(r"sites\s+text\[\]\s*:=\s*array\[([^\]]*)\]", migration_sql())
        self.assertIsNotNone(match, "the migration's site list was not found")
        listed = re.findall(r"'([a-z0-9_]+)'", match.group(1))
        self.assertEqual([site.schema for site in SITES], listed)
        for site in SITES:
            self.assertEqual(site.schema, site.role)


class EncounterColumnsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.columns = columns_of(table_block(migration_sql(), "encounters"))

    def test_the_columns_are_the_ones_the_store_reads_and_writes(self) -> None:
        self.assertEqual(list(site_store.TABLE_COLUMNS), list(self.columns))

    def test_every_contract_field_has_a_column(self) -> None:
        missing = sorted(set(contract_fields()) - set(self.columns))
        self.assertEqual([], missing, f"{missing} are in the contract but have no column")

    def test_contract_fields_are_nullable(self) -> None:
        """Real records have gaps; the data-quality checks need to see them."""
        offenders = [name for name in contract_fields() if "not null" in self.columns[name].lower()]
        self.assertEqual([], offenders)

    def test_the_check_constraints_match_the_store_rules(self) -> None:
        for name, allowed in site_store.CATEGORIES.items():
            with self.subTest(column=name):
                match = re.search(rf"check \({name} in \(([^)]*)\)\)", self.columns[name])
                self.assertIsNotNone(match, f"{name} has no list of allowed values")
                self.assertEqual(list(allowed), re.findall(r"'([^']*)'", match.group(1)))
        for name, (low, high) in site_store.RANGES.items():
            with self.subTest(column=name):
                match = re.search(rf"check \({name} between (\S+) and (\S+)\)", self.columns[name])
                self.assertIsNotNone(match, f"{name} has no range check")
                self.assertEqual((float(low), float(high)), (float(match.group(1)), float(match.group(2))))

    def test_the_column_types_match_the_store(self) -> None:
        for name in site_store.BOOLEAN_FIELDS:
            self.assertIn(" boolean", self.columns[name])
        for name in site_store.REAL_FIELDS:
            self.assertIn(" double precision", self.columns[name], "real would round the stored floats")
        for name in site_store.INTEGER_FIELDS:
            self.assertIn(" integer", self.columns[name])

    def test_entries_are_idempotent_and_marked(self) -> None:
        self.assertIn("unique", self.columns["client_request_id"])
        self.assertIn("check (origin in ('dataset', 'entered'))", self.columns["origin"])
        self.assertIn("encounters_origin_matches_row", normalised(table_block(migration_sql(), "encounters")))


class AuditTableTests(unittest.TestCase):
    def test_the_audit_table_has_the_sinks_columns(self) -> None:
        columns = columns_of(table_block(migration_sql(), "guard_audit"))
        self.assertEqual(list(AUDIT_COLUMNS), list(columns))
        self.assertIn("unique", columns["previous_digest"], "a unique previous digest stops the chain forking")


class RowLevelSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = normalised(migration_sql())

    def test_rls_is_enabled_on_every_table(self) -> None:
        for statement in (
            "alter table %i.encounters enable row level security",
            "alter table %i.guard_audit enable row level security",
            "alter table coordinator.models enable row level security",
            "alter table coordinator.site_contributions enable row level security",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)

    def test_no_policy_names_a_data_api_role_or_public(self) -> None:
        policies = re.findall(r"create policy(.*?)(?:'\s*,|;)", self.sql)
        self.assertEqual(2, len(policies), policies)
        for policy in policies:
            for role in ("anon", "authenticated", "service_role", "public"):
                self.assertNotIn(role, policy)

    def test_the_revokes_are_guarded_by_role_existence(self) -> None:
        self.assertIn("api_roles text[] := array['anon', 'authenticated', 'service_role']", self.sql)
        self.assertIn("foreach r in array api_roles loop if exists (select 1 from pg_roles where rolname = r)", self.sql)
        for statement in (
            "revoke all on schema %i from %i",
            "revoke all on all tables in schema %i from %i",
            "revoke all on schema coordinator from %i",
            "revoke all on all tables in schema coordinator from %i",
            "revoke all on schema %i from public",
            "revoke all on schema coordinator from public",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)


class RolesAndVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = normalised(migration_sql())

    def test_servers_older_than_15_are_refused(self) -> None:
        self.assertIn("current_setting('server_version_num')::int < 150000", self.sql)
        self.assertNotIn("< 160000", self.sql)

    def test_the_connecting_role_may_switch_but_does_not_inherit_on_16(self) -> None:
        self.assertIn(
            "grant_options constant boolean := current_setting('server_version_num')::int >= 160000", self.sql
        )
        self.assertIn(
            "if grant_options then execute format('grant %i to current_user with inherit false, set true', r); "
            "else execute format('grant %i to current_user', r); end if;",
            self.sql,
        )
        self.assertIn("create role %i nologin noinherit nobypassrls", self.sql)

    def test_every_grant_to_the_connecting_role_is_one_of_the_two_branches(self) -> None:
        """A third grant would inherit on 16 too, and nothing else would notice."""
        grants = re.findall(r"grant %i to current_user[^']*", self.sql)
        self.assertEqual(["grant %i to current_user with inherit false, set true", "grant %i to current_user"], grants)

    def test_the_15_branch_says_what_is_lost(self) -> None:
        comments = migration_sql().lower()
        self.assertIn("postgresql 15", comments)
        self.assertIn("inherits", comments)
        self.assertIn("pg_read_all_data", comments)

    def test_the_release_decision_columns_are_added_to_an_existing_table(self) -> None:
        """An earlier version of this file created coordinator.models without them."""
        for statement in (
            "alter table coordinator.models add column if not exists release_decision jsonb",
            "alter table coordinator.models add column if not exists decided_at timestamptz",
            "conname = 'models_release_decision_check'",
            "add constraint models_release_decision_check check (release_decision is null or (",
            "conname = 'models_decision_has_time'",
            "add constraint models_decision_has_time check ((release_decision is null) = (decided_at is null))",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)
        create = re.search(r"create table if not exists coordinator\.models \((.*?)\);", self.sql)
        self.assertIsNotNone(create)
        self.assertNotIn("release_decision", create.group(1))
        self.assertNotIn("decided_at", create.group(1))

    def test_there_is_no_session_level_set_role(self) -> None:
        """SET ROLE without LOCAL outlives the transaction and leaks through poolers."""
        self.assertEqual([], re.findall(r"(?<!local )\bset role\b", self.sql))

    def test_the_v2_tables_are_kept_for_the_api_that_is_still_running(self) -> None:
        """The previous API reads public.encounters and public.hospitals until the new one is deployed.

        The data workflow applies the migrations and the seed before the merge,
        while that API still serves the published page. A later migration drops
        the tables.
        """
        for path in sorted(MIGRATIONS.glob("*.sql")):
            text = normalised(path.read_text(encoding="utf-8"))
            with self.subTest(migration=path.name):
                self.assertNotIn("drop table", text)
                self.assertNotIn("drop schema", text)
                self.assertNotRegex(text, r"(alter|revoke|grant)[^;]* public\.(encounters|hospitals)\b"
                                    if path.name != "0001_stroke_encounters.sql" else r"$^")

    def test_the_emulation_script_is_not_a_migration(self) -> None:
        names = [path.name for path in MIGRATIONS.glob("*.sql")]
        self.assertTrue(all(re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", name) for name in names), names)
        self.assertNotIn("emulate_supabase_roles.sql", names)


COORDINATOR_MIGRATION = MIGRATIONS / "0003_coordinator_records.sql"


def coordinator_table_block(table: str) -> str:
    """The body of one create-table statement in 0003."""
    text = COORDINATOR_MIGRATION.read_text(encoding="utf-8")
    match = re.search(rf"create table if not exists coordinator\.{table} \((.*?)\n    \);", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"Could not find coordinator.{table} in {COORDINATOR_MIGRATION.name}")
    return re.sub(r"--[^\n]*", "", match.group(1))


class CoordinatorRecordsMigrationTests(unittest.TestCase):
    """0003 adds the pilot reports and the coordinator's audit chain."""

    def setUp(self) -> None:
        self.sql = normalised(COORDINATOR_MIGRATION.read_text(encoding="utf-8"))

    def test_it_runs_after_0002(self) -> None:
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertLess(names.index("0002_site_isolation.sql"), names.index(COORDINATOR_MIGRATION.name))
        self.assertIn("apply 0002_site_isolation.sql first", self.sql)

    def test_the_audit_table_has_the_sinks_columns(self) -> None:
        columns = columns_of(coordinator_table_block("guard_audit"))
        self.assertEqual(list(AUDIT_COLUMNS), list(columns))
        self.assertEqual(
            columns_of(table_block(migration_sql(), "guard_audit")),
            columns,
            "the coordinator's chain has the same columns and constraints as each hospital's",
        )

    def test_a_pilot_report_is_an_object_keyed_by_its_pilot(self) -> None:
        columns = columns_of(coordinator_table_block("pilot_reports"))
        self.assertEqual(["seq", "pilot_id", "report", "created_at"], list(columns))
        self.assertIn("primary key", columns["pilot_id"])
        self.assertIn("check (jsonb_typeof(report) = 'object')", columns["report"])
        self.assertIn("unique", columns["seq"])

    def test_the_coordinator_owns_both_tables_and_rls_is_on(self) -> None:
        self.assertIn("coordinator_role constant text := 'dagents_coordinator'", self.sql)
        self.assertIn("execute format('set local role %i', coordinator_role)", self.sql)
        for statement in (
            "alter table coordinator.pilot_reports enable row level security",
            "alter table coordinator.guard_audit enable row level security",
            "revoke all on coordinator.pilot_reports from public",
            "revoke all on coordinator.guard_audit from public",
            "execute format('revoke all on coordinator.pilot_reports from %i', r)",
            "execute format('revoke all on coordinator.guard_audit from %i', r)",
            "api_roles text[] := array['anon', 'authenticated', 'service_role']",
            "foreach r in array api_roles loop if exists (select 1 from pg_roles where rolname = r)",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)
        self.assertNotIn("create policy", self.sql)
        self.assertNotRegex(self.sql, r"\bgrant\b")

    def test_each_chain_has_an_anchor_table_owned_by_its_role(self) -> None:
        text = COORDINATOR_MIGRATION.read_text(encoding="utf-8")
        match = re.search(r"sites\s+text\[\]\s*:=\s*array\[([^\]]*)\]", text)
        self.assertIsNotNone(match, "0003's site list was not found")
        self.assertEqual([site.schema for site in SITES], re.findall(r"'([a-z0-9_]+)'", match.group(1)))
        site_anchor = columns_of(table_block(text, "guard_audit_anchor"))
        self.assertEqual(list(ANCHOR_COLUMNS), list(site_anchor))
        self.assertEqual(site_anchor, columns_of(coordinator_table_block("guard_audit_anchor")))
        self.assertIn("check (id)", site_anchor["id"])
        self.assertIn("record_count between 1 and seq", site_anchor["record_count"])
        for statement in (
            "foreach s in array sites loop execute format('set local role %i', s);",
            "execute format('alter table %i.guard_audit_anchor enable row level security', s)",
            "execute format('revoke all on %i.guard_audit_anchor from public', s)",
            "execute format('revoke all on %i.guard_audit_anchor from %i', s, r)",
            "alter table coordinator.guard_audit_anchor enable row level security",
            "revoke all on coordinator.guard_audit_anchor from public",
            "execute format('revoke all on coordinator.guard_audit_anchor from %i', r)",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)

    def test_it_is_safe_to_run_twice_and_sets_no_session_role(self) -> None:
        self.assertEqual(4, self.sql.count("create table if not exists"))
        self.assertNotRegex(self.sql, r"create table (?!if not exists)")
        self.assertEqual([], re.findall(r"(?<!local )\bset role\b", self.sql))

    def test_the_store_keeps_as_many_reports_as_the_comment_says(self) -> None:
        comments = " ".join(COORDINATOR_MIGRATION.read_text(encoding="utf-8").replace("--", " ").split())
        self.assertIn(f"keeps the newest {site_store.PILOT_REPORTS_KEPT}", comments)


JOINING_MIGRATION = MIGRATIONS / "0004_joining_site.sql"


def literal_list(text: str, name: str) -> list[str]:
    """The quoted names in ``<name> text[] := array[...]`` of a migration."""
    match = re.search(rf"\b{name}\s+text\[\]\s*:=\s*array\[([^\]]*)\]", text)
    if match is None:
        raise AssertionError(f"the list {name} was not found")
    return re.findall(r"'([a-z0-9_]+)'", match.group(1))


class JoiningSiteMigrationTests(unittest.TestCase):
    """0004 gives each hospital that joins while the demo runs what 0002 and 0003 give the others."""

    def setUp(self) -> None:
        self.text = JOINING_MIGRATION.read_text(encoding="utf-8")
        self.sql = normalised(self.text)

    def test_it_runs_after_0003_and_refuses_to_run_before_it(self) -> None:
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertLess(names.index(COORDINATOR_MIGRATION.name), names.index(JOINING_MIGRATION.name))
        self.assertIn("apply 0003_coordinator_records.sql first", self.sql)

    def test_it_creates_the_joining_hospitals_and_names_every_hospital_in_the_policies(self) -> None:
        self.assertEqual([site.schema for site in JOINING_SITES], literal_list(self.text, "sites"))
        self.assertEqual(
            [site.schema for site in (*SITES, *JOINING_SITES)], literal_list(self.text, "all_sites")
        )
        for site in JOINING_SITES:
            self.assertEqual(site.schema, site.role)

    def test_its_tables_are_the_ones_0002_and_0003_create_for_every_hospital(self) -> None:
        for table in ("encounters", "guard_audit"):
            with self.subTest(table=table):
                self.assertEqual(columns_of(table_block(migration_sql(), table)), columns_of(table_block(self.text, table)))
        self.assertEqual(
            columns_of(table_block(COORDINATOR_MIGRATION.read_text(encoding="utf-8"), "guard_audit_anchor")),
            columns_of(table_block(self.text, "guard_audit_anchor")),
        )

    def test_rls_revokes_and_the_coordinator_grants_match_0002(self) -> None:
        for statement in (
            "alter table %i.encounters enable row level security",
            "alter table %i.guard_audit enable row level security",
            "alter table %i.guard_audit_anchor enable row level security",
            "revoke all on schema %i from public",
            "revoke all on all tables in schema %i from public",
            "revoke all on schema %i from %i",
            "revoke all on all tables in schema %i from %i",
            "revoke all on all sequences in schema %i from %i",
            "api_roles text[] := array['anon', 'authenticated', 'service_role']",
            "foreach r in array api_roles loop if exists (select 1 from pg_roles where rolname = r)",
            "'grant usage on schema coordinator to %s'",
            "'grant select on coordinator.site_contributions to %s'",
            "'grant insert (round_id, phase, payload) on coordinator.site_contributions to %s'",
            "'alter policy site_contributions_insert_own on coordinator.site_contributions to %s'",
            "'alter policy site_contributions_select_own on coordinator.site_contributions to %s'",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.sql)
        self.assertNotIn("create policy", self.sql)

    def test_the_coordinator_gets_nothing_on_a_joining_hospital(self) -> None:
        self.assertNotRegex(self.sql, r"grant [^;']* to %i', [a-z_]*, coordinator_role")
        self.assertNotIn("to dagents_coordinator", self.sql)

    def test_it_is_safe_to_run_twice_and_sets_no_session_role(self) -> None:
        self.assertNotRegex(self.sql, r"create table (?!if not exists)")
        self.assertNotRegex(self.sql, r"create schema (?!if not exists)")
        self.assertIn("if not exists (select 1 from pg_roles where rolname = r) then", self.sql)
        self.assertEqual([], re.findall(r"(?<!local )\bset role\b", self.sql))

    def test_it_has_both_branches_of_the_role_grant(self) -> None:
        self.assertIn("current_setting('server_version_num')::int >= 160000", self.sql)
        self.assertIn("'grant %i to current_user with inherit false, set true'", self.sql)
        self.assertIn("'grant %i to current_user'", self.sql)


class ScriptsTests(unittest.TestCase):
    def test_the_seed_reads_its_credentials_from_the_environment(self) -> None:
        source = SEED.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SUPABASE_DB_URL"', source)
        self.assertNotRegex(source, r"postgres(ql)?://[^.\s\"']+[:@]")

    def test_the_seed_loads_the_extract_and_acts_as_each_site(self) -> None:
        source = SEED.read_text(encoding="utf-8")
        self.assertIn("load_site_records", source)
        self.assertIn("JOINING_SITES", source, "the joining hospitals are seeded with the others")
        self.assertIn("PostgresSiteStore", source)
        self.assertNotIn("app.domain.synthetic", source)

    def test_the_migration_ledger_is_closed_to_the_data_api(self) -> None:
        ledger = normalised(APPLY.read_text(encoding="utf-8"))
        self.assertIn("alter table public.schema_migrations enable row level security", ledger)

    def test_no_file_claims_the_api_uses_the_service_role_key(self) -> None:
        """It connects as the project's login role, not with an API key."""
        for path in (MIGRATION, SEED, APPLY):
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8").lower(), r"service[- ]role key")


if __name__ == "__main__":
    unittest.main()
