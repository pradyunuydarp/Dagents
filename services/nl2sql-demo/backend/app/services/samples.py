"""Sample NL2SQL inputs used by the backend and React frontend."""

from __future__ import annotations

from app.models import SamplePrompt, SchemaColumn, SchemaTable


SAMPLES: list[SamplePrompt] = [
    SamplePrompt(
        sample_id="orders-by-tenant",
        title="Orders by tenant",
        question="List the order ids and amounts for paid orders from tenant alpha.",
        tables=[
            SchemaTable(
                name="orders",
                columns=[
                    SchemaColumn(name="order_id", dtype="TEXT"),
                    SchemaColumn(name="tenant_id", dtype="TEXT"),
                    SchemaColumn(name="amount", dtype="NUMBER"),
                    SchemaColumn(name="status", dtype="TEXT"),
                    SchemaColumn(name="created_at", dtype="TEXT"),
                ],
            )
        ],
    ),
    SamplePrompt(
        sample_id="incident-count",
        title="Incident count",
        question="How many critical incidents are open?",
        tables=[
            SchemaTable(
                name="incidents",
                columns=[
                    SchemaColumn(name="incident_id", dtype="TEXT"),
                    SchemaColumn(name="severity", dtype="TEXT"),
                    SchemaColumn(name="status", dtype="TEXT"),
                    SchemaColumn(name="service_name", dtype="TEXT"),
                ],
            )
        ],
    ),
    SamplePrompt(
        sample_id="customers-with-invoices",
        title="Join customers and invoices",
        question="Show customer names and invoice totals for unpaid invoices.",
        tables=[
            SchemaTable(
                name="customers",
                columns=[
                    SchemaColumn(name="customer_id", dtype="NUMBER"),
                    SchemaColumn(name="name", dtype="TEXT"),
                    SchemaColumn(name="region", dtype="TEXT"),
                ],
            ),
            SchemaTable(
                name="invoices",
                columns=[
                    SchemaColumn(name="invoice_id", dtype="NUMBER"),
                    SchemaColumn(name="customer_id", dtype="NUMBER"),
                    SchemaColumn(name="total", dtype="NUMBER"),
                    SchemaColumn(name="status", dtype="TEXT"),
                ],
            ),
        ],
    ),
]


def sample_by_id(sample_id: str) -> SamplePrompt | None:
    return next((sample for sample in SAMPLES if sample.sample_id == sample_id), None)
