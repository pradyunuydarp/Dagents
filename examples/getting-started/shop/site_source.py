"""How a store's LMA reads the store's orders.

An LMA reads its site's records through a site source. An application that
builds its LMAs in its own process hands each one a source object
(``shop/agents.py``). An LMA started as a process of its own, from its
settings, cannot be handed an object, so it builds its source with a factory
that an extension contributes: ``LMA_SITE_SOURCE=shop_orders`` names
:func:`shop_orders`, which ``shop/extension.py`` registers.
"""

from __future__ import annotations

from typing import Any

from shop.store import SITE_IDS, OrderStore, load_store


class OrderStoreSource:
    """One store's orders, in the form an LMA reads them (a ``SiteRecordSource``)."""

    def __init__(self, store: OrderStore) -> None:
        self._store = store

    def records(self) -> list[dict[str, Any]]:
        return self._store.orders()

    def count(self) -> int:
        return self._store.count()

    def version(self) -> str:
        # Orders are only added, so the count changes whenever the orders do.
        # The LMA reads the orders again when the version changes.
        return str(self._store.count())

    def location(self) -> dict[str, str]:
        # The LMA shows this to its callers, so it names the store, never a host.
        return {"store": self._store.site_id}

    def close(self) -> None:
        pass


def shop_orders(lease: Any, role: str | None, site_id: str) -> OrderStoreSource:
    """The ``shop_orders`` site source: the orders of ``site_id``, from its CSV file.

    An LMA calls this once, when it starts. It passes a database lease when
    ``LMA_DATABASE_URL`` is set, and ``None`` otherwise. It passes the value
    of ``LMA_SITE_ROLE`` as ``role``, with or without a database, and
    ``None`` when that is not set. An LMA with ``LMA_DATABASE_URL`` but no
    ``LMA_SITE_ROLE`` stops before it calls this. This example keeps its
    orders in files, so it uses neither.
    """
    if site_id not in SITE_IDS:
        raise ValueError(f"the example has no orders for {site_id!r}; LMA_SITE_ID must be one of {', '.join(SITE_IDS)}")
    return OrderStoreSource(load_store(site_id))
