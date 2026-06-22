"""Letting the owner drive a task's browser by hand (logins, MFA, CAPTCHAs).

The agent is parked while this happens: the task is `waiting_user`, so its workflow takes no
steps. Nothing the owner types or clicks is logged; only the start and end are audited.
"""

import asyncio
import contextlib
import hashlib
import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from worker.browser.session import BrowserSession
from worker.db import Conn
from worker.transitions import InvalidTransitionError, transition

TOKEN_TTL = timedelta(seconds=60)
SCREENCAST_QUALITY = 60
BUTTONS = {"left": "left", "right": "right", "middle": "middle", "none": "none"}


class Browsers(Protocol):
    """What takeover needs from whatever runs the browsers."""

    def session_for(self, task_id: UUID) -> BrowserSession | None: ...

    async def checkpoint(self, task_id: UUID) -> None: ...


class NotAvailableError(Exception):
    """The task is not waiting on the owner, or is not theirs."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_token(conn: Conn, *, user_id: UUID, task_id: UUID) -> str:
    """A single-use token valid for a minute. Only the owner's waiting tasks qualify."""
    waiting = await (
        await conn.execute(
            "select 1 from public.tasks where id = %s and user_id = %s and status = 'waiting_user'",
            (task_id, user_id),
        )
    ).fetchone()
    if waiting is None:
        raise NotAvailableError(str(task_id))
    token = secrets.token_urlsafe(32)
    await conn.execute(
        "insert into public.takeover_tokens (token_hash, user_id, task_id, expires_at) "
        "values (%s, %s, %s, now() + %s)",
        (_digest(token), user_id, task_id, TOKEN_TTL),
    )
    return token


async def redeem_token(conn: Conn, token: str) -> tuple[UUID, UUID] | None:
    """Spends the token. Returns (user_id, task_id), or None if unknown, used or expired."""
    row = await (
        await conn.execute(
            "update public.takeover_tokens set used_at = now() "
            "where token_hash = %s and used_at is null and expires_at > now() "
            "returning user_id, task_id",
            (_digest(token),),
        )
    ).fetchone()
    return (row["user_id"], row["task_id"]) if row else None


# -- messages from the dashboard ------------------------------------------------------------


class Mouse(BaseModel):
    type: Literal["mouse"]
    action: Literal["press", "release", "move", "wheel"]
    x: float
    y: float
    button: Literal["left", "right", "middle", "none"] = "none"
    delta_x: float = 0
    delta_y: float = 0


class Text(BaseModel):
    type: Literal["text"]
    text: Annotated[str, Field(max_length=500)]


class Key(BaseModel):
    type: Literal["key"]
    action: Literal["down", "up"]
    key: Annotated[str, Field(max_length=32)]
    code: Annotated[str, Field(max_length=32)] = ""


class Done(BaseModel):
    type: Literal["done"]
    anyway: bool = False  # hand the task back even though the page still looks unfinished


Message = Annotated[Mouse | Text | Key | Done, Field(discriminator="type")]
_MESSAGE = TypeAdapter(Message)


async def run(websocket: WebSocket, session: BrowserSession) -> bool:
    """Streams the browser to the owner and applies their input. True if they finished (Done),
    False if they just disconnected. Done is refused, once, while the page still needs them."""
    cdp = await session.page.context.new_cdp_session(session.page)
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)

    def on_frame(frame: dict[str, Any]) -> None:
        if frames.full():  # the viewer is slower than the page: drop the oldest picture
            frames.get_nowait()
        frames.put_nowait(frame)

    cdp.on("Page.screencastFrame", on_frame)
    await cdp.send(
        "Page.startScreencast",
        {"format": "jpeg", "quality": SCREENCAST_QUALITY, "maxWidth": 1280, "maxHeight": 800},
    )
    sender = asyncio.create_task(_send_frames(websocket, cdp, frames))
    try:
        while True:
            try:
                message = _MESSAGE.validate_python(await websocket.receive_json())
            except ValidationError:
                continue  # ignore anything that is not a known message
            if isinstance(message, Done):
                if not message.anyway and (reason := await session.needs_owner()):
                    # A half-finished login would send the agent straight back into a loop.
                    await websocket.send_json({"type": "not_done", "reason": reason})
                    continue
                return True
            await _apply(cdp, message)
    except WebSocketDisconnect:
        return False
    finally:
        sender.cancel()
        with contextlib.suppress(PlaywrightError):  # the page may already be gone
            await cdp.send("Page.stopScreencast")
            await cdp.detach()


async def _send_frames(
    websocket: WebSocket, cdp: CDPSession, frames: asyncio.Queue[dict[str, Any]]
) -> None:
    while True:
        frame = await frames.get()
        await cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})
        meta = frame["metadata"]
        await websocket.send_json(
            {
                "type": "frame",
                "data": frame["data"],
                "width": meta["deviceWidth"],
                "height": meta["deviceHeight"],
            }
        )


async def _apply(cdp: CDPSession, message: Mouse | Text | Key) -> None:
    match message:
        case Mouse(action="wheel"):
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": message.x,
                    "y": message.y,
                    "deltaX": message.delta_x,
                    "deltaY": message.delta_y,
                },
            )
        case Mouse():
            kind = {"press": "mousePressed", "release": "mouseReleased", "move": "mouseMoved"}
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": kind[message.action],
                    "x": message.x,
                    "y": message.y,
                    "button": BUTTONS[message.button],
                    "clickCount": 1 if message.action != "move" else 0,
                },
            )
        case Text():
            await cdp.send("Input.insertText", {"text": message.text})
        case Key():
            await cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown" if message.action == "down" else "keyUp",
                    "key": message.key,
                    "code": message.code,
                },
            )


async def finish(conn: Conn, task_id: UUID, user_id: UUID) -> None:
    """Hands the task back to the agent."""
    async with conn.transaction():
        await conn.execute(
            "insert into audit.events (user_id, actor, action, target_type, target_id) "
            "values (%s, 'user', 'takeover_ended', 'task', %s)",
            (user_id, str(task_id)),
        )
        # If it was cancelled or paused during the takeover, leave it as it is.
        with contextlib.suppress(InvalidTransitionError):
            await transition(
                conn,
                task_id,
                "running",
                event_type="takeover_done",
                payload={"summary": "the owner finished by hand; carrying on from the page"},
            )
