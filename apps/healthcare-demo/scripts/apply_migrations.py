#!/usr/bin/env python3
"""Apply the demo's SQL migrations, in order, exactly once each.

A workflow applies the migrations, not a person, so the database holds what is
committed. Running the script again is safe. Each file runs in its own
transaction and is recorded in `public.schema_migrations`. A file that has
already been applied is skipped, because a later migration may not be written
to run twice.

Run it as the project's login role (`postgres` on Supabase). Migration 0002
creates roles, so that role needs CREATEROLE, and PostgreSQL 15 or later. On 15
the login role inherits the site roles' privileges; 0002 explains what that
changes.

The connection string comes from the environment and never from a file in the
repository:

    SUPABASE_DB_URL=postgresql://... apps/healthcare-demo/scripts/apply_migrations.py

Use `--dry-run` to list what would be applied without connecting.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


APP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = APP_ROOT / "supabase" / "migrations"
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])

# The hint for Supabase's IPv6-only direct host lives with the app's data code,
# so there is one copy of it rather than three that drift.
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))
try:
    from app.services.db_hints import connection_hint
except Exception:  # pragma: no cover - the hint is optional; the migrations are not
    def connection_hint(_: str) -> str:
        return ""

#: The ledger lives in `public`, where Supabase's Data API roles may read new
#: tables by default. Row level security with no policy, and the revokes, keep
#: them out. The revokes run only where those roles exist.
LEDGER = """
create table if not exists public.schema_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
);
alter table public.schema_migrations enable row level security;
revoke all on public.schema_migrations from public;
do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on public.schema_migrations from %I', r);
        end if;
    end loop;
end
$$;
"""


def migration_files() -> list[Path]:
    """Every migration, in filename order.

    Ordering is the filename prefix, so `0002_` follows `0001_`. Failing on an
    empty directory matters: a misconfigured path would otherwise report
    "0 migrations applied" and look like success.
    """
    if not MIGRATIONS.is_dir():
        raise SystemExit(f"No migrations directory at {MIGRATIONS}")
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        raise SystemExit(f"No .sql migrations found in {MIGRATIONS}")
    return files


def apply(dsn: str) -> list[str]:
    """Apply every unapplied migration and return the ones that ran."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - listed in requirements
        raise SystemExit(f"psycopg is required to apply migrations: {exc}")

    applied: list[str] = []
    try:
        connection = psycopg.connect(dsn)
    except Exception as exc:
        raise SystemExit(f"Could not connect: {exc}{connection_hint(dsn)}")
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(LEDGER)
            cursor.execute("select filename from public.schema_migrations")
            already = {row[0] for row in cursor.fetchall()}
        connection.commit()

        for path in migration_files():
            if path.name in already:
                print(f"  skip   {path.name} (already applied)")
                continue
            sql = path.read_text(encoding="utf-8")
            with connection.cursor() as cursor:
                cursor.execute(sql)
                cursor.execute(
                    "insert into public.schema_migrations (filename) values (%s)",
                    (path.name,),
                )
            connection.commit()
            applied.append(path.name)
            print(f"  apply  {path.name}")
    return applied


def main(argv: list[str] | None = None) -> int:
    """Apply migrations against `SUPABASE_DB_URL`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list migrations and exit")
    args = parser.parse_args(argv)

    files = migration_files()
    if args.dry_run:
        for path in files:
            print(f"  would apply {path.name}")
        return 0

    dsn = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not dsn:
        raise SystemExit(
            "SUPABASE_DB_URL is not set.\n"
            "Take the URI from Supabase → Project settings → Database → Connection string,\n"
            "and pass it through the environment. Never commit it."
        )

    applied = apply(dsn)
    print(f"{len(applied)} migration(s) applied, {len(files) - len(applied)} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
