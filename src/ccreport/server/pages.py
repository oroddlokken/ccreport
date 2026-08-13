"""The web UI: machines, minting and revoking.

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
def machines(request: Request):
    """Every machine, with its last push, record count and token state."""
    conn = request.app.state.db.connect()
    return templates.TemplateResponse(
        request, "machines.html", {"machines": db.machine_overview(conn)},
    )


@router.get("/machines/{machine_id}", response_class=HTMLResponse)
def machine(request: Request, machine_id: str):
    """One machine's tokens, each with a revoke button.

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
    })


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
