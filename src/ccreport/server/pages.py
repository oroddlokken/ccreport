"""The web UI: machines, minting, revoking and account names.

Server-rendered HTML with no client framework and no build step. Every route
here sits behind the network allowlist; ingest does not, because a machine
pushes from wherever it is.

Anyone on an allowed network can mint. There is no password.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ccreport.server import dashboard, db, tokens

router = APIRouter(tags=["pages"])

TEMPLATE_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _when(ts: float | None) -> str:
    """An epoch as a readable local stamp, or a dash where there is nothing."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["when"] = _when


@router.get("/", response_class=HTMLResponse)
def index(request: Request, days: int = Query(default=dashboard.DEFAULT_RANGE)):
    """The merged spend dashboard."""
    view = dashboard.cached_build(request.app.state.db, days)
    return templates.TemplateResponse(request, "dashboard.html", {
        "view": view,
        "ranges": dashboard.RANGES,
        "range_labels": dashboard.RANGE_LABELS,
        "dimensions": dashboard.DIMENSIONS,
        "chart": json.dumps({
            "days": view.chart_days,
            "series": [
                {"account": s.account, "cost": s.cost, "tokens": s.tokens}
                for s in view.series
            ],
        }),
    })


@router.get("/machines", response_class=HTMLResponse)
def machines(request: Request, deleted: str = Query(default=""),
             records: int = Query(default=0)):
    """Every machine, with its last push, record count and token state.

    *deleted* and *records* say what the last delete took. They ride in the
    query string because a redirect carries nothing else and this server has no
    session to flash a message through.
    """
    conn = request.app.state.db.connect()
    note = f"Deleted {deleted} and its {records:,} records." if deleted else ""
    return templates.TemplateResponse(
        request, "machines.html", {"machines": db.machine_overview(conn), "note": note},
    )


def _machine_page(request: Request, machine_id: str, error: str = "", status_code: int = 200):
    """One machine's page, or a 404 where nothing was minted for that id.

    An id nothing was minted for is a 404. Rendering it would draw a machine
    that does not exist, with a zero record count and no token, which reads as
    a real machine that has never pushed.
    """
    conn = request.app.state.db.connect()
    label = db.machine_label(conn, machine_id)
    if label is None:
        raise HTTPException(status_code=404, detail=f"No machine {machine_id}.")
    return templates.TemplateResponse(request, "machine.html", {
        "machine_id": machine_id,
        "label": label or machine_id,
        "records": db.record_count(conn, machine_id),
        "tokens": db.machine_tokens(conn, machine_id),
        "error": error,
    }, status_code=status_code)


@router.get("/machines/{machine_id}", response_class=HTMLResponse)
def machine(request: Request, machine_id: str):
    """One machine's tokens, each with a revoke and a delete."""
    return _machine_page(request, machine_id)


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    """Every account that has pushed, each row a field for the name to draw it under."""
    conn = request.app.state.db.connect()
    return templates.TemplateResponse(
        request, "accounts.html", {"accounts": db.account_overview(conn)},
    )


@router.post("/accounts/{account_uuid}/alias")
def set_alias(request: Request, account_uuid: str, alias: str = Form("")):
    """Name one account, or clear the name back to the label it pushed under.

    The stored records are untouched: what changes is the string every server
    view resolves through, and the dashboard picks it up because
    db.content_stamp reads account_aliases.
    """
    conn = request.app.state.db.connect()
    db.set_account_alias(conn, account_uuid, alias, time.time())
    conn.commit()
    return RedirectResponse(url="/accounts", status_code=303)


def _bad_cidr(networks: str) -> str | None:
    """The first CIDR that will not parse, or None.

    Checked here rather than left to the machine: a typo blocks every push from
    it and the server has nothing to notice the silence with.
    """
    for item in tokens.csv_list(networks).split(","):
        if not item:
            continue
        try:
            ip_network(item, strict=False)
        except ValueError:
            return item
    return None


@router.post("/machines/mint", response_class=HTMLResponse)
def mint(request: Request, machine_id: str = Form(...), label: str = Form(""),
         networks: str = Form(""), restricted: str = Form(""), allow: str = Form("")):
    """Mint a token and show it once, with the command that consumes it.

    The push policy is written into that command and stored nowhere: it lives
    in the machine's own push.toml, and this is only where it gets typed.
    """
    conn = request.app.state.db.connect()
    bad = _bad_cidr(networks)
    if bad:
        return templates.TemplateResponse(
            request, "machines.html",
            {"machines": db.machine_overview(conn), "error": f"{bad} is not a network."},
            status_code=400,
        )
    token = tokens.mint(conn, machine_id.strip(), label.strip() or machine_id.strip(), time.time())
    conn.commit()
    return templates.TemplateResponse(request, "minted.html", {
        "machine_id": machine_id.strip(),
        "token": token,
        "command": tokens.connect_command(
            str(request.base_url), token,
            networks=networks, restricted=bool(restricted), allow=allow,
        ),
    })


@router.post("/tokens/{token_hash}/revoke")
def revoke(request: Request, token_hash: str):
    """Revoke one token. It stops working on that machine's next push."""
    conn = request.app.state.db.connect()
    db.revoke_token(conn, token_hash, time.time())
    conn.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/", status_code=303)


@router.post("/tokens/{token_hash}/delete")
def delete_token(request: Request, token_hash: str):
    """Remove one token's row. It stops working the same way a revoke does.

    What it does not leave behind is the row: a token minted into the wrong
    machine has no history the table is better for showing.
    """
    conn = request.app.state.db.connect()
    db.delete_token(conn, token_hash)
    conn.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/", status_code=303)


@router.post("/machines/{machine_id}/delete")
def delete_machine(request: Request, machine_id: str, confirm: str = Form("")):
    """Remove a machine, its tokens, its ingest state and every record it pushed.

    The typed id is the whole guard: a machine that has pushed holds money
    nothing else on this server has a copy of. A mismatch re-renders the page
    and deletes nothing.
    """
    conn = request.app.state.db.connect()
    if confirm.strip() != machine_id:
        return _machine_page(
            request, machine_id,
            error=f"Type {machine_id} to delete it. Nothing was deleted.",
            status_code=400,
        )
    if db.machine_label(conn, machine_id) is None:
        raise HTTPException(status_code=404, detail=f"No machine {machine_id}.")
    destroyed = db.delete_machine(conn, machine_id)
    conn.commit()
    return RedirectResponse(
        url=f"/machines?deleted={quote(machine_id)}&records={destroyed:d}", status_code=303,
    )
