"""Keeps the browser away from the worker's own network: loopback, private and link-local hosts."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from worker.browser.errors import UnsafeDestinationError


class DestinationGuard:
    def __init__(self, *, allow_private: bool = False) -> None:
        self._allow_private = allow_private
        self._verdicts: dict[str, bool] = {}  # host -> is public

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise UnsafeDestinationError(f"only http(s) pages may be opened, not {parts.scheme!r}")
        host = parts.hostname
        if not host:
            raise UnsafeDestinationError("the URL has no host")
        if self._allow_private:
            return
        if host not in self._verdicts:
            self._verdicts[host] = await asyncio.to_thread(_is_public, host)
        if not self._verdicts[host]:
            raise UnsafeDestinationError(f"{host} resolves to a private or internal address")


def _is_public(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False  # unresolvable: nothing to protect, and nothing to load either
    return all(ipaddress.ip_address(str(a).split("%")[0]).is_global for a in addresses)
