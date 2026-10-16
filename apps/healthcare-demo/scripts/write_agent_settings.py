#!/usr/bin/env python3
"""Write the settings files of the demo's agents and API, one file per service.

``docker-compose.yml``'s ``setup`` job runs it after the migrations and the
seed, to fill the ``agent-settings`` volume:

    python scripts/write_agent_settings.py compose /run/agent-settings --owner appuser

It writes ``gma.env``, one ``lma-<site>.env`` per hospital and ``api.env``,
which each service reads with ``sh`` (``set -a && . file``), so values are
quoted for ``sh``. Each file holds only the tokens of its own service and is
readable by its owner only. The tokens are made once per volume and kept in
``tokens.json``, readable by root only, so a service that restarts gets the
tokens the others still hold. The addresses are written on every start,
because a port may have changed.

With ``HEALTHCARE_DEMO_DEPLOYER=compose`` (``docker-compose.deploy.yml``) it
also makes the tokens of each hospital that can join while the demo runs, and
writes:

- in ``gma.env``, the hospital's one accepted address
  (``GMA_JOINABLE_SITE_ENDPOINTS``), its token entries, and a dispatch
  concurrency that covers every hospital;
- in ``api.env``, the hospital's LMA address and the app's ``site`` token for it;
- ``lma-<site>.env``, the new LMA's env file, which its Compose file names in
  ``env_file``. Docker Compose reads it, not ``sh``, so it is written in the
  form Compose's ``env_file`` and the appliers read the same way.

``run_local_demo.sh --deploy`` writes the new LMA's env file the same way,
with the values taken from this process's environment, never from its
arguments:

    LMA_SITE_TOKEN=... python scripts/write_agent_settings.py env-file /tmp/x/lma-pl-290.env LMA_SITE_TOKEN ...

An env file refuses a value with a single quote or a line break, and the
message never holds the value. Nothing is printed but a count.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import shlex
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
# The backend package is not installed, so put it and the framework on the path.
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))


def _site_map(values: dict[str, str]) -> str:
    return ",".join(f"{site_id}={value}" for site_id, value in sorted(values.items()))


def _port(site_id: str, environ: dict[str, str]) -> str:
    name = f"HEALTHCARE_DEMO_LMA_{site_id.upper().replace('-', '_')}_PORT"
    try:
        return environ[name]
    except KeyError:
        raise SystemExit(f"{name} is not set. Run docker compose with --env-file env/.env.healthcare-demo.") from None


def compose_files(environ: dict[str, str], tokens: object) -> dict[str, dict[str, str]]:
    """The settings of each service on the Compose network, by file name without ``.env``."""
    from app.core.config import lma_token_variable, lma_url_variable, settings
    from app.domain.dataset import JOINING_SITES, SITES
    from app.services.agents import gma_environment, lma_environment

    deploy = environ.get("HEALTHCARE_DEMO_DEPLOYER", "off").strip().lower() == "compose"
    joining = list(JOINING_SITES) if deploy else []
    database = environ["HEALTHCARE_DEMO_DATABASE_URL"]
    gma_url = "http://gma:" + environ["HEALTHCARE_DEMO_GMA_PORT"]
    lma_urls = {site.site_id: f"http://lma-{site.site_id}:" + _port(site.site_id, environ) for site in SITES}
    joining_urls = {site.site_id: f"http://lma-{site.site_id}:" + _port(site.site_id, environ) for site in joining}
    every = [site.site_id for site in [*SITES, *joining]]

    concurrency = max(settings.dispatch_concurrency, len(every)) if deploy else settings.dispatch_concurrency
    gma = {
        **gma_environment(database=True, dispatch_concurrency=concurrency),
        "GMA_DATABASE_URL": database,
        "GMA_SITE_ENDPOINTS": _site_map(lma_urls),
        "GMA_LMA_TOKENS": _site_map({site_id: tokens.lma_coordinator[site_id] for site_id in every}),
        "GMA_SITE_TOKENS": _site_map({site_id: tokens.gma_site[site_id] for site_id in every}),
        "GMA_ADMIN_TOKEN": tokens.gma_admin,
    }
    if joining_urls:
        gma["GMA_JOINABLE_SITE_ENDPOINTS"] = _site_map(joining_urls)
    api = {
        "HEALTHCARE_DEMO_AGENTS": "network",
        "HEALTHCARE_DEMO_DATABASE_URL": database,
        "HEALTHCARE_DEMO_GMA_URL": gma_url,
        "HEALTHCARE_DEMO_GMA_TOKEN": tokens.gma_admin,
    }
    files: dict[str, dict[str, str]] = {"gma": gma, "api": api}
    for site in [*SITES, *joining]:
        url = (lma_urls | joining_urls)[site.site_id]
        api[lma_url_variable(site.site_id)] = url
        api[lma_token_variable(site.site_id)] = tokens.lma_site[site.site_id]
        secrets = {
            "LMA_DATABASE_URL": database,
            "LMA_GMA_ENDPOINT": gma_url,
            "LMA_GMA_TOKEN": tokens.gma_site[site.site_id],
            "LMA_SITE_TOKEN": tokens.lma_site[site.site_id],
            "LMA_COORDINATOR_TOKEN": tokens.lma_coordinator[site.site_id],
        }
        if site in SITES:
            files[f"lma-{site.site_id}"] = {**lma_environment(site, database=True), **secrets}
        else:
            # The new LMA's Compose file sets its LMA_* settings; the env file
            # holds only what must not be in that file.
            files[f"lma-{site.site_id}"] = secrets
    return files


def write_compose(out: Path, environ: dict[str, str], owner: str | None) -> str:
    """Write every service's settings file in ``out``. Returns whether the tokens are new or kept."""
    from app.domain.dataset import JOINING_SITES, SITES
    from app.services.agents import AgentTokens
    from app.services.deployment import write_env_file

    deploy = environ.get("HEALTHCARE_DEMO_DEPLOYER", "off").strip().lower() == "compose"
    site_ids = sorted(site.site_id for site in [*SITES, *(JOINING_SITES if deploy else ())])
    kept = out / "tokens.json"
    tokens = None
    if kept.exists():
        tokens = AgentTokens(**json.loads(kept.read_text(encoding="utf-8")))
        if sorted(tokens.lma_site) != site_ids:
            tokens = None
    made = "kept from an earlier start"
    if tokens is None:
        tokens = AgentTokens.generate(site_ids)
        write_raw(kept, json.dumps(dataclasses.asdict(tokens)))
        made = "new"
    ids = None
    if owner:
        import pwd

        account = pwd.getpwnam(owner)
        ids = (account.pw_uid, account.pw_gid)
    files = compose_files(environ, tokens)
    joining = {f"lma-{site.site_id}" for site in JOINING_SITES}
    for name, values in files.items():
        if name in joining:
            write_env_file(out / f"{name}.env", values, owner=ids, mode=0o400)
        else:
            text = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
            write_raw(out / f"{name}.env", text, owner=ids)
    return f"Wrote the settings of {len(files)} services. The tokens are {made}."


def write_raw(path: Path, text: str, owner: tuple[int, int] | None = None) -> None:
    """Write a file only its owner can read, give it to ``owner`` when named, then move it into place."""
    temporary = path.with_name(f".{path.name}.new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if owner is not None:
        os.chown(temporary, *owner)
    temporary.chmod(0o400)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    compose = commands.add_parser("compose", help="write every service's settings file for docker compose")
    compose.add_argument("out", type=Path, help="the folder to write, such as /run/agent-settings")
    compose.add_argument("--owner", help="the account that owns the files, such as appuser")
    env_file = commands.add_parser("env-file", help="write one env file from this process's environment")
    env_file.add_argument("path", type=Path, help="the file to write; its folder must exist")
    env_file.add_argument("names", nargs="+", help="the variables to write, each set in this process's environment")
    args = parser.parse_args(argv)

    from app.services.deployment import write_env_file

    environ = dict(os.environ)
    try:
        if args.command == "compose":
            print(write_compose(args.out, environ, args.owner))
            return 0
        missing = [name for name in args.names if not environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} is not set")
        write_env_file(args.path, {name: environ[name] for name in args.names})
    except ValueError as error:
        # The message names the variable, never its value.
        raise SystemExit(str(error)) from None
    print(f"Wrote {len(args.names)} settings to {args.path.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
