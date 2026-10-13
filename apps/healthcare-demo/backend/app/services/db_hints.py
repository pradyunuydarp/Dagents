"""Advice for a database connection failure that has no clear message.

The migration script, the seed script and the site stores all use this, so the
advice is written once.
"""

from __future__ import annotations

from urllib.parse import urlparse


def connection_hint(database_url: str) -> str:
    """Extra text for a failed connection to Supabase's direct host.

    Supabase's direct host, ``db.<ref>.supabase.co``, has an IPv6 address only.
    GitHub Actions runners and Render have IPv4 only, so a connection from
    either fails with "Network is unreachable" and no mention of the cause. The
    pooler host has both kinds of address.

    Params:
    - `database_url`: the connection string that failed. Only its host is read.

    Returns:
    - The advice, starting with two newlines, when the host is Supabase's
      direct host. An empty string for any other host.
    """
    try:
        host = urlparse(database_url).hostname or ""
    except ValueError:
        return ""
    if not (host.startswith("db.") and host.endswith(".supabase.co")):
        return ""
    return (
        "\n\nThis is Supabase's direct host, which has no IPv4 address. GitHub Actions and "
        "Render have IPv4 only, so neither can reach it. Use the pooler connection string "
        "instead: Supabase > Connect > Session pooler. It looks like\n"
        "  postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
        "The user name includes the project ref, and the password must be percent-encoded "
        "if it contains special characters."
    )
