# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi>=0.7", "beautifulsoup4>=4.12"]
# ///
"""One-off scraper for the Geekbench Browser CPU search.

Fetches https://browser.geekbench.com/v6/cpu/search?q=<query> across one or
more result pages, parses every single-core / multi-core score pair on the
page, and prints aggregate statistics. The recommended single number is the
**median** — Geekbench Browser uploads are crowdsourced and the distribution
has heavy upper tails (thermal throttling, background load on the user's
machine, etc.), so the mean is dragged down by bad runs. Median rejects that
without any tuning.

Cloudflare anti-bot: the Geekbench Browser rejects default ``requests`` /
``urllib`` user agents and serves a JS challenge. We work around that with
``curl_cffi``, which sends Chrome's actual TLS fingerprint; most of the
time that clears the challenge silently. If it doesn't on your network, run
the script with ``--html-file`` and pass a page you saved from a real
browser (Cmd-S on the search results page).

Usage
-----

    # Default (auto-fetch, 5 pages = ~125 results)
    uv run scripts/scrape_geekbench.py "Mac17,8"

    # Fetch more pages
    uv run scripts/scrape_geekbench.py "Mac17,8" --pages 10

    # Manual override (if Cloudflare wins)
    #   1. Open https://browser.geekbench.com/v6/cpu/search?q=Mac17%2C8 in a browser
    #   2. Cmd-S to save the page as HTML
    #   3. Pass the file:
    uv run scripts/scrape_geekbench.py --html-file ~/Downloads/page.html

The script never writes to geekbench.toml on its own — copy the printed
median into the right table by hand. That keeps the data file an
operator-curated source of truth.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

_BASE = "https://browser.geekbench.com"
_SEARCH_PATH = "/v6/cpu/search"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)
# Score sanity bounds: Geekbench 6 single-core ranges roughly 200-5000,
# multi-core 500-60000. We accept anything within a generous envelope and
# reject pairs that fail "multi > single" since that almost always means we
# misidentified the columns.
_SINGLE_RANGE = (100, 10000)
_MULTI_RANGE = (200, 200000)


@dataclass(frozen=True)
class Score:
    single: int
    multi: int


def main() -> int:
    args = _parse_args()
    if args.html_file:
        html_pages = [Path(args.html_file).read_text(encoding="utf-8", errors="replace")]
        print(f"# Loaded {len(html_pages[0])} bytes from {args.html_file}", file=sys.stderr)
    else:
        html_pages = _fetch_pages(args.query, args.pages, args.delay)
    scores = _parse_pages(html_pages)
    if not scores:
        print(
            "No score pairs parsed. Most likely causes:\n"
            "  1. Cloudflare blocked the fetch — re-run with --html-file (see --help).\n"
            "  2. The page structure changed — open one of the pages and adjust the\n"
            "     selectors in _parse_one_page().",
            file=sys.stderr,
        )
        return 1
    _report(scores, args.query if not args.html_file else args.html_file)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scrape_geekbench.py",
        description="Scrape Geekbench Browser CPU search and aggregate scores.",
    )
    p.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Search query, e.g. 'Mac17,8' or 'Apple M5 Pro'.",
    )
    p.add_argument(
        "--pages",
        type=int,
        default=5,
        help="Number of result pages to fetch (default: 5; ~25 results each).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to sleep between page fetches (default: 1.5).",
    )
    p.add_argument(
        "--html-file",
        type=str,
        default=None,
        help="Parse a locally-saved HTML file instead of fetching from the server.",
    )
    args = p.parse_args()
    if not args.query and not args.html_file:
        p.error("either a query or --html-file is required")
    return args


def _fetch_pages(query: str, n_pages: int, delay: float) -> list[str]:
    """Pull search result pages via curl_cffi (Chrome impersonation).

    Imports are deferred so the --html-file path doesn't need the network dep.
    """
    from curl_cffi import requests as cffi_requests  # noqa: PLC0415

    out: list[str] = []
    encoded = quote_plus(query)
    for page in range(1, n_pages + 1):
        url = f"{_BASE}{_SEARCH_PATH}?q={encoded}&page={page}"
        print(f"# Fetching {url}", file=sys.stderr)
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
        )
        if resp.status_code != 200:
            print(
                f"# WARNING: page {page} returned HTTP {resp.status_code}; "
                "stopping pagination",
                file=sys.stderr,
            )
            break
        text = resp.text
        if "Just a moment" in text and "challenge" in text.lower():
            print(
                "# WARNING: hit a Cloudflare JS challenge. Re-run with --html-file.",
                file=sys.stderr,
            )
            break
        out.append(text)
        if page < n_pages:
            time.sleep(delay)
    return out


def _parse_pages(html_pages: list[str]) -> list[Score]:
    seen: set[tuple[int, int]] = set()
    scores: list[Score] = []
    for html in html_pages:
        for s in _parse_one_page(html):
            key = (s.single, s.multi)
            # Don't dedupe exact pairs — two real runs can land on the same
            # integer scores. Just keep them all.
            seen.add(key)
            scores.append(s)
    return scores


def _parse_one_page(html: str) -> list[Score]:
    """Pull (single, multi) score pairs from one Geekbench search-result page.

    Geekbench Browser's result rows have varied over time; we try selectors in
    rough order of specificity, then fall back to a permissive heuristic over
    the raw text. The fallback is what makes the script robust to layout
    tweaks — if Geekbench reshuffles the DOM tomorrow, the score-pair regex
    still works as long as the page still shows "Single-Core Score" /
    "Multi-Core Score" labels.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1 — labeled fields. Search-result rows almost always render
    # something like "Single-Core Score: 3854 Multi-Core Score: 20313" inside
    # a result container. Scoop those out of the entire page text.
    text = " ".join(soup.stripped_strings)
    scores = _scan_labeled_pairs(text)
    if scores:
        return scores

    # Strategy 2 — table cells. Some Geekbench pages render a table where
    # consecutive cells are single, multi. Iterate <tr>s and look for two
    # adjacent integer cells.
    return _scan_table_rows(soup)


_PAIR_RE = re.compile(
    r"Single[- ]Core\s*(?:Score)?[^0-9]{0,12}(\d{2,6})"
    r".{0,200}?"
    r"Multi[- ]Core\s*(?:Score)?[^0-9]{0,12}(\d{2,6})",
    re.IGNORECASE | re.DOTALL,
)


def _scan_labeled_pairs(text: str) -> list[Score]:
    out: list[Score] = []
    for m in _PAIR_RE.finditer(text):
        try:
            single = int(m.group(1))
            multi = int(m.group(2))
        except (TypeError, ValueError):
            continue
        if _valid_pair(single, multi):
            out.append(Score(single=single, multi=multi))
    return out


def _scan_table_rows(soup: BeautifulSoup) -> list[Score]:
    out: list[Score] = []
    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        ints: list[int] = []
        for c in cells:
            if c.isdigit():
                ints.append(int(c))
        # Look for the first valid (single, multi) pair within the row's ints
        for i in range(len(ints) - 1):
            a, b = ints[i], ints[i + 1]
            if _valid_pair(a, b):
                out.append(Score(single=a, multi=b))
                break
    return out


def _valid_pair(single: int, multi: int) -> bool:
    if not (_SINGLE_RANGE[0] <= single <= _SINGLE_RANGE[1]):
        return False
    if not (_MULTI_RANGE[0] <= multi <= _MULTI_RANGE[1]):
        return False
    # On multi-core chips, multi > single is essentially universal; reject
    # pairs that flunk this sanity check (almost always misidentified columns).
    return multi > single


def _report(scores: list[Score], label: str) -> None:
    singles = sorted(s.single for s in scores)
    multis = sorted(s.multi for s in scores)
    print(f"# Query: {label}")
    print(f"# Samples: {len(scores)}")
    print("#")
    print("#                    single-core    multi-core")
    print(f"#   median           {statistics.median(singles):>10}    {statistics.median(multis):>10}")
    print(f"#   mean             {statistics.mean(singles):>10.0f}    {statistics.mean(multis):>10.0f}")
    print(f"#   min              {min(singles):>10}    {min(multis):>10}")
    print(f"#   max              {max(singles):>10}    {max(multis):>10}")
    if len(scores) >= 4:
        sq = statistics.quantiles(singles, n=4, method="inclusive")
        mq = statistics.quantiles(multis, n=4, method="inclusive")
        print(f"#   Q1               {sq[0]:>10.0f}    {mq[0]:>10.0f}")
        print(f"#   Q3               {sq[2]:>10.0f}    {mq[2]:>10.0f}")
        print(f"#   IQR              {sq[2] - sq[0]:>10.0f}    {mq[2] - mq[0]:>10.0f}")
    print()
    print("# Recommended values (median, robust to throttled/background-loaded uploads):")
    print(f"single_core = {int(statistics.median(singles))}")
    print(f"multi_core  = {int(statistics.median(multis))}")


if __name__ == "__main__":
    raise SystemExit(main())
