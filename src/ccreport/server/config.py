"""Server configuration, read from the environment.

Four values, no config library and no file: the server runs as one process
under a supervisor that already has an environment to set. The client end of
the same link is the opposite — `ccreport server connect` writes a file,
because a token belongs in a mode-0600 path rather than in a process listing.

Read once per call rather than cached at import: a test sets one with
monkeypatch.setenv and expects the next create_app() to see it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DB_ENV = "CCREPORT_SERVER_DB"
HOST_ENV = "CCREPORT_SERVER_HOST"
PORT_ENV = "CCREPORT_SERVER_PORT"
NETWORKS_ENV = "CCREPORT_SERVER_NETWORKS"
MAX_BODY_ENV = "CCREPORT_SERVER_MAX_BODY"

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "ccreport" / "server.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
"""How large one push may be. A machine's first push is its whole history and
is the one that tests this; later pushes carry the files that changed."""

DEFAULT_NETWORKS = ("127.0.0.1/32", "::1/128")
"""Who reaches the web UI when CCREPORT_SERVER_NETWORKS is unset.

Loopback, so an unconfigured server behind a public interface serves its pages
to nobody rather than to everybody. Ingest is deliberately not behind this: a
machine pushes from wherever it happens to be, and its token is what admits it.
"""


@dataclass(frozen=True)
class ServerConfig:
    db_path: Path
    host: str
    port: int
    networks: tuple[str, ...]
    """CIDRs or bare addresses allowed to reach the web UI; see middleware.py."""
    max_body_bytes: int


def _int_env(name: str, default: int) -> int:
    """An integer environment variable, falling back on anything unparseable.

    A typo in a port number degrades to the default port rather than to a
    server that will not start, which is the same call every other numeric
    env var in this tree makes.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> ServerConfig:
    """The current environment as a ServerConfig."""
    raw_networks = os.environ.get(NETWORKS_ENV, "")
    networks = tuple(part for part in raw_networks.replace(",", " ").split() if part)
    return ServerConfig(
        db_path=Path(os.environ.get(DB_ENV) or DEFAULT_DB_PATH),
        host=os.environ.get(HOST_ENV) or DEFAULT_HOST,
        port=_int_env(PORT_ENV, DEFAULT_PORT),
        networks=networks or DEFAULT_NETWORKS,
        max_body_bytes=_int_env(MAX_BODY_ENV, DEFAULT_MAX_BODY_BYTES),
    )
