"""How accurately the agent reads pages, with the real model (costs a few cents).

Run with `uv run pytest -m live -s tests/live/test_perception.py`. Targets from the plan:
at least 90% of questions answered correctly, and every page that lacks the answer reported
as lacking it."""

import re
from collections.abc import Iterator

import pytest
from openai import AsyncOpenAI

from tests.browser.conftest import Site, serve
from tests.live.perception_pages import CASES, Case
from worker.browser.actions import Navigate
from worker.browser.decider import OpenAIDecider
from worker.browser.session import BrowserSession
from worker.settings import Settings

pytestmark = pytest.mark.live

ABSTAIN = re.compile(
    r"not (shown|listed|available|provided|found|displayed|visible|mentioned|stated|specified|"
    r"on the page|include|given)|isn.t (shown|listed|available|provided|on the page|stated)|"
    r"no .{0,40}(shown|listed|provided|given|displayed|available)|unavailable|"
    r"cannot|can.t|does not|doesn.t|don.t see|unable",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    yield from serve()


def correct(case: Case, answer: str) -> bool:
    if case.expected:
        return all(part.lower() in answer.lower() for part in case.expected)
    # Saying it is missing, or just not offering a figure ("call the store"), both count.
    declined = bool(ABSTAIN.search(answer)) or not re.search(r"\d", answer)
    return declined and not any(d in answer for d in case.decoys)


async def test_reading_thirty_pages(site: Site) -> None:
    settings = Settings(_env_file=".env")  # type: ignore[call-arg]
    if settings.openai_api_key is None:
        pytest.skip("WORKER_OPENAI_API_KEY is not set")
    decider = OpenAIDecider(
        AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value()), settings.browser_model
    )
    for case in CASES:
        site.app.state.pages[case.name] = case.html

    results: list[tuple[Case, str, bool]] = []
    cents = 0.0
    async with BrowserSession(headless=True, allow_private_hosts=True) as session:
        for case in CASES:
            await session.perform(
                Navigate(kind="navigate", url=site.url(f"/page/{case.name}")), None
            )
            await session.observe()  # lets late content draw, as the agent's own step would
            extraction = await decider.extract(case.question, await session.page_text())
            cents += extraction.usage.cost_cents
            results.append((case, extraction.text, correct(case, extraction.text)))

    for case, answer, ok in results:
        print(f"{'ok  ' if ok else 'FAIL'} {case.name}: {answer[:110]!r}")
    answerable = [r for r in results if r[0].expected]
    absent = [r for r in results if not r[0].expected]
    reading = sum(r[2] for r in answerable) / len(answerable)
    abstaining = sum(r[2] for r in absent) / len(absent)
    print(f"read correctly {reading:.0%} of {len(answerable)}; reported missing {abstaining:.0%} "
          f"of {len(absent)}; cost {cents:.1f} cents")  # fmt: skip
    assert reading >= 0.9
    assert abstaining >= 0.95
