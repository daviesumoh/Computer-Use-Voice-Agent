"""The only things the model can ask the browser to do. It never writes selectors or scripts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Navigate(BaseModel):
    kind: Literal["navigate"]
    url: str


class Click(BaseModel):
    kind: Literal["click"]
    ref: str


class Type(BaseModel):
    kind: Literal["type"]
    ref: str
    text: str


class Select(BaseModel):
    kind: Literal["select"]
    ref: str
    option: str


class Press(BaseModel):
    kind: Literal["press"]
    key: Literal["Enter", "Escape", "Tab", "ArrowDown", "ArrowUp", "PageDown", "PageUp"]


class Scroll(BaseModel):
    kind: Literal["scroll"]
    direction: Literal["down", "up"]


class Back(BaseModel):
    kind: Literal["back"]


class Wait(BaseModel):
    kind: Literal["wait"]
    seconds: Annotated[float, Field(ge=0.5, le=10)]


class FillSecret(BaseModel):
    """Types a stored credential into a field. The model names the account, never the value."""

    kind: Literal["fill_secret"]
    ref: str
    account_id: UUID
    field: Literal["username", "password"]


class Extract(BaseModel):
    """Reads values off the current page through a separate, tool-less model call."""

    kind: Literal["extract"]
    question: str


class Look(BaseModel):
    """Photographs the page and has a separate, tool-less model answer one question about it.
    For what text cannot carry (a chart, a canvas, a photo); "fine" is for small print."""

    kind: Literal["look"]
    question: str
    detail: Literal["coarse", "fine"]


class Done(BaseModel):
    kind: Literal["done"]
    result: str


# A plain union, not a discriminated one: OpenAI's strict schemas accept anyOf but reject the
# oneOf that a discriminator produces. Each member's literal `kind` still picks exactly one.
Action = (
    Navigate
    | Click
    | Type
    | Select
    | FillSecret
    | Press
    | Scroll
    | Back
    | Wait
    | Extract
    | Look
    | Done
)


def fingerprint(action: BaseModel) -> str:
    """Identifies repeats of the same action for loop detection."""
    return action.model_dump_json()
