"""One Chromium context for one task: launch, observe, act, verify."""

import logging
import re
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from worker.browser import snapshot
from worker.browser.actions import (
    Action,
    Back,
    Click,
    Navigate,
    Press,
    Scroll,
    Select,
    Type,
    Wait,
)
from worker.browser.errors import (
    ActionNotVerifiedError,
    ElementAmbiguousError,
    ElementNotFoundError,
    IrrecoverableError,
    NeedsUserError,
    PictureRefusedError,
    RecoverableError,
    SecretLeakError,
)
from worker.browser.guard import DestinationGuard
from worker.browser.snapshot import Node

log = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 60_000  # the free worker loads heavy pages in 10-18 s
ACTION_TIMEOUT_MS = 15_000
BLOCKED_RESOURCES = frozenset({"font", "media"})  # saves memory on the 1.5 GB worker
BLOCK_STATUSES = frozenset({403, 429, 456})
_CHALLENGE_TEXT = re.compile(
    r"verify you are (a )?human|unusual traffic|are you a robot|robot or human|captcha|"
    r"access denied|press (&|and) hold",
    re.IGNORECASE,
)
_BODY_CHALLENGE = re.compile(
    r"robot or human|verify you are (a )?human|unusual traffic|press (&|and) hold",
    re.IGNORECASE,
)


@dataclass
class Observation:
    url: str
    title: str
    snapshot: str
    tabs: int
    notices: list[str] = field(default_factory=list)  # things that happened since the last look
    visuals: str = ""  # what the page shows that the text view cannot, e.g. "1 canvas"
    sparse: bool = False  # the text view is nearly empty


MAX_TEXT_POLLS = 16  # with the interval below: at most 8 seconds of waiting for late content
QUIET_POLLS = 3  # the text must hold still this many polls in a row (1.5 s) to count as drawn
TEXT_POLL_MS = 500
SPARSE_TEXT_CHARS = 250  # a text view shorter than this says little about the page
PICTURE_QUALITY = {"coarse": 55, "fine": 80}  # JPEG quality; fine is for small print

# Counts the large things a text view skips. Small icons and logos are not worth a picture.
_VISUALS_SCRIPT = """() => {
  const shown = (el, w, h) => {
    const r = el.getBoundingClientRect();
    return r.width >= w && r.height >= h && r.bottom > 0 && r.right > 0;
  };
  const count = (sel, w, h, keep = () => true) =>
    [...document.querySelectorAll(sel)].filter((el) => shown(el, w, h) && keep(el)).length;
  return {
    canvas: count('canvas', 200, 120),
    graphics: count('svg', 250, 150),
    images: count('img', 300, 200, (el) => !(el.alt || '').trim()),
  };
}"""


class BrowserSession:
    def __init__(
        self,
        *,
        headless: bool,
        allow_private_hosts: bool = False,
        action_timeout_ms: int = ACTION_TIMEOUT_MS,
        storage_state: dict[str, Any] | None = None,
    ) -> None:
        self._storage_state = storage_state
        self._headless = headless
        self._action_timeout_ms = action_timeout_ms
        self._guard = DestinationGuard(allow_private=allow_private_hosts)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._notices: list[str] = []
        self.last_typed: Node | None = None
        self._secrets: set[str] = set()  # values typed from the secret store; never shown on

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chromium",
            headless=self._headless,
            args=["--disable-dev-shm-usage"],  # /dev/shm is tiny in containers
        )
        self._context = await self._browser.new_context(
            accept_downloads=False,
            storage_state=self._storage_state,  # type: ignore[arg-type]  # a plain dict is accepted
        )
        await self._context.route("**/*", self._screen_request)
        self._context.on("page", self._adopt_page)
        await self._context.new_page()  # the context's "page" event adopts it
        assert self._page is not None
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Close in order and tolerate a browser that already died.
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    log.info("browser already closed")
        if self._playwright is not None:
            await self._playwright.stop()

    # -- page and request handling ------------------------------------------------

    def _adopt_page(self, page: Page) -> None:
        """A new tab (popup, target=_blank) becomes the page we work on."""
        if self._page is not None and page is not self._page:
            self._notices.append(f"a new tab opened: {page.url or 'about:blank'}")
        self._page = page
        page.on("dialog", self._dismiss_dialog)
        page.on("response", self._note_refusal)
        page.set_default_timeout(self._action_timeout_ms)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    def _note_refusal(self, response: Response) -> None:
        """Tells the model when the site itself refused a background request (a price or stock
        service answering 403), so it can say that prices are missing instead of searching
        for them. Nothing here retries or works around the refusal."""
        request = response.request
        if request.resource_type not in {"fetch", "xhr"} or response.status not in {401, 403, 429}:
            return
        page_host = urlsplit(self.page.url).hostname or ""
        target = urlsplit(request.url)
        if not (target.hostname and page_host.removeprefix("www.") in target.hostname):
            return  # a third-party tracker's refusal says nothing about the page
        note = f"the site refused a background request ({response.status}): {target.path[-60:]}"
        if note not in self._notices:
            self._notices.append(note)

    async def _dismiss_dialog(self, dialog: Dialog) -> None:
        self._notices.append(f"a {dialog.type} dialog was dismissed: {dialog.message[:120]}")
        await dialog.dismiss()

    async def _screen_request(self, route: Route) -> None:
        request: Request = route.request
        if request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
            return
        try:
            await self._guard.check(request.url)
        except IrrecoverableError as blocked:
            log.warning("blocked request", extra={"url": request.url, "reason": str(blocked)})
            self._notices.append(f"blocked a request to an internal address: {request.url[:80]}")
            await route.abort()
            return
        await route.continue_()

    async def submits_post_form(self, node: Node) -> bool:
        """Whether activating this element would send a form with method POST."""
        try:
            return bool(
                await self.page.locator(f"aria-ref={node.ref}").evaluate(
                    "el => { const f = el.form ?? el.closest('form'); "
                    "return !!f && f.method.toLowerCase() === 'post' "
                    "&& (el.type === 'submit' || el.tagName === 'BUTTON' && !el.type); }"
                )
            )
        except PlaywrightError:
            return False  # the element is gone; resolve() will report that

    async def export_state(self) -> dict[str, Any]:
        assert self._context is not None
        return dict(await self._context.storage_state())

    def remember_secret(self, value: str) -> None:
        """Registers a value typed from the secret store so it is masked wherever it could echo."""
        self._secrets.add(value)

    def scrub(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "•••")
        return text

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise RecoverableError("the page was closed")
        return self._page

    # -- observing ------------------------------------------------------------------

    async def observe(self) -> Observation:
        page = self.page
        await self._settle(page)
        try:
            text = await page.aria_snapshot(mode="ai")
            title = await page.title()
        except PlaywrightError as failure:  # includes timeouts: the page is busy or gone
            raise RecoverableError(
                f"could not read the page: {str(failure).splitlines()[0]}"
            ) from failure
        notices, self._notices = self._notices, []
        assert self._context is not None
        compacted = snapshot.compact(self.scrub(text))
        return Observation(
            url=page.url,
            title=self.scrub(title),
            snapshot=snapshot.truncate(compacted),
            tabs=len(self._context.pages),
            notices=notices,
            visuals=await self._visuals(page),
            sparse=len(compacted) < SPARSE_TEXT_CHARS,
        )

    @staticmethod
    async def _visuals(page: Page) -> str:
        try:
            found = await page.evaluate(_VISUALS_SCRIPT)
        except PlaywrightError:
            return ""  # navigated away; the next observation looks again
        labels = {
            "canvas": "drawn canvas",
            "graphics": "large graphic or chart",
            "images": "large image with no description",
        }
        return ", ".join(
            f"{found[key]} {label}{'s' if found[key] > 1 else ''}"
            for key, label in labels.items()
            if found.get(key)
        )

    async def screenshot(self, detail: str) -> bytes:
        """A JPEG of what is on screen now. Refused where it could capture a login form or a
        value typed from the secret store; passwords are masked in any case."""
        page = self.page
        try:
            if await page.locator("input[type=password]:visible").count():
                raise PictureRefusedError("the page shows a sign-in form")
            if self._secrets:
                visible = await page.evaluate("document.body ? document.body.innerText : ''")
                if self.scrub(visible) != visible:
                    raise PictureRefusedError("the page shows a value from the secret store")
            return await page.screenshot(
                type="jpeg",
                quality=PICTURE_QUALITY[detail],
                mask=[page.locator("input[type=password]")],
            )
        except PlaywrightError as failure:
            raise RecoverableError(
                f"could not take a picture: {str(failure).splitlines()[0]}"
            ) from failure

    async def nodes(self) -> dict[str, Node]:
        return snapshot.parse(await self.page.aria_snapshot(mode="ai"))

    @staticmethod
    async def _settle(page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeout:
            pass  # pages with live connections never go idle; the DOM is what we need
        # Single-page shops draw prices and stock after the network goes quiet (canadiantire.ca
        # showed none at first), so wait until the visible text stops growing.
        previous, quiet = -1, 0
        for _ in range(MAX_TEXT_POLLS):
            try:
                size = await page.evaluate("document.body ? document.body.innerText.length : 0")
            except PlaywrightError:
                return  # navigated away while polling; the next observation settles again
            quiet = quiet + 1 if size == previous else 0
            if quiet >= QUIET_POLLS:
                return
            previous = size
            await page.wait_for_timeout(TEXT_POLL_MS)

    # -- acting ---------------------------------------------------------------------

    async def resolve(self, ref: str, known: Node | None) -> Node:
        """Pre-flight: the ref must still point at the element the model chose.

        If the page re-rendered and the ref moved, the same role and name is accepted only
        when it is unique; anything else is reported rather than guessed.
        """
        nodes = await self.nodes()
        current = nodes.get(ref)
        if current is not None and (
            known is None or (current.role, current.name) == (known.role, known.name)
        ):
            return current
        if known is None:
            raise ElementNotFoundError(f"no element {ref} on the page")
        matches = snapshot.find_same(nodes, known)
        if not matches:
            raise ElementNotFoundError(f"{known.role} '{known.name}' is no longer on the page")
        if len(matches) > 1:
            raise ElementAmbiguousError(
                f"{len(matches)} elements match {known.role} '{known.name}': "
                + ", ".join(m.ref for m in matches)
            )
        return matches[0]

    async def perform(self, action: Action, node: Node | None) -> str:
        """Runs one action and checks its effect. Returns a short description for the history."""
        page = self.page
        before = page.url
        try:
            return await self._perform(page, action, node, before)
        except PlaywrightTimeout as timeout:
            raise RecoverableError(
                f"{action.kind} timed out: {str(timeout).splitlines()[0]}"
            ) from timeout
        except PlaywrightError as failure:
            if "closed" in str(failure).lower():
                raise RecoverableError("the browser page closed during the action") from failure
            raise RecoverableError(
                f"{action.kind} failed: {str(failure).splitlines()[0]}"
            ) from failure

    async def _perform(self, page: Page, action: Action, node: Node | None, before: str) -> str:
        match action:
            case Navigate(url=url):
                await self._guard.check(url)
                if any(secret in url for secret in self._secrets):
                    raise SecretLeakError("the URL contains a secret value")
                response = await page.goto(url, wait_until="domcontentloaded")
                await self._reject_blocked(page, response.status if response else None)
                if page.url != before or response:
                    await self._guard.check(page.url)
                return f"opened {page.url}"
            case Click():
                assert node is not None
                await page.locator(f"aria-ref={node.ref}").click()
                await self._settle(page)
                await self._reject_blocked(page, None)
                return f"clicked {node.role} '{node.name}'" + (
                    f" -> {page.url}" if page.url != before else ""
                )
            case Type(text=text):
                assert node is not None
                field = page.locator(f"aria-ref={node.ref}")
                await field.fill(text)
                if await field.input_value() != text:
                    raise ActionNotVerifiedError(f"'{node.name}' did not keep the typed text")
                self.last_typed = node
                return f"typed into '{node.name}'"
            case Select(option=option):
                assert node is not None
                await page.locator(f"aria-ref={node.ref}").select_option(label=option)
                return f"selected '{option}' in '{node.name}'"
            case Press(key=key):
                await page.keyboard.press(key)
                await self._settle(page)
                await self._reject_blocked(page, None)
                return f"pressed {key}"
            case Scroll(direction=direction):
                await page.mouse.wheel(0, 900 if direction == "down" else -900)
                await page.wait_for_timeout(400)
                return f"scrolled {direction}"
            case Back():
                await page.go_back(wait_until="domcontentloaded")
                return f"went back to {page.url}"
            case Wait(seconds=seconds):
                await page.wait_for_timeout(int(seconds * 1000))
                return f"waited {seconds:g}s"
            case _:
                raise AssertionError(f"{action.kind} is not a browser action")

    async def _reject_blocked(self, page: Page, status: int | None) -> None:
        """A bot check or block page needs the owner; do not try to get around it."""
        if status in BLOCK_STATUSES:
            raise NeedsUserError(f"{page.url} refused the request (HTTP {status})")
        title = await page.title()
        # Challenges also appear as an in-page dialog under a normal title (walmart.ca), so the
        # top of the page text is checked too; the phrases are specific enough to avoid footers.
        head = (await page.inner_text("body"))[:1500] if page.url.startswith("http") else ""
        if _CHALLENGE_TEXT.search(title) or _BODY_CHALLENGE.search(head):
            raise NeedsUserError(f"{page.url} is showing a human-verification page")

    async def needs_owner(self) -> str | None:
        """Why the page still needs the owner (a verification page, a sign-in form), or None.
        Used when the owner says they are done, so a half-finished login is caught then."""
        try:
            await self._reject_blocked(self.page, None)
            if await self.page.locator("input[type=password]:visible").count():
                return "the page still shows a password field"
        except NeedsUserError as blocked:
            return str(blocked)
        except PlaywrightError as failure:
            return f"the page could not be read: {str(failure).splitlines()[0]}"
        return None

    async def page_text(self) -> str:
        """The page as text for the tool-less reader. Built from the accessibility tree rather
        than `innerText`, which skips shadow DOM and iframes and says nothing of a checkbox
        being ticked or a menu option being selected."""
        text = await self.page.aria_snapshot(mode="ai")
        return self.scrub(snapshot.compact(text))
