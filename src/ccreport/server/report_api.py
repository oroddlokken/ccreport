"""GET /v1/report/{kind}: the merged rows, as JSON the CLI can render.

Behind the same network allowlist as the pages. A report is read by a person,
from home, and the token auth on ingest exists for machines pushing from
anywhere — which a report is not.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from ccreport import aggregate
from ccreport.server import reports

router = APIRouter(prefix="/v1/report", tags=["report"])


def _parsed(value: str | None, field: str) -> datetime | None:
    """A YYYY-MM-DD or YYYYMMDD bound as local midnight, or None.

    Raises:
        HTTPException: the date does not parse. 400 rather than a silent
            ignore: a report over the wrong range looks exactly like a report.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").astimezone()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} is not a date: {value!r}") from None


@router.get("/{kind}")
def report(
    request: Request,
    kind: str,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    project: str | None = Query(default=None),
    account: str | None = Query(default=None),
    machine: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    breakdown: bool = Query(default=False),
) -> dict:
    """One merged report, its rows already folded and attributed."""
    if kind not in reports.KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"No report {kind!r}; try one of {', '.join(reports.KINDS)}.",
        )
    conn = request.app.state.db.connect()
    merged = reports.load(conn, reports.Filters(
        since=_parsed(since, "since"), until=_parsed(until, "until"),
        project=project, account=account, machine=machine,
    ))
    nok = reports.nok_context(merged)
    rows = reports.build(merged, kind, nok, limit=limit, breakdown=breakdown)

    payload = aggregate.rows_to_json(rows)
    payload["kind"] = kind
    payload["nok"] = {"enabled": nok.enabled, "label": nok.label}
    payload["machines"] = sorted({m.machine for m in merged})
    payload["accounts"] = sorted({m.account for m in merged})
    if kind == "month":
        payload["projection"] = aggregate.month_projection_to_json(
            aggregate.month_projection([m.record for m in merged], rows, nok),
        )
    return payload
