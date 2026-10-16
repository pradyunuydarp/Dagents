#!/usr/bin/env python3
"""Load each demo hospital's IST records into that hospital's own schema.

The records come from the committed extracts, ``data/ist/ist_three_hospitals.csv``
for the three hospitals in the consortium at start and
``data/ist/ist_joining_hospitals.csv`` for the hospital that joins while the
demo runs (International Stroke Trial database, version 2, ODC-By 1.0; see
``data/ist/NOTICE.md``). They are real, de-identified trial records from
1991-1996. Three of the joining hospital's records have no six-month outcome;
their ``poor_outcome_6m`` is stored as NULL.

Each hospital is written while acting as its own role (``site_uk_134`` and so
on), through the same store the API uses. The script then reads each count
back, again as that hospital. It applies no migrations: run
``apply_migrations.py`` first.

By default each hospital's rows are replaced. ``--keep-entries`` keeps the
records people entered in the demo (``origin = 'entered'``) and replaces only
the dataset rows. The audit chains are never touched.

The connection string comes from the environment, never from a file in the
repository:

    SUPABASE_DB_URL=postgresql://... apps/healthcare-demo/scripts/seed_supabase.py

Use the connection string from Supabase (Project settings, Database,
Connection string). It logs in as the project's ``postgres`` role, which the
migration granted permission to switch to each hospital's role.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
# The backend package is not installed, so put it and the framework on the path.
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))


def _connection_hint(dsn: str) -> str:
    try:
        from app.services.db_hints import connection_hint
    except Exception:  # the hint is optional
        return ""
    return connection_hint(dsn)


def seed(dsn: str, *, keep_entries: bool = False) -> dict[str, dict[str, int]]:
    """Write every hospital's dataset rows and return the counts read back.

    Params:
    - `dsn`: the database connection string.
    - `keep_entries`: keep records entered in the demo.

    Returns:
    - ``{site_id: {"total", "entered", "labelled"}}``, read from the database
      while acting as each hospital.
    """
    # Imported here so `--help` works without the app's dependencies.
    from app.domain.dataset import JOINING_SITES, SITES, load_site_records
    from app.services.site_store import PostgresSiteStore, StoreUnavailableError

    counts: dict[str, dict[str, int]] = {}
    for site in (*SITES, *JOINING_SITES):
        records = load_site_records(site.site_id)
        if len(records) != site.expected_rows:
            raise SystemExit(
                f"{site.site_id}: the extract has {len(records)} rows, expected {site.expected_rows}. "
                "Regenerate it with scripts/extract_ist.py."
            )
        store = PostgresSiteStore(site, dsn, statement_timeout_ms=60_000)
        try:
            store.seed_dataset(records, keep_entries=keep_entries)
            counts[site.site_id] = store.count()
        except StoreUnavailableError as error:
            raise SystemExit(f"{error}{_connection_hint(dsn)}")
    return counts


def main(argv: list[str] | None = None) -> int:
    """Seed the database named by ``SUPABASE_DB_URL``."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--keep-entries",
        action="store_true",
        help="keep records entered in the demo and replace only the dataset rows",
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not dsn:
        raise SystemExit(
            "SUPABASE_DB_URL is not set.\n"
            "Take the connection string from Supabase (Project settings, Database, Connection string)\n"
            "and pass it through the environment. Never commit it."
        )

    counts = seed(dsn, keep_entries=args.keep_entries)
    for site_id, count in counts.items():
        print(
            f"  {site_id:8} {count['total']:5} records "
            f"({count['entered']} entered, {count['labelled']} with a six-month outcome)"
        )
    total = sum(count["total"] for count in counts.values())
    print(f"{total} records stored across {len(counts)} hospitals, each read back as its own role")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
