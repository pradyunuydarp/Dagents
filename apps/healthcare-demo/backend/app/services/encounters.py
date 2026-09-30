"""Where a hospital's cohort comes from.

The demo has two sources and they must not be confusable:

- **`synthetic`** — generated in process by `app.domain.synthetic`. No database
  needed; this is what a local run and the test suite use.
- **`supabase`** — read from Postgres through the framework's own
  `PostgresSourceAdapter`, so the demo exercises the same source path a real
  consumer would rather than growing a private database client.

Two rules hold here.

**The records are synthetic either way.** Storing them in a real database does
not make them real patients, and nothing in this demo ever uses a real record.
That is a design constraint: a demonstration of privacy controls carrying real
patient data would be self-refuting.

**A configured database that cannot be read is an error, not a fallback.** If
`HEALTHCARE_DEMO_DATABASE_URL` is set, this module either returns rows from it
or raises. Quietly generating a cohort instead would leave the app claiming live
data while showing invented data — the one failure this demo cannot afford.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

import re

from agents.common.domain.sources import (
    ConnectionRef,
    DatasetInput,
    PostgresSelection,
    PostgresSourceSpec,
    SourceBatching,
)
from agents.common.infrastructure.sources import DefaultSourceResolver, InMemoryConnectionResolver
from app.domain.synthetic import HospitalProfile, generate_cohort


#: Columns the app reads back, matching `stroke-triage-features-v2` plus the
#: encounter id. `site_id` and `recorded_at` are storage concerns and are not
#: part of a record the rule or the Guard ever sees.
ENCOUNTER_FIELDS = (
    "encounter_id",
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

#: How the cohort was obtained. Reported by the API so a reader can tell a live
#: deployment from a local one without guessing.
Provenance = str


class CohortUnavailableError(RuntimeError):
    """A database was configured and could not be read."""


def connection_args(database_url: str) -> dict[str, Any]:
    """Split a Postgres URL into the keys the framework adapter expects.

    The adapter takes discrete connection settings rather than a DSN, so the URL
    is parsed here instead of being passed through. Percent-encoded passwords
    are decoded, because Supabase passwords routinely contain characters that
    have to be escaped in a URL and would otherwise authenticate as the escaped
    text.
    """
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise CohortUnavailableError(
            f"HEALTHCARE_DEMO_DATABASE_URL must be a postgres URL, got {parsed.scheme!r}"
        )
    args: dict[str, Any] = {
        "host": parsed.hostname,
        "dbname": (parsed.path or "/postgres").lstrip("/") or "postgres",
    }
    if parsed.port:
        args["port"] = parsed.port
    if parsed.username:
        args["user"] = unquote(parsed.username)
    if parsed.password:
        args["password"] = unquote(parsed.password)
    # Supabase requires TLS. Defaulting to `require` rather than leaving it to
    # libpq means a misconfigured deployment fails to connect instead of
    # succeeding over plaintext.
    args["sslmode"] = "require" if "supabase" in (parsed.hostname or "") else "prefer"
    return args


#: Site ids are lowercase slugs. Anything else is refused rather than escaped,
#: because a value that does not match is a bug in the consortium definition,
#: not input to be sanitised into something that runs.
_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _safe_site_id(site_id: str) -> str:
    """Return `site_id` if it is a plain slug, else refuse."""
    if not _SITE_ID.match(site_id):
        raise CohortUnavailableError(
            f"Refusing to build a query for site id {site_id!r}: expected a lowercase slug."
        )
    return site_id


def connection_hint(database_url: str) -> str:
    """Extra guidance for the one connection failure that looks like nothing else.

    Supabase's direct host, `db.<ref>.supabase.co`, resolves to an IPv6 address
    only. GitHub Actions runners and Render's egress are both IPv4-only, so the
    connection string the Supabase dashboard shows first cannot work from
    either — and the failure arrives as a bare "Network is unreachable" with
    nothing pointing at the cause. The pooler host is dual-stack.

    Returns an empty string when the URL is not that shape, so this never
    editorialises about an unrelated error.
    """
    host = urlparse(database_url).hostname or ""
    if not (host.startswith("db.") and host.endswith(".supabase.co")):
        return ""
    return (
        "\n\nThis is Supabase's direct host, which has no IPv4 address. GitHub Actions and "
        "Render are IPv4-only, so it is unreachable from both. Use the pooler connection "
        "string instead: Supabase → Connect → Session pooler, which looks like\n"
        "  postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
        "Note the username carries the project ref, and the password must be percent-encoded "
        "if it contains special characters."
    )


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a database row like a generated record.

    The generator *omits* a field it did not record; SQL returns it as NULL.
    That difference is not cosmetic — the completeness check and the feature
    contract both test for a field's absence, so a row full of NULLs would look
    complete-but-empty rather than incomplete, and the data-quality path would
    stop finding the gaps it exists to find.
    """
    return {key: value for key, value in row.items() if value is not None}


def load_from_database(
    profile: HospitalProfile, database_url: str, *, limit: int
) -> list[dict[str, Any]]:
    """Materialize one site's cohort through the framework's Postgres adapter.

    Going through `DefaultSourceResolver` rather than opening a connection here
    is the point: the demo consumes the same source path the framework offers
    every other consumer, so a fix or a limit applied there applies here too.
    """
    connection_id = f"healthcare-{profile.site_id}"
    resolver = DefaultSourceResolver(
        connections=InMemoryConnectionResolver({connection_id: connection_args(database_url)})
    )
    source = PostgresSourceSpec(
        source_id=f"stroke-encounters-{profile.site_id}",
        connection_ref=ConnectionRef(connection_id=connection_id),
        selection=PostgresSelection(
            table="public.encounters",
            columns=list(ENCOUNTER_FIELDS),
            # The selection is a SQL fragment, so the site id is checked against
            # a strict pattern before it is interpolated. It comes from the
            # app's own consortium definition rather than from a request, but
            # "the caller is trusted" is exactly the assumption that stops being
            # true later — and this file would be the one that got it wrong.
            where=f"site_id = '{_safe_site_id(profile.site_id)}'",
            order_by=["encounter_id"],
        ),
        batching=SourceBatching(batch_size=500, max_records=limit),
    )
    try:
        batches = resolver.materialize(DatasetInput(source=source))
    except Exception as exc:  # the adapter raises driver errors verbatim
        raise CohortUnavailableError(
            f"Could not read {profile.site_id} from the configured database: {exc}"
            + connection_hint(database_url)
        ) from exc

    records = [_clean(record) for batch in batches for record in batch.records]
    if not records:
        raise CohortUnavailableError(
            f"The configured database holds no encounters for {profile.site_id}. "
            "Run apply_migrations.py and seed_supabase.py, or unset "
            "HEALTHCARE_DEMO_DATABASE_URL to use the in-process generator."
        )
    return records


def load_cohort(
    profile: HospitalProfile, cohort_size: int, database_url: str = ""
) -> tuple[list[dict[str, Any]], Provenance]:
    """Return one site's cohort and where it came from.

    Params:
    - `profile`: the site, which supplies both its id and its generator seed.
    - `cohort_size`: how many encounters to use.
    - `database_url`: when set, the cohort is read from it and a failure raises.

    Returns:
    - `(records, provenance)` where provenance is `"supabase"` or `"synthetic"`.
    """
    if database_url.strip():
        return load_from_database(profile, database_url.strip(), limit=cohort_size), "supabase"
    return generate_cohort(profile, cohort_size), "synthetic"
