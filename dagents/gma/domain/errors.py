"""Errors only the GMA raises, with the JSON ``code`` and HTTP status it answers with.

| Error | `code` | HTTP status |
|---|---|---|
| `SourceReadsDisabled` | `forbidden` | 403 |
| `InvalidReleaseRequest` | `invalid_request` | 422 |
| `UnknownConditionPack` | `not_found` | 404 |
| `UnknownRound` | `not_found` | 404 |
| `InvalidSiteAddress` | `invalid_request` | 422 |
| `SiteNotJoinable` | `not_joinable` | 403 |
| `SiteAddressDiffers` | `address_differs` | 403 |
| `SiteConfiguredElsewhere` | `configured_elsewhere` | 409 |
| `SiteConfigured` | `configured` | 409 |

Like the errors in :mod:`dagents.common.domain.errors`, a message never holds a
connection string, a host or a credential.
"""

from __future__ import annotations

from typing import ClassVar

from dagents.common.domain.errors import DagentsError


class SourceReadsDisabled(DagentsError, PermissionError):
    """The GMA was started with ``GMA_ALLOW_SOURCE_READS=0``, so it reads no source."""

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


class InvalidReleaseRequest(DagentsError, ValueError):
    """A release evaluation mixes the two flows or lacks what its flow needs."""

    code: ClassVar[str] = "invalid_request"
    http_status: ClassVar[int] = 422


class UnknownConditionPack(DagentsError, LookupError):
    """No registered extension contributes the condition pack a request names."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class UnknownRound(DagentsError, LookupError):
    """A request names a round the GMA does not hold, other than the round in its path."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class SiteAddressRefused(DagentsError):
    """The GMA refused a site's address. A subclass names the reason.

    ``site_id`` is the site the request named. The message never holds the
    address: the caller sent it, and the GMA never shows an agent's address.
    """

    def __init__(self, site_id: str, message: str) -> None:
        super().__init__(message)
        self.site_id = site_id


class InvalidSiteAddress(SiteAddressRefused, ValueError):
    """The address is not an http or https base address, or holds a user, a password, a query or a fragment."""

    code: ClassVar[str] = "invalid_request"
    http_status: ClassVar[int] = 422


class SiteNotJoinable(SiteAddressRefused, PermissionError):
    """The site is not in ``GMA_JOINABLE_SITE_ENDPOINTS`` (nor in ``GMA_SITE_ENDPOINTS``)."""

    code: ClassVar[str] = "not_joinable"
    http_status: ClassVar[int] = 403


class SiteAddressDiffers(SiteAddressRefused, PermissionError):
    """The address differs from the site's entry in ``GMA_JOINABLE_SITE_ENDPOINTS``."""

    code: ClassVar[str] = "address_differs"
    http_status: ClassVar[int] = 403


class SiteConfiguredElsewhere(SiteAddressRefused):
    """The site is in ``GMA_SITE_ENDPOINTS`` with another address."""

    code: ClassVar[str] = "configured_elsewhere"
    http_status: ClassVar[int] = 409


class SiteConfigured(SiteAddressRefused):
    """The site's address comes from ``GMA_SITE_ENDPOINTS``, so it cannot be removed while the GMA runs."""

    code: ClassVar[str] = "configured"
    http_status: ClassVar[int] = 409


__all__ = [
    "InvalidReleaseRequest",
    "InvalidSiteAddress",
    "SiteAddressDiffers",
    "SiteAddressRefused",
    "SiteConfigured",
    "SiteConfiguredElsewhere",
    "SiteNotJoinable",
    "SourceReadsDisabled",
    "UnknownConditionPack",
    "UnknownRound",
]
