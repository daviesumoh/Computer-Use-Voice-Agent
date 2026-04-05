"""BrowserSession against the chaos site, in a real Chromium."""

import pytest

from tests.browser.conftest import Site
from worker.browser.actions import Back, Click, Navigate, Press, Type
from worker.browser.errors import (
    ElementAmbiguousError,
    ElementNotFoundError,
    NeedsUserError,
    RecoverableError,
)
from worker.browser.session import BrowserSession
from worker.browser.snapshot import Node

pytestmark = pytest.mark.browser


async def open_page(browser: BrowserSession, site: Site, path: str) -> None:
    await browser.perform(Navigate(kind="navigate", url=site.url(path)), None)


async def node_named(browser: BrowserSession, role: str, name: str) -> Node:
    matches = [n for n in (await browser.nodes()).values() if (n.role, n.name) == (role, name)]
    assert matches, f"no {role} '{name}'"
    return matches[0]


async def test_observation_shows_the_page_with_refs(browser: BrowserSession, site: Site) -> None:
    await open_page(browser, site, "/products")
    seen = await browser.observe()
    assert seen.title == "TVs"
    assert "Aurora 55 TV" in seen.snapshot
    assert "$499.99" in seen.snapshot
    assert (await node_named(browser, "link", "Borealis 50 TV")).ref


async def test_clicking_a_link_navigates_and_says_where(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/products")
    link = await node_named(browser, "link", "Cirrus 65 TV")
    outcome = await browser.perform(Click(kind="click", ref=link.ref), link)
    assert outcome.endswith("/product/2")
    assert "Price: $799.50" in (await browser.observe()).snapshot


async def test_typing_into_a_search_box_and_pressing_enter(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/search")
    box = await node_named(browser, "textbox", "Search")
    await browser.perform(Type(kind="type", ref=box.ref, text="oled tv"), box)
    await browser.perform(Press(kind="press", key="Enter"), None)
    assert "Results for oled tv" in (await browser.observe()).snapshot


async def test_a_click_blocked_by_an_overlay_fails_recoverably_then_works_once_dismissed(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/overlay")
    details = await node_named(browser, "link", "Details")
    with pytest.raises(RecoverableError):
        await browser.perform(Click(kind="click", ref=details.ref), details)

    accept = await node_named(browser, "button", "Accept cookies")
    await browser.perform(Click(kind="click", ref=accept.ref), accept)
    await browser.perform(Click(kind="click", ref=details.ref), details)
    assert browser.page.url.endswith("/products")


async def test_a_ref_that_moved_is_re_found_when_the_match_is_unique(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/rerender")
    chosen = await node_named(browser, "link", "Details")
    await browser.page.wait_for_timeout(600)  # the link has been rebuilt: new element, new ref

    found = await browser.resolve(chosen.ref, chosen)
    assert (found.role, found.name) == ("link", "Details")
    await browser.perform(Click(kind="click", ref=found.ref), found)
    assert browser.page.url.endswith("/products")


async def test_a_ref_that_moved_is_refused_when_two_elements_now_match(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/rerender?twins=1")
    chosen = await node_named(browser, "link", "Details")
    await browser.page.wait_for_timeout(600)
    with pytest.raises(ElementAmbiguousError):
        await browser.resolve(chosen.ref, chosen)


async def test_a_ref_for_an_element_that_is_gone_is_reported(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/overlay")
    accept = await node_named(browser, "button", "Accept cookies")
    await browser.perform(Click(kind="click", ref=accept.ref), accept)
    with pytest.raises(ElementNotFoundError):
        await browser.resolve(accept.ref, accept)


async def test_controls_inside_frames_and_shadow_roots_are_reachable(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/frame")
    inside = await node_named(browser, "button", "In frame")
    await browser.perform(Click(kind="click", ref=inside.ref), inside)

    await open_page(browser, site, "/shadow")
    link = await node_named(browser, "link", "Shadow link")
    await browser.perform(Click(kind="click", ref=link.ref), link)
    assert browser.page.url.endswith("/products")


async def test_a_popup_tab_becomes_the_working_page_and_is_reported(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/popup")
    link = await node_named(browser, "link", "Open TVs")
    await browser.perform(Click(kind="click", ref=link.ref), link)
    seen = await browser.observe()
    assert seen.tabs == 2
    assert any("new tab" in notice for notice in seen.notices)
    assert seen.title == "TVs"


async def test_a_javascript_dialog_is_dismissed_and_reported(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/dialog")
    button = await node_named(browser, "button", "Show offer")
    await browser.perform(Click(kind="click", ref=button.ref), button)
    assert any("Subscribe now!" in n for n in (await browser.observe()).notices)


async def test_a_bot_check_page_is_handed_to_the_owner_not_worked_around(
    browser: BrowserSession, site: Site
) -> None:
    with pytest.raises(NeedsUserError):
        await open_page(browser, site, "/challenge")


async def test_a_challenge_shown_only_in_the_page_body_is_also_handed_to_the_owner(
    browser: BrowserSession, site: Site
) -> None:
    with pytest.raises(NeedsUserError):
        await open_page(browser, site, "/challenge-dialog")


async def test_a_footer_that_merely_mentions_captcha_is_not_a_block(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/footer-mentions-captcha")
    assert (await browser.observe()).title == "Chaos"


async def test_a_redirect_loop_fails_recoverably(browser: BrowserSession, site: Site) -> None:
    with pytest.raises(RecoverableError):
        await open_page(browser, site, "/loop")


async def test_a_blank_page_can_be_observed(browser: BrowserSession, site: Site) -> None:
    await open_page(browser, site, "/blank")
    assert (await browser.observe()).url.endswith("/blank")


async def test_back_returns_to_the_previous_page(browser: BrowserSession, site: Site) -> None:
    await open_page(browser, site, "/products")
    await open_page(browser, site, "/search")
    await browser.perform(Back(kind="back"), None)
    assert browser.page.url.endswith("/products")


async def test_a_background_request_the_site_refuses_is_reported_so_the_model_can_say_so(
    browser: BrowserSession, site: Site
) -> None:
    await open_page(browser, site, "/refused-prices")

    seen = await browser.observe()

    assert "the site refused a background request (403): /api/price" in seen.notices
    assert len(seen.notices) == 1  # the tracker's failure is not the site's refusal
    assert "unavailable" in seen.snapshot
