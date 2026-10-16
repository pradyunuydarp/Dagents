"""Each store keeps its orders in a store of its own.

This example keeps them in memory, loaded from ``data/<site_id>.csv``. A
deployment with a database gives each store its own Postgres schema and role
and runs every query through ``dagents.common.infrastructure.postgres_roles.acting_as``;
the healthcare demo's ``PostgresSiteStore`` shows how.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

from dagents.common.application.running_profile import RunningProfile

from shop.contract import FEATURE_CONTRACT

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

#: The two stores in this example. Each has its own CSV file in data/.
SITE_IDS = ("store-north", "store-south")

CATEGORIES = ("clothing", "shoes", "electronics", "home")
AGE_BANDS = ("18-24", "25-34", "35-44", "45-54", "55+")


def parse_order(row: Mapping[str, str]) -> dict[str, Any]:
    """Turn one CSV row into a typed order, or raise ValueError."""
    if row["category"] not in CATEGORIES:
        raise ValueError(f"unknown category {row['category']!r}")
    if row["customer_age_band"] not in AGE_BANDS:
        raise ValueError(f"unknown age band {row['customer_age_band']!r}")
    order: dict[str, Any] = {
        "order_id": row["order_id"],
        "customer_postcode": row["customer_postcode"],
        "order_value": float(row["order_value"]),
        "items": int(row["items"]),
        "category": row["category"],
        "customer_age_band": row["customer_age_band"],
        "loyalty_member": row["loyalty_member"] == "true",
    }
    if row.get("returned") in ("true", "false"):
        order["returned"] = row["returned"] == "true"
    return order


class OrderStore:
    """One store's orders, and the running profile of their contract fields.

    The profile grows by one record for each order added, so the store never
    reads every order again to describe them.
    """

    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        self._orders: list[dict[str, Any]] = []
        self._profile = RunningProfile(fields=FEATURE_CONTRACT.field_names())

    def add(self, order: dict[str, Any]) -> None:
        self._orders.append(dict(order))
        self._profile.add(order)

    def orders(self) -> list[dict[str, Any]]:
        """Copies of every order, so a caller cannot change the store."""
        return [dict(order) for order in self._orders]

    def count(self) -> int:
        return len(self._orders)

    def profile(self) -> dict[str, Any]:
        return self._profile.snapshot()


def load_store(site_id: str) -> OrderStore:
    """Build a store from its CSV file."""
    store = OrderStore(site_id)
    with (DATA_DIR / f"{site_id}.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            store.add(parse_order(row))
    return store
