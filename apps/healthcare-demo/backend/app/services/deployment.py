"""Add a hospital to a running consortium: the settings that allow it.

A person who runs the split demo on their own machine can add a hospital that
is not in the consortium at start (``JOINING_SITES``) while the demo runs.
Adding one starts a process or a container, so it is off unless
``HEALTHCARE_DEMO_DEPLOYER`` turns it on, and :func:`check_deployer` refuses
every setting that would let a visitor of a public demo start one.
"""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit

from app.core.config import Settings
from app.domain.dataset import JOINING_SITE_IDS

#: The values ``HEALTHCARE_DEMO_DEPLOYER`` may take.
DEPLOYERS = ("off", "process", "compose")

#: Hosts that mean "this machine" in an address.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class DeployerSettingError(ValueError):
    """A deploy setting the app refuses to start with."""


def check_deployer(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse deploy settings that are unsafe or cannot work. Called at start-up.

    Raises :class:`DeployerSettingError` at the first rule that fails:

    1. ``HEALTHCARE_DEMO_DEPLOYER`` is ``off``, ``process`` or ``compose``.
    2. ``off`` needs nothing else.
    3. Only the ``app`` role adds hospitals.
    4. Never on Render: the public demo never starts a process or a container.
    5. ``HEALTHCARE_DEMO_JOINING_LMA_URLS`` names at least one hospital.
    6. Each one is a hospital that can join.
    7. None is in ``HEALTHCARE_DEMO_LMA_URLS``, by id or by address.
    8. Each address is ``http`` with an explicit port.
    9. In compose mode each address is ``http://lma-{id}:<port>``, the
       container's name on the Compose network.
    10. In compose mode the hospitals reach the app's internal listener by its
        name on the Compose network, not by 127.0.0.1.
    11. In process mode each address is on 127.0.0.1 or localhost.
    """
    environ = os.environ if environ is None else environ
    value = settings.deployer
    if value not in DEPLOYERS:
        raise DeployerSettingError(f"HEALTHCARE_DEMO_DEPLOYER must be off, process or compose, not {value!r}.")
    if value == "off":
        return
    if settings.role != "app":
        raise DeployerSettingError(
            f"HEALTHCARE_DEMO_DEPLOYER={value} needs HEALTHCARE_DEMO_ROLE=app. Only the app of a split run adds "
            "hospitals."
        )
    if environ.get("RENDER"):
        raise DeployerSettingError(
            "HEALTHCARE_DEMO_DEPLOYER must be off on Render. The public demo never starts a process or a container."
        )
    try:
        joining = settings.joining_lma_url_map()
        configured = settings.lma_url_map()
    except ValueError as error:
        raise DeployerSettingError(str(error)) from None
    if not joining:
        raise DeployerSettingError("HEALTHCARE_DEMO_JOINING_LMA_URLS names no hospital.")
    for site_id in joining:
        if site_id not in JOINING_SITE_IDS:
            raise DeployerSettingError(
                f"{site_id} is not a hospital that can join. Known: {', '.join(JOINING_SITE_IDS)}."
            )
    configured_urls = set(configured.values())
    for site_id, url in joining.items():
        if site_id in configured or url in configured_urls:
            raise DeployerSettingError(f"{site_id} clashes with HEALTHCARE_DEMO_LMA_URLS.")
    hosts: dict[str, str] = {}
    for site_id, url in joining.items():
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme != "http" or port is None or not parts.hostname:
            raise DeployerSettingError(f"The address of {site_id} needs http and an explicit port.")
        hosts[site_id] = parts.hostname
    if value == "compose":
        for site_id, host in hosts.items():
            if host != f"lma-{site_id}":
                raise DeployerSettingError(
                    f"In compose mode the address of {site_id} must be http://lma-{site_id}:<port>."
                )
        if urlsplit(settings.coordinator_url).hostname in LOOPBACK_HOSTS:
            raise DeployerSettingError(
                "In compose mode set HEALTHCARE_DEMO_COORDINATOR_URL to the app's internal address on the Compose "
                "network, such as http://healthcare-demo-backend:8081."
            )
    else:
        for site_id, host in hosts.items():
            if host not in LOOPBACK_HOSTS:
                raise DeployerSettingError(
                    f"In process mode the address of {site_id} must be on 127.0.0.1 or localhost."
                )
