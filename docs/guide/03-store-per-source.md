# Step 3: Give each source its own store

Each data source keeps its records in a store of its own. Two rules hold for
every store:

- **Every read or write of a source's records goes through that source's store.**
  No code path reads two sources' records together.
- **The coordinator never reads records.** It sees what each source reports:
  counts, summaries the Guard permits, round results and audit chain lengths.

The worked example keeps each store in memory. A deployment with a database
gives each source its own Postgres schema and role. Both are described below.

## 1. An in-memory store

<!-- source: examples/getting-started/shop/store.py -->
```python
DATA_DIR = Path(__file__).resolve().parents[1] / "data"

#: The two stores in this example. Each has its own CSV file in data/.
SITE_IDS = ("store-north", "store-south")

CATEGORIES = ("clothing", "shoes", "electronics", "home")
AGE_BANDS = ("18-24", "25-34", "35-44", "45-54", "55+")
```

<!-- source: examples/getting-started/shop/store.py -->
```python
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
```

What each part does:

- `parse_order` checks each value before the store accepts it: a category or
  age band outside the agreed list is refused. Accept typed values only. A store
  that takes free text cannot promise what it holds.
- `OrderStore` keeps one source's orders and nothing else. `orders()` returns
  copies, so a caller cannot change the store by editing a result.
- The `RunningProfile` grows by one record for each order added (Step 5 uses
  it).
- `load_store("store-north")` reads `data/store-north.csv` only.

In memory, nothing survives a restart. That is right for tests and examples.

## 2. A Postgres schema and role per source

In Postgres, each source gets:

- a schema that holds its tables, for example `site_uk_134`;
- a role with the same name that owns the schema and cannot log in (`NOLOGIN`);
- no grant for any other source's role or for the coordinator's role.

Your service logs in with one connection string. Before each operation it
switches to the source's role for one transaction only. Dagents provides the
switch: `acting_as` in
[`agents/common/infrastructure/postgres_roles.py`](../../agents/common/infrastructure/postgres_roles.py).
It sends `BEGIN`, `SET LOCAL ROLE <role>` and `SELECT current_user` as one
message, checks the user it gets back, runs your queries and commits. The role
ends with the transaction.

The stroke demo is the reference implementation. Its migration creates the
roles and schemas:

<!-- source: apps/healthcare-demo/supabase/migrations/0002_site_isolation.sql -->
```sql
    foreach r in array sites || coordinator_role loop
        if not exists (select 1 from pg_roles where rolname = r) then
            execute format('create role %I nologin noinherit nobypassrls', r);
        end if;
        if grant_options then
            execute format('grant %I to current_user with inherit false, set true', r);
        else
            execute format('grant %I to current_user', r);
        end if;
    end loop;

    -- 2. One schema per hospital. Tables are created while acting as the
    --    hospital's role, so the hospital owns them and the connecting role
    --    does not.
    foreach s in array sites loop
        execute format('create schema if not exists %I authorization %I', s, s);
```

Its store keeps one connection, a `KeptConnection` from
[`agents/common/infrastructure/postgres_lease.py`](../../agents/common/infrastructure/postgres_lease.py).
The store builds the connection itself, or runs on one it is given:

<!-- source: apps/healthcare-demo/backend/app/services/site_store.py -->
```python
        if lease is not None:
            if dsn is not None:
                raise ValueError("give a Postgres store a connection string or a lease, not both")
            self.lease = lease
            self._owns_lease = False
        else:
            if not isinstance(dsn, str) or not dsn.strip():
                raise ValueError("a Postgres store needs a connection string or a lease")
            self.lease = KeptConnection(
                dsn,
                name=name,
                statement_timeout_ms=statement_timeout_ms,
                liveness_idle_seconds=liveness_idle_seconds,
                hint=connection_hint,
            )
            self._owns_lease = True
```

Every query runs through the connection's `acting_as`, which calls
`postgres_roles.acting_as` for one transaction:

<!-- source: apps/healthcare-demo/backend/app/services/site_store.py -->
```python
    def records(self) -> list[dict[str, Any]]:
        query = self._sql.SQL(
            "SELECT {columns} FROM {table} ORDER BY source_row NULLS LAST, length(encounter_id), encounter_id"
        ).format(columns=self._select(ROW_COLUMNS), table=self._table)
        with self.lease.acting_as(self.role) as conn:
            rows = conn.execute(query).fetchall()
        return [_record_view(row) for row in rows]

    def count(self) -> dict[str, int]:
        query = self._q(
            "SELECT count(*) AS total, count(*) FILTER (WHERE origin = 'entered') AS entered, "
            "count(poor_outcome_6m) AS labelled FROM {table}"
        )
        with self.lease.acting_as(self.role) as conn:
            row = conn.execute(query).fetchone()
        return {key: int(row[key]) for key in ("total", "entered", "labelled")}
```

The hospital's LMA reads the same store, through a site source
(`app/services/site_source.py`), and keeps its audit chain and job log on the
store's connection, as the hospital's role. So each hospital still uses one
database connection, and every read of its records acts as its role, whether
the app or the LMA makes it.

Rules for a Postgres store:

- **Use `SET LOCAL ROLE`, never a session-level `SET ROLE`.** A connection pool,
  or a pooler such as PgBouncer, hands the same server connection to the next
  caller, and a session-level role would go with it. `acting_as` refuses a
  connection that is not in autocommit mode and idle.
- **Never take a role name from a request.** Role names come from your own
  configuration. `acting_as` checks them against a strict pattern and refuses
  reserved names such as `postgres`.
- **Give each source its own login in production.** With one shared login,
  the isolation binds your code paths, not the credential: a person who holds
  the credential can switch to any source's role. The stroke demo reads an
  optional connection string per hospital,
  `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`, for this.
- **Keep the audit chain next to the records.**
  `PostgresAuditSink` (in `agents/common/infrastructure/postgres_audit.py`)
  writes each source's audit chain to a table in that source's schema, as that
  source's role.

## 3. Test both stores with the same assertions

If your application has an in-memory store and a Postgres store, run one test
suite against both. The stroke demo does this in
[`tests/test_site_store.py`](../../apps/healthcare-demo/tests/test_site_store.py):
the Postgres tests create a scratch database, add the roles, apply the
migrations as a non-superuser and drop everything afterwards. Point them at an
admin login on PostgreSQL 16:

```bash
cd apps/healthcare-demo
export HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres
PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest tests.test_site_store tests.test_site_isolation
```

Expected output, the last lines:

```text
Ran 138 tests in 5.958s

OK
```

Without the variable the Postgres tests skip, and each skip reason starts with
`database test skipped`. A run with skips has not tested the Postgres store.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `RoleScopeError: role-scoped access needs an autocommit connection` | The connection was opened without autocommit | Open it with `postgres_roles.connect(dsn)`, which sets autocommit and turns off prepared statements for poolers |
| `RoleScopeError: refusing role name 'postgres': it is reserved` | The role name is a built-in or platform role | Name each source's role yourself, for example `site_store_north` |
| `permission denied for schema ...` | The code switched to one source's role and read another source's schema | This is the isolation working. Read each source through its own store |
| Records from one source appear in another source's results | A query ran without `acting_as`, as the login role | Route every query through the store, and add a test that reads one source as another and expects a failure |

Next: [Step 4: Govern every read](04-govern-every-read.md).
