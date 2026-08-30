"""The web UI: machines, minting, revoking and account names.

Server-rendered HTML with no client framework and no build step. Every route
here sits behind the network allowlist; ingest does not, because a machine
pushes from wherever it is.

Anyone on an allowed network can mint. There is no password.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ccreport import tier_timeline
from ccreport.server import dashboard, db, limits, reports, tokens

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
    view = dashboard.cached_build(
        request.app.state.db, days, hide_redacted=_hide_redacted(request),
    )
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


HIDE_REDACTED_COOKIE = "hide_redacted"
"""The cookie a browser asking to leave redacted spend out carries.

A cookie rather than a row: which spend a person wants drawn is a property of
the screen they are looking at, not of the server, and two people reading the
same dashboard from one machine's database disagree about it. There is no
login here to hang it off either.
"""

PREF_MAX_AGE_S = 400 * 24 * 60 * 60
"""How long a stored preference lives. Chrome caps a cookie at 400 days and
silently shortens anything longer, so this is the longest one that survives."""


def _hide_redacted(request: Request) -> bool:
    """Whether this browser asked for redacted spend to be left out."""
    return request.cookies.get(HIDE_REDACTED_COOKIE) == "1"


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, saved: str = Query(default="")):
    """What this browser has asked the dashboard to leave out.

    Registered above the machines, accounts and projects pages it sits beside,
    and unlike them it writes nothing anyone else can see.
    """
    return templates.TemplateResponse(request, "settings.html", {
        "hide_redacted": _hide_redacted(request),
        "note": "Saved." if saved else "",
    })


@router.post("/settings")
def save_settings(hide_redacted: str = Form("")):
    """Store the preferences this browser sends, or clear what it left out.

    An unticked checkbox is absent from the form rather than false, so a
    missing field deletes the cookie: the two states are what was posted and
    what was not.
    """
    response = RedirectResponse(url="/settings?saved=1", status_code=303)
    if hide_redacted:
        response.set_cookie(
            HIDE_REDACTED_COOKIE, "1", max_age=PREF_MAX_AGE_S,
            path="/", httponly=True, samesite="lax",
        )
    else:
        response.delete_cookie(HIDE_REDACTED_COOKIE, path="/")
    return response


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


@router.post("/settings/machines/{machine_id}/label")
def rename_machine(request: Request, machine_id: str, label: str = Form("")):
    """Name one machine, or clear the name back to its id.

    The records are untouched: what changes is the string every server view
    resolves a machine through, and the dashboard picks it up because
    db.content_stamp reads the stamp this writes. An id nothing was minted for
    is a 404 rather than a silent no-op, as the machine page already is.
    """
    conn = request.app.state.db.connect()
    if not db.set_machine_label(conn, machine_id, label, time.time()):
        raise HTTPException(status_code=404, detail=f"No machine {machine_id}.")
    conn.commit()
    return RedirectResponse(url="/settings/machines", status_code=303)


_ACCOUNTS_CACHE: dashboard.StampCache[list[dict]] = dashboard.StampCache()
_PROJECTS_CACHE: dashboard.StampCache[list[dict]] = dashboard.StampCache()


def _overview(request: Request, cache: dashboard.StampCache[list[dict]], build) -> list[dict]:
    """One /settings table, held against the stamp every dashboard view is.

    Both queries dedup the whole record table to sum a cost, which is seconds
    on a corpus of half a million and is the same answer until a push lands or
    a name is typed. A plan declared to start later in the day is the one thing
    the stamp does not carry, and it shows at the next midnight.
    """
    database = request.app.state.db
    conn = database.connect()
    now = datetime.now(tz=UTC).astimezone()
    return cache.get((str(database.path),), dashboard.cache_stamp(conn, now), lambda: build(conn))


@router.get("/settings/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    """Every account that has pushed, each row a field for the name to draw it under."""
    accounts = _overview(request, _ACCOUNTS_CACHE, reports.account_overview)
    return templates.TemplateResponse(request, "accounts.html", {"accounts": accounts})


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


def _account_names(account: dict) -> set[str]:
    """Every string a timeline may use to name *account*.

    The uuid, the login email it pushed under, and whatever this server renames
    it to — the three things a person has in front of them when they write the
    file, none of which they should have to look up.
    """
    return {v for v in (account["account_uuid"], account["label"], account["alias"]) if v}


def _tiers_page(request: Request, account_uuid: str, text: str, **note):
    """The timeline editor for one account, with *text* in the box.

    Takes the text rather than reading it back, so a paste that was refused is
    redisplayed as the person typed it. Re-rendering from the stored rows would
    hand back the document they were trying to replace, with their own work
    gone and the error pointing at a line no longer on screen.
    """
    accounts = {a["account_uuid"]: a
                for a in _overview(request, _ACCOUNTS_CACHE, reports.account_overview)}
    if account_uuid not in accounts:
        raise HTTPException(status_code=404, detail=f"No account {account_uuid}.")
    return templates.TemplateResponse(
        request, "tiers.html",
        {
            "account": accounts[account_uuid], "text": text,
            "names": sorted(_account_names(accounts[account_uuid])),
            "error": None, "saved": None, "skipped": (), **note,
        },
        status_code=422 if note.get("error") else 200,
    )


@router.get("/settings/accounts/{account_uuid}/tiers", response_class=HTMLResponse)
def tiers(request: Request, account_uuid: str):
    """One account's declared plan history, as the TOML it was typed in."""
    conn = request.app.state.db.connect()
    stored = [e for e in db.account_tiers(conn) if e.account == account_uuid]
    return _tiers_page(request, account_uuid, tier_timeline.render(stored))


@router.post("/settings/accounts/{account_uuid}/tiers", response_class=HTMLResponse)
def set_tiers(request: Request, account_uuid: str, timeline: str = Form("")):
    """Replace one account's plan history with the pasted document.

    A document that will not parse is refused whole and handed back with the
    reason. Storing the entries it did manage to read would leave a timeline
    that is neither what was there nor what was typed, and nothing on the page
    would say which lines had been dropped.

    An entry naming some other account is left alone rather than filed here, so
    one document covering a person's accounts can be pasted into each of their
    pages unedited. The page says which names it passed over: a typo and
    somebody else's account look identical from here, and only the person
    reading can tell which it was.

    Renders rather than redirects, because that count is the answer and a
    redirect has nowhere to carry it.
    """
    conn = request.app.state.db.connect()
    accounts = {a["account_uuid"]: a
                for a in _overview(request, _ACCOUNTS_CACHE, reports.account_overview)}
    if account_uuid not in accounts:
        raise HTTPException(status_code=404, detail=f"No account {account_uuid}.")
    try:
        entries = tier_timeline.parse(timeline)
    except ValueError as e:
        return _tiers_page(request, account_uuid, timeline, error=str(e))

    names = _account_names(accounts[account_uuid])
    mine = [replace(e, account=account_uuid) for e in entries if e.account in names]
    skipped = sorted({e.account for e in entries if e.account not in names})
    db.set_account_tiers(conn, account_uuid, mine, time.time())
    conn.commit()
    return _tiers_page(
        request, account_uuid, timeline, saved=len(mine), skipped=skipped,
    )


@router.get("/settings/projects", response_class=HTMLResponse)
def projects(request: Request):
    """Every (machine, project) pair that has pushed, each a field for its name.

    One row per pair rather than per name: a project name is only unique within
    the machine that pushed it, and folding two machines' names into one row is
    what typing the same name in both fields does.
    """
    projects = _overview(request, _PROJECTS_CACHE, reports.project_overview)
    return templates.TemplateResponse(request, "projects.html", {"projects": projects})


@router.post("/settings/projects/alias")
def set_project_alias(request: Request, machine_id: str = Form(...), project: str = Form(...),
                      alias: str = Form("")):
    """Name one machine's project, or clear the name back to what it pushed.

    The machine and the project ride in the form rather than in the path: a
    project name is free text and carries slashes often enough that a path
    segment would have to be decoded before it could be matched.

    The stored records are untouched, and the dashboard picks the name up
    because db.content_stamp reads project_aliases.
    """
    conn = request.app.state.db.connect()
    if not db.project_exists(conn, machine_id, project):
        raise HTTPException(status_code=404, detail=f"No project {project!r} on {machine_id}.")
    db.set_project_alias(conn, machine_id, project, alias, time.time())
    conn.commit()
    return RedirectResponse(url="/settings/projects", status_code=303)


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
         networks: str = Form(""), restricted: str = Form(""), allow: str = Form(""),
         exclude: str = Form("")):
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
                      "networks": networks, "restricted": bool(restricted), "allow": allow,
                      "exclude": exclude}},
            status_code=400,
        )
    token = tokens.mint(conn, machine_id, label.strip() or machine_id, time.time())
    conn.commit()
    return templates.TemplateResponse(request, "minted.html", {
        "machine_id": machine_id,
        "token": token,
        "command": tokens.connect_command(
            str(request.base_url), token,
            networks=networks, restricted=bool(restricted), allow=allow, exclude=exclude,
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


def _chart_payload(charts: list[dashboard.Chart]) -> str:
    """A page's charts as the JSON its script reads."""
    return _json_for_script([
        {
            "key": chart.key,
            "title": chart.title,
            "unit": chart.unit,
            "axis": chart.axis,
            "traces": [{"label": t.label, "values": t.values} for t in chart.traces],
        }
        for chart in charts
    ])


@router.get("/limits", response_class=HTMLResponse)
def limit_windows(request: Request, days: int = Query(default=dashboard.DEFAULT_RANGE)):
    """Every rate-limit window the machines pushed a reading of, merged.

    Named above the catch-all below, which would otherwise answer this path
    with a 404 for a dimension called "limits".

    Cached through `limits.cached_build`, on the stamp and the local date the
    dashboard's index is: the records behind it are loaded over the window
    spans rather than over the range, and that span holds still between pushes.
    """
    view = limits.cached_build(request.app.state.db, days)
    return templates.TemplateResponse(request, "limits.html", {
        "view": view,
        "ranges": dashboard.RANGES,
        "range_labels": dashboard.RANGE_LABELS,
    })


@router.get("/limits/{window}/{resets_at}", response_class=HTMLResponse)
def limit_window(request: Request, window: str, resets_at: float,
                 model: str = Query(default=""), account: str = Query(default=""),
                 stretch: int = Query(default=0)):
    """One window instance: its fill curve, and the work that drew it.

    The model and the account ride in the query string rather than the path.
    Both can carry a slash — an account renamed on the /settings page is
    whatever someone typed — and the reset time is what identifies the window
    within them. So does the stretch, which a plan change adds under an
    unchanged reset: a link with none names the curve that opened the window,
    which is the only one a link written before the split could have meant.

    A window nothing was pushed a reading of is a 404, for the reason a mistyped
    period key is: an empty page reads as a window nobody used.
    """
    try:
        view = limits.cached_window(
            request.app.state.db, window, resets_at, model or None, account, stretch,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"No {window} window stored for that reset.",
        ) from exc
    return templates.TemplateResponse(request, "limit.html", {
        "view": view,
        "charts": _chart_payload(view.charts),
    })


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

    Cached through `dashboard.cached_detail`, which invalidates on the same
    push or midnight `cached_build` does and evicts the least recently served
    entry: one entity per model, project, machine, account, day, week and month
    is a key space that grows daily, where the index's is one per range toggle.

    A period key the period cannot be keyed on is a 404 for the same reason:
    /month/2026-13 is a mistyped URL, and its empty page reads as an idle month.
    """
    if dimension not in dashboard.SCOPES:
        raise HTTPException(status_code=404, detail=f"No {dimension} pages.")
    scope = dashboard.Scope(dimension=dimension, key=key)
    try:
        view = dashboard.cached_detail(
            request.app.state.db, days, scope, hide_redacted=_hide_redacted(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"{key} is not a {dimension}.") from exc
    return templates.TemplateResponse(request, "detail.html", {
        "view": view,
        "scope": scope,
        "ranges": dashboard.RANGES,
        "range_labels": dashboard.RANGE_LABELS,
        "dimensions": [name for name in dashboard.SCOPES if name != dimension],
        "charts": _chart_payload(view.charts),
    })
