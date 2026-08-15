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
STATIC_DIR = Path(__file__).with_name("static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _asset(name: str) -> str:
    """A /static URL stamped with the file's mtime.

    StaticFiles sends no Cache-Control, which leaves a browser free to hold an
    old app.css for as long as its own heuristic allows. The stamp is what makes
    an edit reach a tab that is already open. It is read per render rather than
    cached for the process: this server runs from a working tree, where a file
    changes under a process that keeps running.

    A file that will not stat still gets its URL — a missing asset is StaticFiles'
    404 to report, not this function's exception.
    """
    try:
        return f"/static/{name}?mtime={int(Path(STATIC_DIR, name).stat().st_mtime)}"
    except OSError:
        return f"/static/{name}"


def _json_for_script(payload: object) -> str:
    """json.dumps, safe to inline inside a script element.

    json.dumps leaves "</" alone, so an account or project pushed as
    "</script><script>…" would end the element and run as markup. Escaping the
    slash keeps the JSON identical to a parser and inert to the HTML tokenizer.
    """
    return json.dumps(payload).replace("</", "<\\/")


def _when(ts: float | None) -> str:
    """An epoch as a readable local stamp, or a dash where there is nothing."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["when"] = _when
templates.env.globals["asset"] = _asset


@router.get("/", response_class=HTMLResponse)
def index(request: Request, days: int = Query(default=dashboard.DEFAULT_RANGE),
          by: str = Query(default=""), metric: str = Query(default="")):
    """The merged spend dashboard.

    *by* and *metric* are which breakdown table and which chart series the page
    opens on. They stay out of `cached_build`'s key, because the view carries
    every breakdown and both series whichever one is showing — the query string
    is where they live so a reload lands on the last click rather than back at
    model and cost.
    """
    view = dashboard.cached_build(request.app.state.db, days)
    return templates.TemplateResponse(request, "dashboard.html", {
        "view": view,
        "ranges": dashboard.RANGES,
        "range_labels": dashboard.RANGE_LABELS,
        "dimensions": dashboard.DIMENSIONS,
        "dimension": by if by in dashboard.DIMENSIONS else dashboard.DIMENSIONS[0],
        "metrics": dashboard.METRICS,
        "metric": metric if metric in dashboard.METRICS else dashboard.METRICS[0],
        "chart": _json_for_script({
            "days": view.chart_days,
            "series": [
                {"account": s.account, "cost": s.cost, "tokens": s.tokens}
                for s in view.series
            ],
        }),
    })


@router.get("/settings/machines", response_class=HTMLResponse)
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


@router.get("/settings/machines/{machine_id}", response_class=HTMLResponse)
def machine(request: Request, machine_id: str):
    """One machine's tokens, each with a revoke and a delete."""
    return _machine_page(request, machine_id)


@router.get("/settings/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    """Every account that has pushed, each row a field for the name to draw it under."""
    conn = request.app.state.db.connect()
    return templates.TemplateResponse(
        request, "accounts.html", {"accounts": db.account_overview(conn)},
    )


@router.post("/settings/accounts/{account_uuid}/alias")
def set_alias(request: Request, account_uuid: str, alias: str = Form("")):
    """Name one account, or clear the name back to the label it pushed under.

    The stored records are untouched: what changes is the string every server
    view resolves through, and the dashboard picks it up because
    db.content_stamp reads account_aliases.
    """
    conn = request.app.state.db.connect()
    db.set_account_alias(conn, account_uuid, alias, time.time())
    conn.commit()
    return RedirectResponse(url="/settings/accounts", status_code=303)


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


@router.post("/settings/machines/mint", response_class=HTMLResponse)
def mint(request: Request, machine_id: str = Form(...), label: str = Form(""),
         networks: str = Form(""), restricted: str = Form(""), allow: str = Form("")):
    """Mint a token and show it once, with the command that consumes it.

    The push policy is written into that command and stored nowhere: it lives
    in the machine's own push.toml, and this is only where it gets typed.
    """
    conn = request.app.state.db.connect()
    machine_id = machine_id.strip()
    error = ""
    if not machine_id:
        error = "A machine id is required."
    elif (bad := _bad_cidr(networks)):
        error = f"{bad} is not a network."
    if error:
        # The submitted values ride back into the form: a policy that took a
        # minute to type is not retyped over one bad CIDR.
        return templates.TemplateResponse(
            request, "machines.html",
            {"machines": db.machine_overview(conn), "error": error,
             "form": {"machine_id": machine_id, "label": label.strip(),
                      "networks": networks, "restricted": bool(restricted), "allow": allow}},
            status_code=400,
        )
    token = tokens.mint(conn, machine_id, label.strip() or machine_id, time.time())
    conn.commit()
    return templates.TemplateResponse(request, "minted.html", {
        "machine_id": machine_id,
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


def _chart_payload(view: dashboard.Dashboard) -> str:
    """The detail page's charts as the JSON its script reads."""
    return _json_for_script([
        {
            "key": chart.key,
            "title": chart.title,
            "unit": chart.unit,
            "axis": chart.axis,
            "traces": [{"label": t.label, "values": t.values} for t in chart.traces],
        }
        for chart in view.charts
    ])


@router.post("/settings/machines/{machine_id}/delete")
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
        url=f"/settings/machines?deleted={quote(machine_id)}&records={destroyed:d}", status_code=303,
    )


@router.get("/{dimension}/{key:path}", response_class=HTMLResponse)
def detail(request: Request, dimension: str, key: str,
           days: int = Query(default=dashboard.DEFAULT_RANGE)):
    """One entity's page: the same fold, over the records that match it alone.

    Registered last, so every page above owns its own path and only what none
    of them claimed reaches here. A dimension this server has no breakdown for
    is a 404: the URL was mistyped, and an empty page reads as an idle month.

    Not cached. `cached_build` holds the whole-server view per range, which is
    the page a browser opens over and over; one entity is a page someone
    clicked into.
    """
    if dimension not in dashboard.SCOPES:
        raise HTTPException(status_code=404, detail=f"No {dimension} pages.")
    scope = dashboard.Scope(dimension=dimension, key=key)
    view = dashboard.build(request.app.state.db.connect(), days, scope=scope)
    return templates.TemplateResponse(request, "detail.html", {
        "view": view,
        "scope": scope,
        "ranges": dashboard.RANGES,
        "range_labels": dashboard.RANGE_LABELS,
        "dimensions": [name for name in dashboard.SCOPES if name != dimension],
        "charts": _chart_payload(view),
    })
