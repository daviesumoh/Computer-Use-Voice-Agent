"""When the agent takes a picture, and when it must not: real Chromium, real Postgres."""

from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from tests.browser.conftest import Site
from tests.browser.support import ScriptedDecider, events
from worker.browser.actions import Done, Look, Navigate
from worker.browser.agent import MAX_LOOKS, BrowserExecutor
from worker.browser.errors import PictureRefusedError
from worker.browser.session import BrowserSession

pytestmark = pytest.mark.browser

JPEG = b"\xff\xd8\xff"


def look(question: str = "What is the Q3 revenue?", detail: str = "coarse") -> Look:
    return Look.model_validate({"kind": "look", "question": question, "detail": detail})


async def open_page(browser: BrowserSession, site: Site, path: str) -> None:
    await browser.perform(Navigate(kind="navigate", url=site.url(path)), None)


async def test_a_page_with_a_drawn_chart_says_the_text_view_is_missing_something(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/chart")
    assert (await browser.observe()).visuals == "1 drawn canvas"


async def test_an_ordinary_text_page_carries_no_hint_and_is_not_sparse(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/products")
    seen = await browser.observe()
    assert (seen.visuals, seen.sparse) == ("", False)


async def test_a_blank_page_is_reported_as_nearly_empty(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/blank")
    assert (await browser.observe()).sparse


async def test_a_screenshot_is_a_jpeg_and_fine_detail_is_larger(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/chart")
    coarse, fine = await browser.screenshot("coarse"), await browser.screenshot("fine")
    assert coarse.startswith(JPEG) and fine.startswith(JPEG)
    assert len(fine) > len(coarse)


async def test_no_picture_is_taken_of_a_sign_in_form(browser: BrowserSession, site: Site) -> None:
    await open_page(browser, site, "/login")
    with pytest.raises(PictureRefusedError, match="sign-in form"):
        await browser.screenshot("coarse")


async def test_no_picture_is_taken_while_a_secret_is_on_screen(
    browser: BrowserSession, site: Site
) -> None:
    site.app.state.pages["greeting"] = "<h1>Signed in as ada.lovelace@example.com</h1>"
    await open_page(browser, site, "/page/greeting")
    browser.remember_secret("ada.lovelace@example.com")
    with pytest.raises(PictureRefusedError, match="secret store"):
        await browser.screenshot("coarse")


async def test_the_model_is_told_about_the_chart_then_reads_it_from_one_picture(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [
            Navigate(kind="navigate", url=site.url("/chart")),
            look(detail="fine"),
            Done(kind="done", result="42"),
        ]
    )
    executor, task_id = make_executor(decider), new_task()

    results = [await executor(task_id) for _ in range(3)]

    assert "1 drawn canvas" in decider.briefs[1].observation.visuals
    [(question, picture, detail)] = decider.pictures
    assert (question, detail) == ("What is the Q3 revenue?", "fine")
    assert picture.startswith(JPEG)
    # Only the answer, as text, reaches the model's later turns.
    assert "Q3 revenue is 42" in decider.briefs[2].history[-1]
    assert results[1].cost_cents == 1 + 1  # the decision and the reading are both billed
    routes = admin.execute(
        "select route from public.llm_calls where task_id = %s order by ts, id", (task_id,)
    ).fetchall()
    assert "browser_look" in [r["route"] for r in routes]


async def test_a_second_picture_of_an_unchanged_page_is_refused_and_costs_nothing(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider(
        [Navigate(kind="navigate", url=site.url("/chart")), look("First?"), look("Again?")]
    )
    executor, task_id = make_executor(decider), new_task()

    for _ in range(3):
        await executor(task_id)

    assert len(decider.pictures) == 1
    [failed] = events(admin, task_id, "step_failed")
    assert "has not changed" in failed["summary"]


async def test_a_task_gets_a_fixed_number_of_pictures(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    for i in range(MAX_LOOKS + 1):
        site.app.state.pages[f"p{i}"] = f"<h1>Report {i}</h1><canvas width=300 height=200></canvas>"
    plan: list[Navigate | Look] = []
    for i in range(MAX_LOOKS + 1):
        plan += [Navigate(kind="navigate", url=site.url(f"/page/p{i}")), look(f"Page {i}?")]
    decider = ScriptedDecider(list(plan))
    executor, task_id = make_executor(decider), new_task()

    for _ in plan:
        await executor(task_id)

    assert len(decider.pictures) == MAX_LOOKS
    assert "pictures" in events(admin, task_id, "step_failed")[-1]["summary"]


async def test_a_refused_picture_is_recorded_and_nothing_is_sent_to_the_model(
    make_executor: Callable[..., BrowserExecutor],
    new_task: Callable[[], UUID],
    site: Site,
    admin: psycopg.Connection[DictRow],
) -> None:
    decider = ScriptedDecider([Navigate(kind="navigate", url=site.url("/login")), look("Which?")])
    executor, task_id = make_executor(decider), new_task()

    await executor(task_id)
    await executor(task_id)

    assert decider.pictures == []
    [failed] = events(admin, task_id, "step_failed")
    assert "sign-in form" in failed["summary"]
