"""Shared setup for the server tests: an app, a client, and a batch to push."""

from __future__ import annotations

import time

from ccreport.server import db, tokens
from ccreport.server.config import ServerConfig

LOOPBACK = ("127.0.0.1/32", "::1/128")
"""What TestClient's address is read as; see middleware._TEST_HOSTS."""

ELSEWHERE = ("10.0.0.0/8",)
"""An allowlist that admits nobody in these tests, for the 403 cases."""


def config(tmp_path, *, networks=LOOPBACK, max_body_bytes=1_000_000) -> ServerConfig:
    return ServerConfig(
        db_path=tmp_path / "server.db",
        host="127.0.0.1",
        port=8787,
        networks=networks,
        max_body_bytes=max_body_bytes,
    )


def mint_for(app, machine_id: str = "laptop-1", label: str = "Laptop") -> str:
    """A live token for *machine_id*, minted straight through the database.

    The mint page does the same thing; a test of ingest should not have to go
    through the UI to get in.
    """
    conn = app.state.db.connect()
    token = tokens.mint(conn, machine_id, label, time.time())
    conn.commit()
    return token


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def record(**over) -> dict:
    rec = {
        "mid": "msg_1",
        "model": "claude-sonnet-4-5-20250929",
        "ts": 1_770_000_000.0,
        "sid": "sess-1",
        "project": "ccr-projA",
        "cwd": "/tmp/ccr-projA",
        "repo": "github.com/o/p",
        "dk": "msg_1:req_1",
        "cost": None,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_create": 5000,
        "cache_read": 30000,
        "account_uuid": "acct-1",
        "account_label": "me@example.net",
    }
    rec.update(over)
    return rec


def batch(records=None, *, path="/p/a.jsonl", mtime_ns=1, size=100, label="Laptop") -> dict:
    return {
        "label": label,
        "client_version": "0.1.0",
        "files": [{
            "path": path,
            "mtime_ns": mtime_ns,
            "size": size,
            "records": [record()] if records is None else records,
        }],
    }


def stored(app, machine_id: str, path: str = "/p/a.jsonl") -> list[dict]:
    return db.load_file_records(app.state.db.connect(), machine_id, path)
