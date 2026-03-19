"""Go/no-go check for a worker host: can it load heavy sites in Chromium within its memory?

Run inside the deployed container:  xvfb-run browser-smoke --memory-limit-mb 1024
Exit code 0 = pass. Prints one JSON summary line.

The browser runs headed, as the worker will: sites such as walmart.ca and canadiantire.ca
reject Chromium's headless mode outright (its user agent says "HeadlessChrome").
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, sync_playwright

# Heavy pages that serve real content to a normal browser. Amazon and Best Buy answer
# automated browsers with challenge pages, which say nothing about memory use.
DEFAULT_URLS = [
    "https://www.walmart.ca/en",
    "https://www.canadiantire.ca/en.html",
    "https://www.theweathernetwork.com/ca",
]
# Same launch settings the worker will use: /dev/shm is tiny in most containers.
CHROMIUM_ARGS = ["--disable-dev-shm-usage"]
BLOCKED_RESOURCE_TYPES = {"media", "font"}
CGROUP_PEAK = Path("/sys/fs/cgroup/memory.peak")  # cgroup v2: peak usage of this container
# A real storefront is hundreds of KB of HTML; bot challenges are a few KB.
MIN_REAL_PAGE_BYTES = 10_000


@dataclass
class PageResult:
    url: str
    load_seconds: float | None
    status: int | None
    title: str | None
    html_bytes: int | None
    error: str | None


def _skip_heavy_resources(route: Route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _blocked_reason(status: int | None, html_bytes: int) -> str | None:
    if status is None or status >= 400:
        return f"HTTP {status}: site refused the browser"
    if status == 202 or html_bytes < MIN_REAL_PAGE_BYTES:
        return f"HTTP {status}, {html_bytes} bytes: likely a bot-challenge page"
    return None


def load_pages(urls: list[str], timeout_ms: int, headless: bool = False) -> list[PageResult]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chromium", headless=headless, args=CHROMIUM_ARGS)
        context = browser.new_context()
        context.route("**/*", _skip_heavy_resources)
        page = context.new_page()
        for url in urls:
            started = time.monotonic()
            try:
                response = page.goto(url, wait_until="load", timeout=timeout_ms)
            except PlaywrightError as exc:
                results.append(PageResult(url, None, None, None, None, str(exc).splitlines()[0]))
                continue
            elapsed = round(time.monotonic() - started, 2)
            status = response.status if response else None
            html_bytes = len(page.content())
            reason = _blocked_reason(status, html_bytes)
            results.append(
                PageResult(
                    url,
                    elapsed if reason is None else None,
                    status,
                    page.title()[:80],
                    html_bytes,
                    reason,
                )
            )
        browser.close()
    return results


def peak_memory_mb() -> float | None:
    """Container peak memory, or None when not running under cgroup v2 (e.g. macOS)."""
    try:
        return round(int(CGROUP_PEAK.read_text()) / 1024 / 1024, 1)
    except (FileNotFoundError, ValueError):
        return None


def p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=20, method="inclusive")[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    parser.add_argument("--memory-limit-mb", type=float, required=True)
    parser.add_argument("--max-p95-seconds", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    results = load_pages(args.urls, int(args.timeout_seconds * 1000))
    load_times = [r.load_seconds for r in results if r.load_seconds is not None]
    peak = peak_memory_mb()

    failures = [f"{r.url}: {r.error}" for r in results if r.error]
    if load_times and p95(load_times) > args.max_p95_seconds:
        failures.append(f"p95 load {p95(load_times):.1f}s > {args.max_p95_seconds}s")
    # Leave 15% headroom: the worker process itself shares this memory.
    if peak is not None and peak > 0.85 * args.memory_limit_mb:
        failures.append(f"peak memory {peak} MB > 85% of {args.memory_limit_mb} MB")

    summary = {
        "passed": not failures,
        "failures": failures,
        "peak_memory_mb": peak,
        "p95_load_seconds": round(p95(load_times), 2) if load_times else None,
        "pages": [asdict(r) for r in results],
    }
    print(json.dumps(summary))
    sys.exit(0 if not failures else 1)
