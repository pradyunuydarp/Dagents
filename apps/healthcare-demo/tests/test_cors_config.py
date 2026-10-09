"""What this deployment will let a browser do, and how it reads the setting.

CORS is the one piece of configuration whose being wrong is invisible from
outside the browser: the request is refused before it arrives, so the page shows
nothing and the service looks down. A deployed API that blocked every request
from the site it was deployed for is what these tests exist to prevent.

Pure string handling, so none of this needs the planner or a database.
"""

from __future__ import annotations

import unittest

from app.core.config import normalize_origins


class NormalizeOriginsTests(unittest.TestCase):
    def test_a_page_url_is_reduced_to_its_origin(self) -> None:
        """The mistake that cannot ever match.

        A browser's `Origin` header is scheme, host and port — never a path. An
        allowlist entry copied from the address bar of the published demo
        therefore matches nothing at all.
        """
        self.assertEqual(
            ["https://example.github.io"],
            normalize_origins("https://example.github.io/Dagents/healthcare-demo/"),
        )

    def test_a_trailing_slash_is_dropped(self) -> None:
        self.assertEqual(["https://example.github.io"], normalize_origins("https://example.github.io/"))

    def test_wrapping_punctuation_is_stripped(self) -> None:
        """The one that actually happened.

        The deployed service held `https://pradyunuydarp.github.io)` — a stray
        closing parenthesis from a pasted link. No origin a browser sends ends
        in one, so the API refused every request from the published page while
        looking perfectly healthy from a plain curl.
        """
        for written in (
            "https://example.github.io)",
            "(https://example.github.io)",
            "<https://example.github.io>",
            "'https://example.github.io',",
        ):
            with self.subTest(written=written):
                self.assertEqual(["https://example.github.io"], normalize_origins(written))

    def test_an_uppercased_host_is_lowered(self) -> None:
        """Browsers send the host lowercased and the comparison is exact."""
        self.assertEqual(["https://example.github.io"], normalize_origins("HTTPS://Example.GitHub.IO"))

    def test_an_ipv6_origin_keeps_its_brackets(self) -> None:
        """Stripping `]` as punctuation would corrupt a correct value."""
        self.assertEqual(["http://[::1]:8080"], normalize_origins("http://[::1]:8080"))

    def test_a_port_is_kept_because_it_is_part_of_the_origin(self) -> None:
        self.assertEqual(["http://127.0.0.1:5174"], normalize_origins("http://127.0.0.1:5174"))

    def test_several_entries_keep_their_order_and_lose_duplicates(self) -> None:
        self.assertEqual(
            ["https://a.example", "https://b.example"],
            normalize_origins("https://a.example, https://b.example/, https://a.example/x"),
        )

    def test_the_wildcard_passes_through(self) -> None:
        """`*` is not a URL, and parsing it as one would throw it away."""
        self.assertEqual(["*"], normalize_origins("*"))

    def test_empty_means_no_opinion(self) -> None:
        """The caller turns this into the permissive default, not this function."""
        self.assertEqual([], normalize_origins(""))
        self.assertEqual([], normalize_origins(" , ,"))

    def test_something_that_is_not_a_url_is_kept_as_written(self) -> None:
        """Visibly wrong beats silently dropped: the status endpoint shows it."""
        self.assertEqual(["github.io"], normalize_origins("github.io"))

    def test_an_entry_whose_host_is_mangled_is_kept_as_written(self) -> None:
        """The limit on repairing configuration.

        Noise at the ends is a paste artifact with one obvious reading. Noise in
        the middle is not, so it is left alone and reported rather than guessed
        at — which is why `/api/v1/framework/status` prints this list.
        """
        self.assertEqual(["https://exa mple.github.io"], normalize_origins("https://exa mple.github.io"))


class SettingsOriginsTests(unittest.TestCase):
    def test_an_unset_value_allows_any_origin(self) -> None:
        """Right for a local run and for a credential-free public demo."""
        from app.core.config import Settings

        self.assertEqual(["*"], Settings(cors_origins="").allowed_origins())

    def test_a_configured_value_is_normalized_not_passed_through(self) -> None:
        from app.core.config import Settings

        settings = Settings(cors_origins="https://example.github.io/Dagents")
        self.assertEqual(["https://example.github.io"], settings.allowed_origins())


if __name__ == "__main__":
    unittest.main()
