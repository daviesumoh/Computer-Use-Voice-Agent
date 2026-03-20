import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from worker.browser_smoke import load_pages, p95

pytestmark = pytest.mark.browser

PAGE = (
    b"""<!doctype html><html><head>
<style>@font-face { font-family: X; src: url(/font.woff2); } body { font-family: X; }</style>
</head><body><h1>fixture</h1><video src="/clip.mp4" autoplay></video>
"""
    + b"<p>product listing</p>" * 1000
    + b"</body></html>"
)
CHALLENGE = b"<html><body>Checking your browser...</body></html>"


class FixtureServer(ThreadingHTTPServer):
    requested_paths: list[str]


class Handler(BaseHTTPRequestHandler):
    server: FixtureServer

    def do_GET(self) -> None:
        self.server.requested_paths.append(self.path)
        status, body = {
            "/": (200, PAGE),
            "/denied": (403, b"Access Denied"),
            "/challenge": (200, CHALLENGE),
        }.get(self.path, (200, b"\x00" * 1024))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # keep test output clean
        pass


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    srv = FixtureServer(("127.0.0.1", 0), Handler)
    srv.requested_paths = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def test_loads_page_and_never_fetches_fonts_or_media(server: FixtureServer) -> None:
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    (result,) = load_pages([url], timeout_ms=10_000, headless=True)

    assert result.error is None
    assert result.load_seconds is not None
    assert result.status == 200
    assert "/font.woff2" not in server.requested_paths
    assert "/clip.mp4" not in server.requested_paths


def test_unreachable_site_is_reported_not_raised() -> None:
    # Port 9 (discard) is closed on test machines; the run must continue and record it.
    results = load_pages(["http://127.0.0.1:9/"], timeout_ms=5_000, headless=True)

    assert results[0].load_seconds is None
    assert results[0].error is not None


def test_refused_and_challenge_pages_count_as_failures_not_loads(server: FixtureServer) -> None:
    base = f"http://127.0.0.1:{server.server_address[1]}"

    denied, challenge = load_pages(
        [f"{base}/denied", f"{base}/challenge"], timeout_ms=10_000, headless=True
    )

    assert denied.load_seconds is None
    assert denied.status == 403
    assert denied.error is not None and "refused" in denied.error
    assert challenge.load_seconds is None
    assert challenge.status == 200
    assert challenge.error is not None and "bot-challenge" in challenge.error


def test_p95_of_single_sample_is_that_sample() -> None:
    assert p95([3.2]) == 3.2
