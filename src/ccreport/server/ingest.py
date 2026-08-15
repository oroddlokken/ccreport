"""The write side: one machine's batch in, one verdict per file out.

A batch carries whole files and never part of one. That is what makes replacing
a file's rows wholesale correct, and it is why a crash can never leave the
delete committed without its re-insert — both are in the same transaction as
the records that arrived in the same request.

The response says what the server stored, per file, so the push client moves
its watermark on what happened rather than on what it hoped.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ccreport import exchange, pricing
from ccreport.server import db, tokens

router = APIRouter(prefix="/v1", tags=["ingest"])

ACCEPTED = "accepted"
SKIPPED = "skipped"
REJECTED = "rejected"


class IngestRecord(BaseModel):
    """One assistant message, as the client's record cache holds it.

    sid, project, cwd and repo are optional because a project a restricted
    machine has not opted in to pushes its token counts with exactly those
    stripped. cost is the log's own costUSD and nothing else — the server
    prices every record itself, and a client that has not pulled must not be
    able to write a stale price into the merged history.
    """

    mid: str | None = None
    model: str
    ts: float
    utc_offset: int | None = None
    """The machine's offset from UTC at *ts*, in seconds, which is what makes
    `day` the machine's own calendar day rather than the server's. Per record
    rather than per batch: a corpus spans months, and the offset moves with
    daylight saving. Absent from a client older than this field, and then the
    server's zone is the only answer available."""
    sid: str | None = None
    project: str | None = None
    cwd: str | None = None
    repo: str | None = None
    dk: str | None = None
    cost: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_create: int = 0
    cache_read: int = 0
    account_uuid: str
    account_label: str | None = None


class IngestFile(BaseModel):
    path: str
    mtime_ns: int
    size: int
    records: list[IngestRecord] = Field(default_factory=list)
    replace: bool = False
    """Store this file even though its fingerprint has not moved.

    The skip below is keyed on (mtime_ns, size), which answers "has the log
    changed" and not "has what the client would send changed". A restricted
    machine that drops a project from its allow list has to re-send closed
    logs under new names, and their fingerprints are the same as ever."""


class IngestBatch(BaseModel):
    label: str
    """The machine's hostname. Shown only until the machine is named in the web
    UI, which is where the label a person reads comes from."""
    client_version: str = ""
    files: list[IngestFile] = Field(default_factory=list)


class FileResult(BaseModel):
    path: str
    status: str
    records: int = 0
    detail: str | None = None


class IngestResponse(BaseModel):
    machine_id: str
    files: list[FileResult]


class HealthResponse(BaseModel):
    version: str
    machine_id: str
    label: str
    records: int


def server_version() -> str:
    """The running build's version, or "unknown" if the package is not installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ccreport")
    except PackageNotFoundError:
        return "unknown"


class Authenticated:
    """A push that presented a live token: which machine, under which hash."""

    def __init__(self, machine_id: str, token_hash: str) -> None:
        self.machine_id = machine_id
        self.token_hash = token_hash


def authenticate(
    request: Request, authorization: str | None = Header(default=None),
) -> Authenticated:
    """Resolve the bearer token to its machine, or refuse.

    401 with no detail for every failure. Saying which of unknown, malformed
    and revoked applies tells a caller holding a wrong token that it once was
    a right one.

    Not behind the web UI's network allowlist on purpose: a machine pushes from
    a hotel, and its token is the whole of what admits it.
    """
    token = tokens.bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="")
    conn = request.app.state.db.connect()
    digest = tokens.token_hash(token)
    machine_id = db.machine_for_token(conn, digest)
    if machine_id is None:
        raise HTTPException(status_code=401, detail="")
    db.touch_token(conn, digest, time.time())
    return Authenticated(machine_id, digest)


def enforce_body_limit(request: Request) -> None:
    """Refuse a batch bigger than the configured limit, naming the limit.

    Checked against Content-Length, which every HTTP client sending a JSON body
    provides. A chunked request carries no length to check and is left to the
    server in front; the limit exists so one oversized log is reported to its
    owner rather than retried forever, not as a defence against a crafted body.
    """
    limit = request.app.state.config.max_body_bytes
    raw = request.headers.get("content-length")
    if raw and raw.isdigit() and int(raw) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Batch is larger than the {limit} byte limit; split it or raise the limit.",
        )


def _priced(rec: IngestRecord) -> float:
    """What the record cost, computed here rather than taken from the client.

    Raises:
        LookupError: the model has no price at that timestamp. The file fails
            loudly instead: a silently zeroed model is a week of money that
            looks like an idle week.
    """
    when = datetime.fromtimestamp(rec.ts, tz=UTC)
    # A pseudo-model — "<synthetic>" — is a record Claude Code wrote for its own
    # bookkeeping. It has no price because it had no call, which is a known
    # zero rather than an unknown one.
    if pricing.find_pricing(rec.model, when) is None and not rec.model.startswith("<"):
        raise LookupError(rec.model)
    return pricing.calc_cost(
        rec.input_tokens, rec.output_tokens, rec.cache_create, rec.cache_read,
        rec.model, when,
    )


def _day(rec: IngestRecord, when: datetime) -> str:
    """The calendar day this record is bucketed under.

    The machine's own, from the offset it sent — a laptop working past midnight
    in another zone belongs to its day, not to the server's. A record without
    one falls back to the server's zone, which is wrong by up to a day and is
    still better than refusing to store it.
    """
    if rec.utc_offset is None:
        return when.astimezone().strftime("%Y-%m-%d")
    return (when + timedelta(seconds=rec.utc_offset)).strftime("%Y-%m-%d")


def _row(machine_id: str, path: str, rec: IngestRecord) -> tuple:
    """One record as a server_records insert row."""
    when = datetime.fromtimestamp(rec.ts, tz=UTC)
    return db.record_to_row({
        "machine_id": machine_id,
        "file_path": path,
        "account_uuid": rec.account_uuid,
        "account_label": rec.account_label,
        "mid": rec.mid,
        "model": rec.model,
        "ts": rec.ts,
        "day": _day(rec, when),
        "oslo_date": exchange.to_oslo_date(when).isoformat(),
        "sid": rec.sid,
        "project": rec.project,
        "cwd": rec.cwd,
        "repo": rec.repo,
        "dk": rec.dk,
        "cost": _priced(rec),
        "log_cost": rec.cost,
        "t": [rec.input_tokens, rec.output_tokens, rec.cache_create, rec.cache_read],
    })


def _ingest_file(conn, machine_id: str, item: IngestFile, now: float) -> FileResult:
    """Store one file, or say why it was skipped or refused."""
    if not item.replace and db.file_fingerprint(
        conn, machine_id, item.path,
    ) == (item.mtime_ns, item.size):
        return FileResult(path=item.path, status=SKIPPED, records=len(item.records))
    try:
        rows = [_row(machine_id, item.path, rec) for rec in item.records]
    except LookupError as exc:
        return FileResult(
            path=item.path, status=REJECTED,
            detail=f"No pricing for model {exc.args[0]!r}; the file was not stored.",
        )
    db.replace_file_records(
        conn, machine_id, item.path, item.mtime_ns, item.size, rows, now,
    )
    return FileResult(path=item.path, status=ACCEPTED, records=len(rows))


def _warm_rates(conn, files: list[IngestFile]) -> None:
    """Fetch the NOK rates this batch's dates need, best effort.

    The schema stores no converted amount — a rate is revised, and a stored
    conversion would outlive the revision — so the read side converts. Doing
    the fetch here means it happens once per push rather than once per report,
    and an unreachable Norges Bank costs this batch nothing: exchange.py
    degrades to whatever is cached and the read side does the same.
    """
    dates = {
        exchange.to_oslo_date(datetime.fromtimestamp(rec.ts, tz=UTC))
        for item in files for rec in item.records
    }
    if not dates:
        return
    try:
        exchange.load_rates(dates)
    except Exception:  # noqa: BLE001 - a rate fetch may never fail a push
        pass
    conn.commit()


@router.post("/ingest", response_model=IngestResponse,
             dependencies=[Depends(enforce_body_limit)])
def ingest(
    request: Request, batch: IngestBatch, auth: Authenticated = Depends(authenticate),
) -> IngestResponse:
    """Store one machine's batch, one transaction per file.

    A file that fails does not take the rest of the batch with it: each is
    reported on its own, and the client resends only what it has to.
    """
    conn = request.app.state.db.connect()
    now = time.time()
    db.upsert_machine(conn, auth.machine_id, batch.label, now)
    _warm_rates(conn, batch.files)
    results = [_ingest_file(conn, auth.machine_id, item, now) for item in batch.files]
    return IngestResponse(machine_id=auth.machine_id, files=results)


@router.get("/health", response_model=HealthResponse)
def health(request: Request, auth: Authenticated = Depends(authenticate)) -> HealthResponse:
    """Validate a token and describe what it belongs to.

    `ccreport server connect` calls this, so a token typed wrong fails at setup
    rather than silently at the first push half an hour later.
    """
    conn = request.app.state.db.connect()
    return HealthResponse(
        version=server_version(),
        machine_id=auth.machine_id,
        label=db.machine_label(conn, auth.machine_id) or "",
        records=db.record_count(conn, auth.machine_id),
    )
