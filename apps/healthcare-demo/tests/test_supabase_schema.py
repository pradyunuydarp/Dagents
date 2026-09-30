"""The database schema must not drift from the feature contract, or from RLS.

Two things are easy to break here and expensive to notice:

- **A contract field with no column.** The consortium's agreement is that every
  site derives the same fields with the same units. A field added to
  `stroke-triage-features-v2` but not to the migration would not fail: the site
  would simply never store it, and the round would measure something narrower
  than it claimed to.
- **Row Level Security quietly opened.** The tables deny the anon role on
  purpose. The browser never talks to Supabase; the API does, and the Ethical
  Guard decides what leaves. An anon read policy would route patient-level rows
  past the Guard straight to the browser and defeat the whole demo — while
  making the app look like it was working better.

Neither check needs a database.
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

from app.domain.conditions import STROKE_FEATURE_CONTRACT  # noqa: E402

MIGRATION = APP_ROOT / "supabase" / "migrations" / "0001_stroke_encounters.sql"


def migration_sql() -> str:
    """The migration text, failing loudly if it has moved."""
    if not MIGRATION.is_file():
        raise AssertionError(f"No migration at {MIGRATION}")
    return MIGRATION.read_text(encoding="utf-8")


def encounter_columns(sql: str) -> set[str]:
    """Column names declared on `public.encounters`.

    Parses the one create-table block rather than the whole file, so a column
    named in a comment or another table cannot make this pass by accident.
    """
    match = re.search(
        r"create table if not exists public\.encounters\s*\((.*?)\n\);", sql, re.DOTALL
    )
    if match is None:
        raise AssertionError("Could not find the encounters table in the migration")
    columns: set[str] = set()
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        name = stripped.split()[0]
        if name in {"primary", "foreign", "constraint", "unique", "check"}:
            continue
        columns.add(name)
    if len(columns) < 5:
        raise AssertionError(f"Only parsed {sorted(columns)}; the parser is broken")
    return columns


class SchemaMatchesContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = migration_sql()
        self.columns = encounter_columns(self.sql)

    def test_every_contract_field_has_a_column(self) -> None:
        """A field the sites agreed to derive has somewhere to be stored."""
        missing = sorted(
            field.name for field in STROKE_FEATURE_CONTRACT.fields if field.name not in self.columns
        )
        self.assertEqual(
            [],
            missing,
            f"{missing} are in {STROKE_FEATURE_CONTRACT.version} but have no column. "
            "Add them to the migration, or they will never be stored.",
        )

    def test_the_seed_writes_every_contract_field(self) -> None:
        """A column nothing seeds is a column that is always NULL."""
        from seed_supabase import ENCOUNTER_COLUMNS

        missing = sorted(
            field.name
            for field in STROKE_FEATURE_CONTRACT.fields
            if field.name not in ENCOUNTER_COLUMNS
        )
        self.assertEqual([], missing, f"the seed never writes {missing}")

    def test_the_seed_only_writes_columns_that_exist(self) -> None:
        from seed_supabase import ENCOUNTER_COLUMNS

        unknown = sorted(set(ENCOUNTER_COLUMNS) - self.columns)
        self.assertEqual([], unknown, f"the seed writes columns the schema lacks: {unknown}")

    def test_required_contract_fields_are_nullable(self) -> None:
        """Real records have gaps, and the demo needs them to.

        A NOT NULL on a required field would move the contract check into the
        database and make the missingness path unreachable — the site would
        fail to insert rather than store an incomplete record for the
        data-quality check to find.
        """
        block = re.search(
            r"create table if not exists public\.encounters\s*\((.*?)\n\);", self.sql, re.DOTALL
        )
        assert block is not None
        contract_fields = {field.name for field in STROKE_FEATURE_CONTRACT.fields}
        offenders = []
        for line in block.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            name = stripped.split()[0]
            if name in contract_fields and "not null" in stripped.lower():
                offenders.append(name)
        self.assertEqual([], offenders, f"these contract fields are NOT NULL: {offenders}")


class RowLevelSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = migration_sql()

    def test_rls_is_enabled_on_both_tables(self) -> None:
        for table in ("public.hospitals", "public.encounters"):
            with self.subTest(table=table):
                self.assertIn(
                    f"alter table {table} enable row level security",
                    " ".join(self.sql.lower().split()),
                    f"RLS is not enabled on {table}",
                )

    def test_no_policy_grants_the_anon_role_access(self) -> None:
        """The publishable key must read nothing.

        It ships in the browser bundle. A policy letting it read `encounters`
        would hand patient-level rows to anyone who opened the page, with the
        Guard never consulted.
        """
        policies = re.findall(r"create\s+policy(.*?);", self.sql, re.DOTALL | re.IGNORECASE)
        offenders = [p.strip()[:80] for p in policies if "anon" in p.lower()]
        self.assertEqual([], offenders, f"a policy grants anon access: {offenders}")

    def test_privileges_are_revoked_from_the_public_roles(self) -> None:
        """Both browser-reachable roles lose access to both tables.

        The revokes are guarded by a role-exists check so the migration also
        runs on a plain Postgres, so this looks for each table/role pair rather
        than one combined statement.
        """
        normalised = " ".join(self.sql.lower().split())
        for table in ("public.hospitals", "public.encounters"):
            for role in ("anon", "authenticated"):
                with self.subTest(table=table, role=role):
                    self.assertIn(f"revoke all on {table} from {role}", normalised)

    def test_the_revokes_are_guarded_so_the_migration_runs_anywhere(self) -> None:
        """`anon` and `authenticated` exist on Supabase and nowhere else.

        Revoking unconditionally would make this migration succeed on Supabase
        and fail on every local or CI database — so it could never be rehearsed
        anywhere but production.
        """
        normalised = " ".join(self.sql.lower().split())
        self.assertIn("from pg_roles where rolname = 'anon'", normalised)
        self.assertIn("from pg_roles where rolname = 'authenticated'", normalised)


class SyntheticOnlyTests(unittest.TestCase):
    """The seed must stay pointed at the generator."""

    def test_the_seed_uses_the_demo_generator(self) -> None:
        source = (APP_ROOT / "scripts" / "seed_supabase.py").read_text(encoding="utf-8")
        self.assertIn("from app.domain.synthetic import", source)
        self.assertIn("generate_cohort", source)

    def test_the_seed_reads_its_credentials_from_the_environment(self) -> None:
        """A connection string in the file would be a committed credential."""
        source = (APP_ROOT / "scripts" / "seed_supabase.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SUPABASE_DB_URL"', source)
        self.assertNotIn("postgresql://postgres", source.replace("postgresql://...", ""))


if __name__ == "__main__":
    unittest.main()
