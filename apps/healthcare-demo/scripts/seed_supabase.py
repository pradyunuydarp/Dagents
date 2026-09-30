#!/usr/bin/env python3
"""Seed the demo's Supabase database from the existing synthetic generator.

The generator stays the single source of truth for what a cohort looks like. If
this script invented its own distributions the database would hold a different
consortium from the one the demo was designed around — the per-site differences,
the recording gaps, the site that reports glucose in mg/dL without saying so —
and the drift checks, cohort floors and fairness gate would stop being
exercised by the data they exist for.

**Every row written here is synthetic.** No real record is used and none is
required. That is a design constraint of this demo, not a convenience: a
demonstration of privacy controls carrying real patient data would be
self-refuting. Moving the data into a real database does not change it.

The connection string is read from the environment and never from a file in the
repository:

    SUPABASE_DB_URL=postgresql://... apps/healthcare-demo/scripts/seed_supabase.py

Use the pooled connection string from the Supabase dashboard
(Project settings → Database → Connection string → URI). It carries the
service-role credentials, which bypass RLS — which is the point, since the
tables deny the anon role entirely.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "apps" / "healthcare-demo"
# The generator lives in the backend package, which is not installed.
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

from app.domain.synthetic import DEFAULT_HOSPITALS, HospitalProfile, generate_cohort  # noqa: E402

#: Columns written per encounter, in the order the insert binds them. Derived
#: from `stroke-triage-features-v2`; a field added to the contract and not here
#: fails the schema test rather than silently going unseeded.
ENCOUNTER_COLUMNS = (
    "encounter_id",
    "site_id",
    "nihss_total",
    "face_droop",
    "arm_weakness",
    "speech_difficulty",
    "last_known_well_minutes",
    "age_band",
    "systolic_bp",
    "blood_glucose",
    "blood_glucose_unit",
    "arrival_mode",
    "anticoagulated",
    "confirmed_stroke",
)


def hospital_row(profile: HospitalProfile) -> tuple:
    """One consortium row, keeping the generator parameters inspectable."""
    return (
        profile.site_id,
        profile.display_name,
        profile.seed,
        profile.stroke_rate,
        profile.severity_bias,
        profile.missingness,
        profile.older_population,
        profile.glucose_unit_error_rate,
    )


def encounter_rows(profile: HospitalProfile, cohort_size: int) -> list[tuple]:
    """Generate one site's cohort and shape it for the insert.

    A field the generator dropped stays absent here as NULL: the missingness is
    the point, and filling it in would remove the case the data-quality path
    exists to catch.
    """
    rows: list[tuple] = []
    for record in generate_cohort(profile, cohort_size):
        rows.append(
            tuple(
                record.get(column) if column != "site_id" else profile.site_id
                for column in ENCOUNTER_COLUMNS
            )
        )
    return rows


def seed(dsn: str, cohort_size: int, *, replace: bool) -> dict[str, int]:
    """Write the consortium and its cohorts.

    Params:
    - `dsn`: Postgres connection string with service-role credentials.
    - `cohort_size`: encounters generated per site.
    - `replace`: delete existing encounters first, so a reseed is not additive.

    Returns:
    - Encounter counts per site, read back from the database rather than
      assumed, so the report reflects what is actually stored.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - listed in requirements
        raise SystemExit(f"psycopg is required to seed: {exc}")

    written: dict[str, int] = {}
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            if replace:
                # Encounters cascade from hospitals, but be explicit: a reseed
                # that silently doubled every cohort would move every site above
                # its floor and quietly change what the demo demonstrates.
                cursor.execute("delete from public.encounters")

            for profile in DEFAULT_HOSPITALS:
                cursor.execute(
                    """
                    insert into public.hospitals
                        (site_id, display_name, seed, stroke_rate, severity_bias,
                         missingness, older_population, glucose_unit_error_rate)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (site_id) do update set
                        display_name = excluded.display_name,
                        seed = excluded.seed,
                        stroke_rate = excluded.stroke_rate,
                        severity_bias = excluded.severity_bias,
                        missingness = excluded.missingness,
                        older_population = excluded.older_population,
                        glucose_unit_error_rate = excluded.glucose_unit_error_rate
                    """,
                    hospital_row(profile),
                )

                placeholders = ", ".join(["%s"] * len(ENCOUNTER_COLUMNS))
                cursor.executemany(
                    f"""
                    insert into public.encounters ({", ".join(ENCOUNTER_COLUMNS)})
                    values ({placeholders})
                    on conflict (encounter_id) do nothing
                    """,
                    encounter_rows(profile, cohort_size),
                )

            for profile in DEFAULT_HOSPITALS:
                cursor.execute(
                    "select count(*) from public.encounters where site_id = %s",
                    (profile.site_id,),
                )
                written[profile.site_id] = int(cursor.fetchone()[0])
        connection.commit()
    return written


def main(argv: list[str] | None = None) -> int:
    """Seed the database named by `SUPABASE_DB_URL`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort-size",
        type=int,
        default=400,
        help="encounters generated per site (default: 400, the demo's own size)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="keep existing encounters instead of replacing them",
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not dsn:
        raise SystemExit(
            "SUPABASE_DB_URL is not set.\n"
            "Take the URI from Supabase → Project settings → Database → Connection string,\n"
            "and pass it through the environment. Never commit it."
        )

    counts = seed(dsn, args.cohort_size, replace=not args.append)
    total = sum(counts.values())
    for site_id, count in sorted(counts.items()):
        print(f"  {site_id:28} {count:5} encounters")
    print(f"seeded {total} synthetic encounters across {len(counts)} sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
