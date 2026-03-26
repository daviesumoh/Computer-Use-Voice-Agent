"""Reads Playwright's AI-mode accessibility snapshot, in which every node carries a ref."""

import re
from dataclasses import dataclass

# - button "Place order" [disabled] [ref=e12] [cursor=pointer]:
_LINE = re.compile(r'^\s*- (?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r"\[ref=(?P<ref>[\w]+)\]")

INTERACTIVE_ROLES = frozenset(
    {
        "link", "button", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
        "option", "tab", "menuitem", "slider", "spinbutton",
    }
)  # fmt: skip
MAX_SNAPSHOT_CHARS = 24_000


@dataclass(frozen=True)
class Node:
    ref: str
    role: str
    name: str
    disabled: bool = False


def parse(snapshot: str) -> dict[str, Node]:
    """Every node that has a ref, by ref."""
    nodes: dict[str, Node] = {}
    for line in snapshot.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        ref = _REF.search(match["rest"])
        if ref is None:
            continue
        name = (match["name"] or "").replace('\\"', '"')
        nodes[ref["ref"]] = Node(
            ref["ref"], match["role"], name, disabled="[disabled]" in match["rest"]
        )
    return nodes


def find_same(nodes: dict[str, Node], wanted: Node) -> list[Node]:
    """Nodes with the wanted role and name: how a ref is re-found after the page re-rendered."""
    return [n for n in nodes.values() if n.role == wanted.role and n.name == wanted.name]


_TEXT_ONLY = re.compile(
    r"^(?P<head> *- (?:generic|text|paragraph|strong|emphasis)(?: \[[^\]]*\])*:)(?: (?P<text>.+))?$"
)
MAX_TEXT_CHARS = 200  # a paragraph of site boilerplate says nothing the first lines don't
MAX_NAME_CHARS = 300  # product links spell out price and stock in their names; keep those
_URL_LINE = re.compile(r"^(?P<head> *- /url: )(?P<url>\S+)$")


def compact(snapshot: str) -> str:
    """The same page in fewer tokens, for the model (refs are untouched).

    Drops what repeats or says nothing: text that a link or button's own name already
    contains (a product card spells its price in the link's name and again in nested
    elements), wrapper elements with no text, cursor hints, and tracking query strings.
    """
    lines: list[str] = []
    named: list[tuple[int, str]] = []  # (indent, name) of the open ancestors that have a name
    for raw in snapshot.splitlines():
        line = raw.replace(" [cursor=pointer]", "")
        indent = len(line) - len(line.lstrip(" "))
        while named and named[-1][0] >= indent:
            named.pop()

        if match := _URL_LINE.match(line):
            line = match["head"] + match["url"].split("?", 1)[0]
        elif match := _TEXT_ONLY.match(line):
            text = (match["text"] or "").strip().strip("'\"")
            if not text or any(text.lower() in name for _, name in named):
                continue  # an empty wrapper, or already said by an ancestor's name
            if len(text) > MAX_TEXT_CHARS:
                line = f"{match['head']} {text[:MAX_TEXT_CHARS]}…"
        elif (head := _LINE.match(line)) and head["name"]:
            named.append((indent, head["name"].lower()))
            if len(head["name"]) > MAX_NAME_CHARS:
                line = line.replace(head["name"], head["name"][:MAX_NAME_CHARS] + "…", 1)
        lines.append(line)
    return "\n".join(lines)


def truncate(snapshot: str) -> str:
    if len(snapshot) <= MAX_SNAPSHOT_CHARS:
        return snapshot
    return snapshot[:MAX_SNAPSHOT_CHARS] + "\n[snapshot truncated; scroll or extract to see more]"
