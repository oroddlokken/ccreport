"""Access control for the web UI, as a FastAPI dependency.

A dependency rather than middleware, because it applies to the pages and not
to ingest: a machine pushes from a hotel network and its token is what admits
it, while the machines-and-tokens pages are only ever opened from home.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Iterable

from fastapi import HTTPException, Request

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


def restrict_remote_addr_dep(networks: Iterable[str]) -> Callable[[Request], Awaitable[None]]:
    """A dependency that answers 403 to anything outside *networks*."""
    allowed = tuple(networks)

    async def dependency(request: Request) -> None:
        client = request.client
        remote_addr = client.host if client else None
        if remote_addr in _TEST_HOSTS:
            remote_addr = "127.0.0.1"
        if not remote_addr or not ip_allowed(remote_addr, allowed):
            raise HTTPException(status_code=403, detail="Access denied")

    return dependency
