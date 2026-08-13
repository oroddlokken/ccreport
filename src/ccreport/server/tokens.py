"""Minting and checking the per-machine tokens.

The plaintext exists for exactly one response: the mint page shows it, and only
its hash is stored. A lost token is re-minted, never recovered — which is the
same guarantee that makes a database copy useless to whoever ends up with it.

SHA-256 rather than a password hash. The input is 32 bytes from
secrets.token_urlsafe, so there is no dictionary to run and no work factor
worth paying on a lookup that happens on every push.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh token, in plaintext. The only time it exists."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The stored form of *token*."""
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(header: str | None) -> str | None:
    """The token out of an Authorization header, or None if there is not one.

    Case-insensitive on the scheme, because that is what RFC 7235 says and
    what an intermediary is free to rewrite.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def mint(conn: sqlite3.Connection, machine_id: str, label: str, now: float) -> str:
    """Create *machine_id* if new, give it a token, and return the plaintext.

    The machine row comes first: machine_tokens references it, and the mint
    page is where a machine gets named at all — ingest only ever touches
    last_seen, so a laptop that was never minted has no way in.
    """
    from ccreport.server.db import upsert_machine

    upsert_machine(conn, machine_id, label, now)
    token = new_token()
    conn.execute(
        "INSERT INTO machine_tokens (token_hash, machine_id, created_at) VALUES (?, ?, ?)",
        (token_hash(token), machine_id, now),
    )
    return token


def connect_command(base_url: str, token: str) -> str:
    """The line to paste on the new machine.

    Rendered on the mint page because that is the one moment both halves are
    known: the server knows its own URL and has just generated the token, and
    the machine knows neither.
    """
    return f"ccreport server connect {base_url.rstrip('/')} --token {token}"
