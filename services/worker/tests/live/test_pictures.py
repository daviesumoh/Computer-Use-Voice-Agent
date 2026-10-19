"""Whether the real model asks for a picture only when the text cannot answer (a few cents).

Run with `uv run pytest -m live -s tests/live/test_pictures.py`."""

from collections.abc import Iterator

import pytest
from openai import AsyncOpenAI

from tests.browser.conftest import Site, serve
from worker.browser.actions import Look, Navigate
from worker.browser.decider import Brief, OpenAIDecider
from worker.browser.session import BrowserSession
from worker.settings import Settings

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    yield from serve()


async def decide(site: Site, path: str, task: str) -> tuple[object, OpenAIDecider, BrowserSession]:
    settings = Settings(_env_file=".env")  # type: ignore[call-arg]
    if settings.openai_api_key is None:
        pytest.skip("WORKER_OPENAI_API_KEY is not set")
    decider = OpenAIDecider(
        AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value()), settings.browser_model
    )
    browser = BrowserSession(headless=True, allow_private_hosts=True)
    await browser.__aenter__()
    await browser.perform(Navigate(kind="navigate", url=site.url(path)), None)
    brief = Brief(task, {}, [], await browser.observe())
    return (await decider.decide(brief)).action, decider, browser


async def test_a_figure_painted_on_a_canvas_is_read_from_a_picture(site: Site) -> None:
    action, decider, browser = await decide(site, "/chart", "Find the Q3 revenue on this page")
    try:
        assert isinstance(action, Look), action
        reading = await decider.look(action.question, await browser.screenshot(action.detail), "x")
        assert "42" in reading.text
    finally:
        await browser.__aexit__(None, None, None)


async def test_a_price_in_the_page_text_is_not_looked_for_in_a_picture(site: Site) -> None:
    action, _, browser = await decide(site, "/product/2", "What does the Cirrus 65 TV cost?")
    await browser.__aexit__(None, None, None)
    assert not isinstance(action, Look), action


async def test_a_chart_on_the_page_does_not_tempt_it_when_the_answer_is_in_the_text(
    site: Site,
) -> None:
    site.app.state.pages["chart-and-price"] = (
        "<h1>Aurora 55 TV</h1><p>Price: $499.99</p><canvas width=400 height=200></canvas>"
    )
    action, _, browser = await decide(
        site, "/page/chart-and-price", "What is the price of the Aurora 55 TV?"
    )
    await browser.__aexit__(None, None, None)
    assert not isinstance(action, Look), action
