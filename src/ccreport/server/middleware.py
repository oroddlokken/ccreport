"""Access control for the web UI, as a FastAPI dependency and as an ASGI wrapper.

A dependency rather than middleware, because it applies to the pages and not
to ingest: a machine pushes from a hotel network and its token is what admits
it, while the machines-and-tokens pages are only ever opened from home.

A mount takes no dependencies, so the static files carry the same check one
layer down. Both read the same allowlist through `ip_allowed`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_TEST_HOSTS = frozenset({"testclient", "localhost"})
"""Hostnames Starlette's TestClient and a local browser present instead of an
address. Read as loopback so a test does not have to allow a name that is not
one."""


def ip_allowed(remote_addr: str, networks: Iterable[str]) -> bool:
    """Whether *remote_addr* falls in any of *networks*.

    An entry is a CIDR or a bare address; anything that parses as neither is
    skipped, so one typo in the environment variable costs that one entry
    rather than the whole allowlist.
    """
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    for network in networks:
        try:
            if ip in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            try:
                if ip == ipaddress.ip_address(network):
                    return True
            except ValueError:
                continue
    return False


def _admitted(remote_addr: str | None, networks: tuple[str, ...]) -> bool:
    """Whether a caller at *remote_addr* is inside the allowlist."""
    if remote_addr in _TEST_HOSTS:
        remote_addr = "127.0.0.1"
    return bool(remote_addr) and ip_allowed(remote_addr, networks)


def restrict_remote_addr_dep(networks: Iterable[str]) -> Callable[[Request], Awaitable[None]]:
    """A dependency that answers 403 to anything outside *networks*."""
    allowed = tuple(networks)

    async def dependency(request: Request) -> None:
        client = request.client
        if not _admitted(client.host if client else None, allowed):
            raise HTTPException(status_code=403, detail="Access denied")

    return dependency


class ImmutableCached:
    """Adds Cache-Control to static responses whose URL carries an mtime stamp.

    pages._asset stamps every asset URL with the file's mtime, so what one URL
    names never changes content — immutable is exact, and saves the browser a
    conditional GET per asset per navigation. An unstamped URL (the stat-failed
    fallback) and any non-200 pass through unmarked, so nothing stale or missing
    is ever pinned.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or b"mtime=" not in scope.get("query_string", b""):
            await self.app(scope, receive, send)
            return

        async def stamped(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                MutableHeaders(scope=message)["Cache-Control"] = "max-age=31536000, immutable"
            await send(message)

        await self.app(scope, receive, stamped)


class NetworkGated:
    """An ASGI app that answers 403 outside *networks* and delegates inside it.

    For the static mount, which `app.mount` gives no dependencies to. Without
    it the stylesheet is the one thing on this server that answers from
    anywhere, while every page it styles answers 403.

    A non-HTTP scope passes straight through: there is no client address to
    read on a lifespan message, and refusing one would take the app down at
    startup.
    """

    def __init__(self, app: ASGIApp, networks: Iterable[str]) -> None:
        self.app = app
        self.networks = tuple(networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if not _admitted(client[0] if client else None, self.networks):
            await PlainTextResponse("Access denied", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
