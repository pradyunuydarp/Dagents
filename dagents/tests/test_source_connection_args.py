"""What a resolved connection may tell the Postgres driver, what a source shows,
and what a request may ask of a site's LMA.

Pure translation and checks, so these tests need no database and never skip.
That matters, because the Postgres suites that do need one skip in CI jobs
without a database, and an allowlist nothing exercises quietly stops matching.

The static site source is here too: it holds records in memory and needs no
database either.
"""

from __future__ import annotations

import inspect
import unittest

from dagents.common.application import ports
from dagents.common.domain.errors import DagentsError
from dagents.common.domain.sources import (
    ConnectionRef,
    DatasetInput,
    InlineSourceSpec,
    MongoSourceSpec,
    ObjectStorageSourceSpec,
    PostgresSelection,
    PostgresSourceSpec,
)
from dagents.common.infrastructure.site_sources import StaticSiteSource
from dagents.common.infrastructure.sources import (
    DEFAULT_SOURCE_STATEMENT_TIMEOUT_MS,
    REDACTED,
    DefaultSourceResolver,
    PostgresSourceAdapter,
    SourceRequestRefused,
    _postgres_connection_args,
    check_site_request_dataset,
    check_site_request_source,
    redact_source,
)

SECRET = "s3cret-source-pw"


class PostgresConnectionArgsTests(unittest.TestCase):
    def test_it_maps_the_common_aliases(self) -> None:
        args = _postgres_connection_args(
            {"database": "healthcare", "username": "dagents", "password": "x", "host": "db", "port": "6543"}
        )
        self.assertEqual(
            {"dbname": "healthcare", "user": "dagents", "password": "x", "host": "db", "port": 6543},
            args,
        )

    def test_a_connect_timeout_reaches_the_driver(self) -> None:
        """Without this, a host that drops packets holds the caller forever.

        It is the failure mode of a remote managed database: the connection is
        accepted and nothing ever answers. A service whose source adapter can
        hang indefinitely can itself hang indefinitely, which is how a request
        ends with no response rather than with an error.
        """
        args = _postgres_connection_args({"host": "db", "connect_timeout": "10"})
        self.assertEqual(10, args["connect_timeout"])

    def test_unknown_keywords_are_not_passed_through(self) -> None:
        """The allowlist exists so that a payload cannot steer the driver."""
        args = _postgres_connection_args(
            {"host": "db", "options": "-c statement_timeout=0", "passfile": "/etc/passwd"}
        )
        self.assertEqual({"host": "db"}, args)

    def test_empty_values_are_dropped_rather_than_sent_as_blank(self) -> None:
        """A blank host is not a host; libpq would read it as "use the default"."""
        self.assertEqual({}, _postgres_connection_args({"host": "", "port": None, "user": ""}))


def postgres_source(options: dict | None = None, **selection: object) -> PostgresSourceSpec:
    return PostgresSourceSpec(
        source_id="orders",
        connection_ref=ConnectionRef(connection_id="store", options=options or {}),
        selection=PostgresSelection(**(selection or {"table": "orders"})),
    )


class PostgresAdapterSettingsTests(unittest.TestCase):
    """Settings of the adapter itself. The database tests are in test_postgres_lease.py."""

    def test_the_default_statement_timeout_matches_the_stores(self) -> None:
        self.assertEqual(15_000, DEFAULT_SOURCE_STATEMENT_TIMEOUT_MS)
        self.assertEqual(15_000, PostgresSourceAdapter().statement_timeout_ms)
        self.assertEqual(250, PostgresSourceAdapter(statement_timeout_ms=250).statement_timeout_ms)

    def test_a_timeout_must_be_positive(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PostgresSourceAdapter(statement_timeout_ms=value)

    def test_null_values_are_kept_unless_asked(self) -> None:
        rows = [{"a": 1, "b": None}, {"a": None, "b": 2}]
        source = PostgresSourceSpec(
            source_id="mock",
            connection_ref=ConnectionRef(connection_id="none"),
            selection=PostgresSelection(table="t"),
            options={"rows": rows},
        )
        self.assertEqual(rows, PostgresSourceAdapter().scan(source, {})[0].records)
        self.assertEqual(
            [{"a": 1}, {"b": 2}], PostgresSourceAdapter(omit_null_keys=True).scan(source, {})[0].records
        )

    def test_the_default_resolver_still_reads_mock_rows(self) -> None:
        source = postgres_source()
        resolver = DefaultSourceResolver()
        resolver.register(source.model_copy(update={"options": {"rows": [{"id": 1}]}}))
        self.assertEqual([{"id": 1}], resolver.materialize(DatasetInput(source_id="orders"))[0].records)


class RedactSourceTests(unittest.TestCase):
    """A source definition returned to a caller never shows a credential."""

    def test_credentials_and_targets_are_replaced_and_safe_keys_kept(self) -> None:
        options = {
            "role": "tenant_a",
            "sslmode": "require",
            "connect_timeout": 5,
            "password": SECRET,
            "user": "owner",
            "host": "db.internal.example",
            "dbname": "app",
            "rows": [{"id": 1}],
            "something_new": "unknown keys are hidden too",
        }
        source = postgres_source(options)
        shown = redact_source(source)
        self.assertEqual(
            {
                "role": "tenant_a",
                "sslmode": "require",
                "connect_timeout": 5,
                "password": REDACTED,
                "user": REDACTED,
                "host": REDACTED,
                "dbname": REDACTED,
                "rows": REDACTED,
                "something_new": REDACTED,
            },
            shown.connection_ref.options,
        )
        self.assertNotIn(SECRET, shown.model_dump_json())
        # The stored definition keeps its values; only the copy is redacted.
        self.assertEqual(SECRET, source.connection_ref.options["password"])
        self.assertEqual(source.selection, shown.selection)

    def test_a_source_without_options_comes_back_the_same(self) -> None:
        for source in (
            postgres_source(),
            InlineSourceSpec(source_id="inline", selection={"records": [{"a": 1}]}),
        ):
            with self.subTest(kind=source.kind):
                self.assertEqual(source, redact_source(source))

    def test_an_inline_source_with_a_connection_is_redacted_too(self) -> None:
        source = InlineSourceSpec(
            source_id="inline",
            connection_ref=ConnectionRef(connection_id="store", options={"password": SECRET}),
        )
        self.assertEqual({"password": REDACTED}, redact_source(source).connection_ref.options)


class SiteRequestSourceTests(unittest.TestCase):
    """An LMA that serves a configured site accepts only inline sources from a request."""

    def assert_refused(self, source: object, *phrases: str) -> SourceRequestRefused:
        with self.assertRaises(SourceRequestRefused) as caught:
            check_site_request_source(source)  # type: ignore[arg-type]
        for phrase in phrases:
            self.assertIn(phrase, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        return caught.exception

    def test_the_error_is_a_forbidden_permission_error(self) -> None:
        error = self.assert_refused(postgres_source())
        self.assertIsInstance(error, PermissionError)
        self.assertIsInstance(error, DagentsError)
        self.assertEqual("forbidden", error.code)
        self.assertEqual(403, error.http_status)

    def test_a_postgres_source_is_refused_and_its_sql_named(self) -> None:
        self.assert_refused(postgres_source(), "postgres")
        self.assert_refused(postgres_source(sql="SELECT * FROM site_b.encounters"), "postgres", "SQL")
        self.assert_refused(postgres_source(table="t", where="true; DROP TABLE t"), "SQL")

    def test_sources_that_read_through_a_connection_or_a_file_are_refused(self) -> None:
        mongo = MongoSourceSpec(
            source_id="m",
            connection_ref=ConnectionRef(connection_id="store"),
            selection={"database": "d", "collection": "c"},
        )
        files = ObjectStorageSourceSpec(
            source_id="f",
            connection_ref=ConnectionRef(connection_id="files"),
            selection={"uri": "file:///etc/passwd"},
            format="csv",
        )
        self.assert_refused(mongo, "mongodb")
        self.assert_refused(files, "object_storage")

    def test_roles_and_credentials_are_refused_in_any_source(self) -> None:
        source = InlineSourceSpec(
            source_id="inline",
            connection_ref=ConnectionRef(
                connection_id="store", options={"role": "tenant_b", "password": SECRET, "host": "elsewhere"}
            ),
            selection={"records": [{"a": 1}]},
        )
        self.assert_refused(source, "host", "password", "role")
        self.assert_refused(postgres_source({"role": "tenant_b"}), "role")

    def test_an_inline_source_is_accepted(self) -> None:
        check_site_request_source(InlineSourceSpec(source_id="inline", selection={"records": [{"a": 1}]}))
        # Fixed records in the connection options are data, not a credential.
        check_site_request_source(
            InlineSourceSpec(
                source_id="inline", connection_ref=ConnectionRef(connection_id="x", options={"rows": [{"a": 1}]})
            )
        )


class SiteRequestDatasetTests(unittest.TestCase):
    def test_an_embedded_source_is_refused(self) -> None:
        dataset = DatasetInput(source=InlineSourceSpec(source_id="inline", selection={"records": [{"a": 1}]}))
        with self.assertRaises(SourceRequestRefused) as caught:
            check_site_request_dataset(dataset)
        self.assertIn("source", str(caught.exception))

    def test_inline_records_and_registered_sources_are_accepted(self) -> None:
        check_site_request_dataset(DatasetInput(inline_records=[{"a": 1}]))
        check_site_request_dataset(DatasetInput(source_id="site-records"))


class StaticSiteSourceTests(unittest.TestCase):
    """A site source that holds its records in memory, for examples and tests."""

    def test_it_has_every_member_of_the_port(self) -> None:
        members = [
            name
            for name, value in vars(ports.SiteRecordSource).items()
            if inspect.isfunction(value) and not name.startswith("_")
        ]
        self.assertTrue(members, "SiteRecordSource declares no methods")
        source = StaticSiteSource([])
        for name in members:
            with self.subTest(member=name):
                self.assertTrue(callable(getattr(source, name, None)), f"StaticSiteSource has no {name}()")

    def test_records_are_copies_in_the_given_order(self) -> None:
        given = [{"id": 2, "tags": ["b"]}, {"id": 1, "tags": ["a"]}]
        source = StaticSiteSource(given)
        given[0]["id"] = 99
        given.append({"id": 3})
        read = source.records()
        self.assertEqual([{"id": 2, "tags": ["b"]}, {"id": 1, "tags": ["a"]}], read)
        read[0]["tags"].append("changed")
        read.pop()
        self.assertEqual([{"id": 2, "tags": ["b"]}, {"id": 1, "tags": ["a"]}], source.records())
        self.assertEqual(2, source.count())

    def test_the_version_changes_only_when_the_records_change(self) -> None:
        source = StaticSiteSource([{"id": 1}])
        first = source.version()
        self.assertIsInstance(first, str)
        source.records()
        source.count()
        self.assertEqual(first, source.version())
        source.append({"id": 2})
        second = source.version()
        self.assertNotEqual(first, second)
        self.assertEqual(2, source.count())
        # Same number of records, different content: the version still changes.
        source.replace([{"id": 3}, {"id": 4}])
        self.assertNotEqual(second, source.version())
        self.assertEqual([{"id": 3}, {"id": 4}], source.records())

    def test_it_names_a_memory_source_without_a_role(self) -> None:
        source = StaticSiteSource([])
        self.assertEqual("memory", source.kind)
        self.assertIsNone(source.role)
        self.assertEqual({}, source.location())
        named = StaticSiteSource([], location={"name": "fixture"})
        self.assertEqual({"name": "fixture"}, named.location())
        location = named.location()
        location["name"] = "changed"
        self.assertEqual({"name": "fixture"}, named.location())

    def test_close_keeps_the_records(self) -> None:
        source = StaticSiteSource([{"id": 1}])
        source.close()
        self.assertEqual([{"id": 1}], source.records())

    def test_records_must_be_objects(self) -> None:
        with self.assertRaises(TypeError):
            StaticSiteSource([{"id": 1}, "not a record"])  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            StaticSiteSource([]).append(["not", "a", "record"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
