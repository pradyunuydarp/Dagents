"""Which site an LMA serves, and how it describes itself.

A typed value with no I/O. The LMA reads the rest of what it needs, such as
the classification, the label field and the approved purposes, from the
condition pack named here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The policy version a site reports when its configuration names none.
DEFAULT_POLICY_VERSION = "site-policy-v1"


@dataclass(frozen=True)
class SiteConfig:
    """One site, as its LMA is configured.

    - ``site_id``: the site's id in the study.
    - ``condition_id``: the condition pack the site serves. An extension
      contributes the pack.
    - ``study_id``: who the coordinator is when the site reports its summary.
      ``None`` uses the condition id.
    - ``reporting_purpose``: the purpose the summary's label-rate request
      declares. ``None`` uses the pack's first approved purpose.
    - ``profile_fields``: the fields the running profile tracks. Empty tracks
      every field of the pack's feature contract.
    - ``capabilities``: what the site can do in a round, as its registration
      says.
    - ``policy_version``: the version of the site's local policy.
    """

    site_id: str
    condition_id: str
    study_id: str | None = None
    reporting_purpose: str | None = None
    profile_fields: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    policy_version: str = DEFAULT_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in ("site_id", "condition_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a site configuration needs a non-empty {name}")
        object.__setattr__(self, "profile_fields", tuple(self.profile_fields))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
