#!/usr/bin/env python3
"""Check the link previews of the published sites, before and after the deploy.

A link to one of the sites, shared on LinkedIn, WhatsApp, Slack or X, shows the
image that the page names in its `og:image` tag. Crawlers ignore a relative
address. So each build writes its address from `env/.env.published` into the
tags (the `social-meta.mjs` Vite plugin), and the Pages workflow runs this
script three times:

| Command | Step in `pages.yml` | Fails when |
|---|---|---|
| `addresses` | before the builds | an address is missing, is not https, does not end in `/`, carries a query string, a fragment, credentials or a space, or is not under the framework site's address |
| `built` | before the upload | a built page lacks the absolute `og:url`, `og:image`, `og:image:secure_url` or `twitter:image`, still holds the placeholder, or its card (a 1200 x 630 PNG under 300 KB), an icon or its web manifest is missing |
| `published` | after the deploy | a live page names no `og:image`, names another address, or the image does not answer 200 with an image content type |

Usage::

    python scripts/check_link_previews.py addresses --github-env "$GITHUB_ENV"
    python scripts/check_link_previews.py built --tree _site
    python scripts/check_link_previews.py published

Each command prints one line per site, `ok` or `FAIL`, and an `::error::`
line for each problem. `tests/test_social_previews.py` runs each command on
good and broken inputs, the last one against a local stand-in server.

The script uses the standard library only, because the deploy job installs
nothing.

Exit codes: 0 when every site passes, 1 when one fails, 2 for wrong usage.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import struct
import sys
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = REPO_ROOT / "env" / ".env.published"

#: Each published site's address in env/.env.published. The first is the
#: framework site; the Pages workflow publishes the demos inside its tree.
KEYS = ("DAGENTS_PUBLIC_SITE_URL", "HEALTHCARE_DEMO_PUBLIC_SITE_URL", "NL2SQL_DEMO_PUBLIC_SITE_URL")

PLACEHOLDER = "%PUBLIC_SITE_URL%"
CARD = "social-preview.png"
CARD_SIZE = (1200, 630)
#: WhatsApp drops a preview image above about 300 KB.
MAX_CARD_BYTES = 300 * 1024
ICONS = ("favicon.svg", "favicon-32.png", "apple-touch-icon.png", "icon-512.png", "site.webmanifest")
#: The tags that must hold an absolute address, and the file each one names.
ABSOLUTE_TAGS = {"og:url": "", "og:image": CARD, "og:image:secure_url": CARD, "twitter:image": CARD}
USER_AGENT = "Dagents link preview check (https://github.com/pradyunuydarp/Dagents)"


class MetaTags(HTMLParser):
    """The content of every `<meta>` tag, by its `property` or `name`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = {key: value or "" for key, value in attrs}
        key = values.get("property") or values.get("name")
        if key:
            self.tags.setdefault(key, []).append(values.get("content", ""))


def meta_tags(html: str) -> dict[str, list[str]]:
    parser = MetaTags()
    parser.feed(html)
    return parser.tags


def read_published(path: Path) -> dict[str, str]:
    """Every `KEY=value` line of an env file; the last line wins, as in the shell."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        # Read like the shell: the value is everything after the first "=", spaces included,
        # so an address with a stray space fails here instead of passing.
        if sep and key and not key.startswith("#"):
            values[key] = value.rstrip("\r")
    return values


def address_problems(key: str, value: str, root: str) -> list[str]:
    """Why a published address cannot go into a page's tags; empty when it can."""
    if not value:
        return [f"{key} is missing from env/.env.published. The link preview tags are built from it."]
    parts = urlsplit(value)
    problems = []
    if parts.scheme != "https" or not parts.netloc:
        problems.append(f"{key} must be an https address: '{value}'. Crawlers need an https og:image.")
    if not value.endswith("/"):
        problems.append(f"{key} must end in /: '{value}'.")
    if parts.query or parts.fragment or parts.username or parts.password or any(c in value for c in "?#@ "):
        problems.append(f"{key} must be a plain address, without a query string, fragment, credentials or space: '{value}'.")
    if key != KEYS[0] and root and not value.startswith(root):
        problems.append(f"{key} ({value}) is not under the framework site ({root}), which holds the demos.")
    return problems


def report(key: str, detail: str, problems: list[str]) -> bool:
    for problem in problems:
        print(f"::error::{problem}")
    print(f"{'ok  ' if not problems else 'FAIL'}  {key}: {detail}")
    return not problems


def check_addresses(published: dict[str, str], github_env: Path | None) -> int:
    root = published.get(KEYS[0], "")
    passed = [report(key, published.get(key, "(missing)"), address_problems(key, published.get(key, ""), root)) for key in KEYS]
    if not all(passed):
        return 1
    if github_env is not None:
        with github_env.open("a", encoding="utf-8") as handle:
            for key in KEYS:
                handle.write(f"{key}={published[key]}\n")
    return 0


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def built_page_problems(tree: Path, url: str, root: str) -> list[str]:
    folder = tree / url[len(root):]
    page = folder / "index.html"
    if not page.is_file():
        return [f"{page} is missing."]
    html = page.read_text(encoding="utf-8")
    tags = meta_tags(html)
    problems = []
    for tag, file in ABSOLUTE_TAGS.items():
        expected = f"{url}{file}"
        found = tags.get(tag, [])
        if found != [expected]:
            hint = " A relative address means the build had no PUBLIC_SITE_URL." if any(
                value.startswith("/") for value in found) else ""
            problems.append(f"{page}: {tag} is {found or 'missing'}, not ['{expected}'].{hint}")
    if PLACEHOLDER in html:
        problems.append(f"{page} still holds the {PLACEHOLDER} placeholder.")
    card = folder / CARD
    if not card.is_file():
        problems.append(f"{card} is missing.")
    else:
        data = card.read_bytes()
        size = png_size(data)
        if size != CARD_SIZE or len(data) >= MAX_CARD_BYTES:
            problems.append(
                f"{card} is {'not a PNG' if size is None else f'{size[0]} x {size[1]}'} and {len(data)} bytes; "
                f"it must be a {CARD_SIZE[0]} x {CARD_SIZE[1]} PNG under {MAX_CARD_BYTES} bytes."
            )
    for file in ICONS:
        icon = folder / file
        if not icon.is_file() or icon.stat().st_size == 0:
            problems.append(f"{icon} is missing or empty.")
    return problems


def check_built(published: dict[str, str], tree: Path) -> int:
    root = published.get(KEYS[0], "")
    passed = []
    for key in KEYS:
        url = published.get(key, "")
        problems = address_problems(key, url, root) or built_page_problems(tree, url, root)
        passed.append(report(key, f"{url}{CARD}", problems))
    return 0 if all(passed) else 1


@dataclass(frozen=True)
class Answer:
    """What a GET returned. `status` is 0 when nothing answered, and `error` says why."""

    status: int
    content_type: str = ""
    body: bytes = b""
    error: str = ""

    def describe(self) -> str:
        if not self.status:
            return f"no answer ({self.error})"
        return f"{self.status} {self.content_type or 'no content type'}, {len(self.body)} bytes"


def fetch(url: str, timeout: float) -> Answer:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return Answer(response.status, response.headers.get("Content-Type", ""), response.read())
    except HTTPError as error:
        return Answer(error.code, error.headers.get("Content-Type", "") if error.headers else "")
    except (URLError, OSError) as error:
        return Answer(0, error=str(getattr(error, "reason", error)))


def fetch_until(url: str, done: Callable[[Answer], bool], attempts: int, delay: float, timeout: float) -> Answer:
    """GET until `done` holds or the attempts run out. Pages can take a moment to serve a new deploy."""
    answer = fetch(url, timeout)
    for _ in range(attempts - 1):
        if done(answer):
            break
        time.sleep(delay)
        answer = fetch(url, timeout)
    return answer


def page_images(answer: Answer) -> list[str]:
    return meta_tags(answer.body.decode("utf-8", "replace")).get("og:image", []) if answer.status == 200 else []


def is_image(answer: Answer) -> bool:
    return answer.status == 200 and answer.content_type.startswith("image/") and len(answer.body) > 0


def published_page_problems(url: str, attempts: int, delay: float, timeout: float) -> tuple[str, list[str]]:
    """Read the live page's og:image and request it. Returns a one-line result and the problems."""
    expected = f"{url}{CARD}"
    page = fetch_until(url, lambda answer: bool(page_images(answer)), attempts, delay, timeout)
    if page.status != 200:
        return expected, [f"{url} returned {page.describe()}, so its og:image cannot be read."]
    images = page_images(page)
    if images != [expected]:
        return expected, [
            f"{url} names og:image {images or 'nowhere'}, not ['{expected}']. A shared link to it shows no picture."
        ]
    image = fetch_until(expected, is_image, attempts, delay, timeout)
    if not is_image(image):
        return f"{image.describe()}  {expected}", [
            f"{expected}, the og:image of {url}, returned {image.describe()}, not 200 with an image."
        ]
    return f"{image.describe()}  {expected}", []


def check_published(published: dict[str, str], attempts: int, delay: float, timeout: float) -> int:
    passed = []
    for key in KEYS:
        url = published.get(key, "")
        if not url:
            passed.append(report(key, "(missing)", [f"{key} is missing from env/.env.published."]))
            continue
        detail, problems = published_page_problems(url, attempts, delay, timeout)
        passed.append(report(key, detail, problems))
    return 0 if all(passed) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--published", type=Path, default=PUBLISHED, help="the env file with the addresses")
    commands = parser.add_subparsers(dest="command", required=True)
    addresses = commands.add_parser("addresses", help="check each published address before the builds")
    addresses.add_argument("--github-env", type=Path, help="append KEY=value lines here when every address passes")
    built = commands.add_parser("built", help="check each built page before the upload")
    built.add_argument("--tree", type=Path, required=True, help="the assembled tree, such as _site")
    live = commands.add_parser("published", help="request each published page's og:image after the deploy")
    live.add_argument("--attempts", type=int, default=5)
    live.add_argument("--delay", type=float, default=5.0, help="seconds between attempts")
    live.add_argument("--timeout", type=float, default=30.0, help="seconds per request")
    args = parser.parse_args(argv)

    published = read_published(args.published)
    if args.command == "addresses":
        return check_addresses(published, args.github_env)
    if args.command == "built":
        return check_built(published, args.tree)
    return check_published(published, max(1, args.attempts), args.delay, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
