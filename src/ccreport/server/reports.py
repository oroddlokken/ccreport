"""The read side: merged records in, the same rows a local report has out.

Every number here goes through `aggregate.py`, the module the CLI renders from,
so a merged report and a single-machine one cannot disagree about what a week
cost. What this adds is the part only a merged report has: which machine and
which account each row came from.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from ccreport import aggregate, exchange, pricing, tier_timeline, windows
from ccreport.aggregate import NokCtx, ReportRows, Row, UsageRecord
from ccreport.server import db
from ccreport.server.db import REC_COLS


@dataclass(frozen=True)
class Filters:
    """What a report may narrow itself to. Every field is optional."""

    since: datetime | None = None
    until: datetime | None = None
    project: str | None = None
    account: str | None = None
    machine: str | None = None


@dataclass
class MergedRecord:
    """A record with the three things a local one has no room for.

    tier is the plan the account was on when the record was written, declared
    off its billing receipts rather than pushed. None where nothing declares
    one — which is every account until someone types a timeline in, and every
    moment older than the first receipt after they do.
    """

    record: UsageRecord
    machine: str
    account: str
    tier: str | None = None
    redacted: bool = False
    """Whether the pushing machine stripped this record's project name.

    The record's own project is the bucket those rows fold into, which reads
    as a project and is not one. Kept as the column's own answer rather than
    matched off the displayed name, which a real project called
    `foo-aggregated` would answer to as well.
    """


_SELECT = ", ".join(REC_COLS)


def account_display(account_uuid: str, account_label: str | None, aliases: dict[str, str]) -> str:
    """What this server calls an account: its alias, then its label, then its uuid.

    The one place both row builders spell it, so the dashboard and
    `ccreport --server` cannot disagree about a name. The label is the login
    email the pushing machine stamped on and stays in the record either way —
    an alias renames the view, not the history.
    """
    return aliases.get(account_uuid) or account_label or account_uuid


AGGREGATED = "aggregated"
"""The tail of the bucket every unallowed project folds into."""


def project_display(
    project: str | None, machine_id: str, account_uuid: str, account_label: str | None,
    aliases: dict[str, str], project_aliases: dict[tuple[str, str], str],
) -> str:
    """A record's project, or the bucket its account's redacted spend folds into.

    A named project is drawn under its alias where this server has one for that
    (machine, name) pair. Two machines that checked one repo out under
    different names are one row once both pairs carry the same alias, and the
    stored records keep the names they arrived with.

    A restricted machine sends a NULL project for everything it has not opted
    in to, and every one of those rows lands under one name per account: how
    many private projects there are, and what each costs, is the shape of the
    work and is the thing being kept back.

    Named off the account, with the alias replacing the whole account segment
    rather than sitting beside it — the point of naming an account is that the
    login email stops being drawn.

    The server holds no push policy, so it cannot tell a NULL project sent by a
    restricted machine from a NULL sent by anything else. Nothing else sends
    one today; the day something does, this needs a column to key on instead.
    """
    if project:
        return project_aliases.get((machine_id, project)) or project
    alias = aliases.get(account_uuid)
    if alias:
        return f"{alias}-{AGGREGATED}"
    return f"{account_label or account_uuid}/{AGGREGATED}"


def _clauses(
    filters: Filters, table: str = "", aliased: tuple[str, ...] = (),
    pairs: tuple[tuple[str, str], ...] = (),
) -> tuple[list[str], list]:
    """One condition per set filter, qualified by *table*, and their parameters.

    ts bounds rather than day bounds: `day` is the machine's own calendar day
    and two machines can disagree about which day an instant falls in, so a
    range that means the same thing to both has to be stated in instants.

    *aliased* is the accounts whose alias the account filter spells, so a name
    typed off the dashboard selects the same rows the stored email does.
    *pairs* is the same for projects: the (machine, pushed name) pairs the
    project filter's alias names, each matched on both halves because a name is
    only unique within the machine that pushed it.
    """
    at = f"{table}." if table else ""
    clauses, params = [], []
    if filters.since is not None:
        clauses.append(f"{at}ts >= ?")
        params.append(filters.since.timestamp())
    if filters.until is not None:
        clauses.append(f"{at}ts < ?")
        params.append(filters.until.timestamp())
    if filters.project is not None:
        matches = [f"{at}project = ?"]
        params.append(filters.project)
        for machine_id, project in pairs:
            matches.append(f"({at}machine_id = ? AND {at}project = ?)")
            params += [machine_id, project]
        clauses.append("(" + " OR ".join(matches) + ")")
    if filters.account is not None:
        matches = [f"{at}account_label = ?", f"{at}account_uuid = ?"]
        params += [filters.account, filters.account]
        matches += [f"{at}account_uuid = ?"] * len(aliased)
        params += list(aliased)
        clauses.append("(" + " OR ".join(matches) + ")")
    if filters.machine is not None:
        clauses.append(f"{at}machine_id = ?")
        params.append(filters.machine)
    return clauses, params


def _where(
    filters: Filters, aliased: tuple[str, ...] = (), pairs: tuple[tuple[str, str], ...] = (),
) -> tuple[str, list]:
    """The filter clause and its parameters, or an empty clause."""
    clauses, params = _clauses(filters, aliased=aliased, pairs=pairs)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def load(conn: sqlite3.Connection, filters: Filters | None = None) -> list[MergedRecord]:
    """Every stored record the filters admit, deduplicated, oldest first.

    Two machines sharing a synced home directory both stored their copy of the
    same call. Here they collapse to one, keyed by account and dedup key, and
    the copy that survives is the one with the lowest id — the machine that
    reported it first. So a synced log neither double-counts nor silently
    erases one machine's contribution.

    A record with no dedup key is kept as it stands: the log carried nothing to
    match it on, and dropping it would lose a real call.

    Where the filters cannot move which copy wins, the stored flag drops the
    losers in SQL and the loop below finds nothing left to collapse. That is
    half the table on a corpus two machines have both pushed — rows this used to
    read, build a UsageRecord for, and throw away. The loop stays because it is
    what answers the filtered case, where no stored flag can.
    """
    filters = filters or Filters()
    aliases = db.account_aliases(conn)
    proj_aliases = db.project_aliases(conn)
    tiers = tier_timeline.TierTimeline(db.account_tiers(conn))
    clause, params = _where(
        filters, db.accounts_with_alias(conn, filters.account),
        db.projects_with_alias(conn, filters.project),
    )
    if not _narrows_dedup(filters):
        clause = f"{clause} AND dup = 0" if clause else " WHERE dup = 0"
    rows = conn.execute(
        f"SELECT machine_id, {_SELECT} FROM server_records{clause} ORDER BY id",  # noqa: S608
        params,
    ).fetchall()

    labels = dict(conn.execute("SELECT machine_id, label FROM machines").fetchall())
    seen: set[tuple[str, str]] = set()
    merged: list[MergedRecord] = []
    for row in rows:
        machine_id, rec = row[0], dict(zip(REC_COLS, row[1:], strict=True))
        key = (rec["account_uuid"], rec["dk"])
        if rec["dk"] and key in seen:
            continue
        seen.add(key)
        merged.append(
            _as_merged(rec, labels.get(machine_id, machine_id), aliases, proj_aliases, tiers)
        )
    merged.sort(key=lambda m: m.record.timestamp)
    return merged


def _as_merged(
    rec: dict, machine_label: str, aliases: dict[str, str],
    proj_aliases: dict[tuple[str, str], str],
    tiers: tier_timeline.TierTimeline | None = None,
) -> MergedRecord:
    """One stored row as the record the aggregation understands.

    day and oslo_date travel on the record rather than being re-derived: they
    were computed against the machine's own zone at ingest, and the server's is
    nobody's working day.
    """
    account = account_display(rec["account_uuid"], rec["account_label"], aliases)
    record = UsageRecord(
        message_id=rec["mid"] or "",
        model=rec["model"],
        tokens=aggregate.TokenCounts(
            input=rec["input_tokens"], output=rec["output_tokens"],
            cache_create=rec["cache_create"], cache_read=rec["cache_read"],
        ),
        timestamp=datetime.fromtimestamp(rec["ts"], tz=UTC),
        session_id=rec["sid"] or "",
        project=project_display(
            rec["project"], rec["machine_id"], rec["account_uuid"], rec["account_label"],
            aliases, proj_aliases,
        ),
        # The server's own price, always. cost_usd is the field the aggregation
        # reads as "already known", which here it is: the server computed it at
        # ingest with its own pricing.py and stored it NOT NULL.
        cost_usd=rec["cost"],
        dedup_key=rec["dk"],
        account=account,
        oslo_date=date.fromisoformat(rec["oslo_date"]),
    )
    # The stored day is the machine's calendar day. Priming the memo is what
    # makes the daily and monthly reports bucket by it rather than by the
    # server's midnight.
    record._day = rec["day"]  # noqa: SLF001 - the memo is the point
    return MergedRecord(
        record=record, machine=machine_label, account=account,
        tier=tiers.at(rec["account_uuid"], rec["ts"]) if tiers else None,
        redacted=rec["project"] is None,
    )


GROUP_COLS = ("machine_id", "account_uuid", "account_label", "project", "model", "day", "oslo_date")
"""What one grouped row stands for. Every column a merged report groups on
except the session, which no page folding these rows breaks down by."""

def _narrows_dedup(filters: Filters) -> bool:
    """Whether *filters* can change which copy of a shared call survives.

    Project and machine can. Ask for the desk alone and the desk's copy of a
    synced call wins, where over both machines the laptop's did; ask for one of
    the two names a repo was checked out under and that machine's copy wins.
    Both are what a page about one machine or one project is for.

    Account cannot: the dedup groups by account already, so narrowing to one
    account leaves every group inside it whole. Neither can the date bounds --
    two copies of one call carry the ts their log gave them, so no bound splits
    a pair.
    """
    return filters.project is not None or filters.machine is not None


def _dedup_clause(
    filters: Filters, aliased: tuple[str, ...] = (), pairs: tuple[tuple[str, str], ...] = (),
) -> tuple[str, list]:
    """load()'s dedup, stated in SQL, and the parameters it binds.

    The lowest id per (account, dedup key) wins and a record with no key is
    kept as it stands.

    Where the filters cannot move that answer — which is every page this server
    draws — it is `server_records.dup`, decided when the row was written. The
    subquery it replaces materialised 384,063 MIN(id) rows on every request, and
    the flag took the dashboard fold from 1.69s to 0.20s and the accounts page
    from 2.16s to 0.23s.

    Where they can, the subquery still runs, over the same filters minus the
    date bounds: which copy survives depends on the set being deduped, and a
    stored flag only ever answers for the whole table. That is `/v1/report`
    narrowed to a project or a machine, and nothing else.
    """
    if not _narrows_dedup(filters):
        return "a.dup = 0", []
    clauses, params = _clauses(
        replace(filters, since=None, until=None), aliased=aliased, pairs=pairs,
    )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    inner = f"SELECT MIN(id) FROM server_records{where} GROUP BY account_uuid, dk"  # noqa: S608
    return f"(a.dk IS NULL OR a.dk = '' OR a.id IN ({inner}))", params


_GROUP_LIST = ", ".join(f"a.{name}" for name in GROUP_COLS)
_GROUPED_SQL = f"""
    SELECT {_GROUP_LIST}, MIN(a.ts), SUM(a.cost), COUNT(*),
           SUM(a.input_tokens), SUM(a.output_tokens), SUM(a.cache_create), SUM(a.cache_read)
      FROM server_records a
     WHERE %s
     GROUP BY {_GROUP_LIST}
"""  # noqa: S608 - GROUP_COLS is a literal tuple; every filter binds a parameter


def load_grouped(conn: sqlite3.Connection, filters: Filters | None = None) -> list[MergedRecord]:
    """The same records load() returns, pre-folded by SQL to one per GROUP_COLS.

    A page that only ever sums cannot tell these from the records they stand
    for: each carries its group's summed tokens and cost, its call count in
    `count`, and the day and Oslo date every row in it shares. It is the same
    trick the CLI's rollup table plays, and the aggregation is the same code.

    What it buys is the corpus never reaching Python: a merged history of half
    a million calls is a few thousand groups.

    Not for a session report or anything else keyed on a record's identity —
    session_id and message_id are empty here, so every row would collapse into
    one bucket rather than raise.
    """
    filters = filters or Filters()
    aliases = db.account_aliases(conn)
    proj_aliases = db.project_aliases(conn)
    tiers = tier_timeline.TierTimeline(db.account_tiers(conn))
    aliased = db.accounts_with_alias(conn, filters.account)
    pairs = db.projects_with_alias(conn, filters.project)
    dedup, dedup_params = _dedup_clause(filters, aliased, pairs)
    clauses, params = _clauses(filters, table="a", aliased=aliased, pairs=pairs)
    rows = conn.execute(
        _GROUPED_SQL % " AND ".join([dedup, *clauses]), dedup_params + params,
    ).fetchall()

    labels = dict(conn.execute("SELECT machine_id, label FROM machines").fetchall())
    merged = [_as_grouped(row, labels, aliases, proj_aliases, tiers) for row in rows]
    merged.sort(key=lambda m: m.record.timestamp)
    return merged


_BUCKETED_SQL = f"""
    SELECT {_GROUP_LIST}, MIN(a.ts), SUM(a.cost), COUNT(*),
           SUM(a.input_tokens), SUM(a.output_tokens), SUM(a.cache_create), SUM(a.cache_read)
      FROM server_records a
     WHERE %s
     GROUP BY {_GROUP_LIST}, CAST(FLOOR((a.ts - ?) / ?) AS INTEGER)
"""  # noqa: S608 - GROUP_COLS is a literal tuple; every filter binds a parameter


def load_bucketed(
    conn: sqlite3.Connection, filters: Filters | None = None, *,
    origin: float, step: float,
) -> list[MergedRecord]:
    """load_grouped's rows, cut again at every *step* from *origin*.

    For a page that plots a span finer than the day `load_grouped` folds to.
    A window page's axis is five minutes or an hour wide, and folding to
    exactly that is lossless for it: every chart sums its records into those
    same buckets, and a breakdown sums them all.

    `FLOOR` rather than a bare `CAST`, which truncates toward zero and would
    put the bucket before the origin in the one after it. Each group carries
    its earliest instant, as `load_grouped`'s do, and that instant is inside
    the group's own bucket — so a caller that recomputes the position from it
    lands where the grouping put it, with no rounding to agree about.

    Every field is real; nothing here is hollowed out. The one thing folded
    away is which call within a (identity, bucket) pair was which, which is
    what `count` says instead.
    """
    filters = filters or Filters()
    aliases = db.account_aliases(conn)
    proj_aliases = db.project_aliases(conn)
    tiers = tier_timeline.TierTimeline(db.account_tiers(conn))
    aliased = db.accounts_with_alias(conn, filters.account)
    pairs = db.projects_with_alias(conn, filters.project)
    dedup, dedup_params = _dedup_clause(filters, aliased, pairs)
    clauses, params = _clauses(filters, table="a", aliased=aliased, pairs=pairs)
    rows = conn.execute(
        _BUCKETED_SQL % " AND ".join([dedup, *clauses]),
        [*dedup_params, *params, origin, step],
    ).fetchall()

    labels = dict(conn.execute("SELECT machine_id, label FROM machines").fetchall())
    merged = [_as_grouped(row, labels, aliases, proj_aliases, tiers) for row in rows]
    merged.sort(key=lambda m: m.record.timestamp)
    return merged


_SPEND_SQL = """
    SELECT a.account_uuid, a.account_label, a.ts, a.cost, a.cache_read,
           a.input_tokens, a.cache_create, a.model
      FROM server_records a
     WHERE %s
  ORDER BY a.ts
"""


def load_spend(
    conn: sqlite3.Connection, filters: Filters | None = None,
) -> dict[str, list[windows.SpendRow]]:
    """The five numbers a `windows.SpendIndex` keeps, per displayed account.

    Deduplicated and ordered like `load`, which is what the bisect behind an
    index needs, but stopping at the columns that index reads. A window page
    was building 83,250 UsageRecords -- each with a project, a session, a tier
    lookup and three date derivations -- to sum three columns, and threw every
    one of them away.

    Per account because a window belongs to one: pricing a shared machine's
    windows against everyone's work would bill one person for another's.
    """
    filters = filters or Filters()
    aliases = db.account_aliases(conn)
    aliased = db.accounts_with_alias(conn, filters.account)
    pairs = db.projects_with_alias(conn, filters.project)
    dedup, dedup_params = _dedup_clause(filters, aliased, pairs)
    clauses, params = _clauses(filters, table="a", aliased=aliased, pairs=pairs)
    rows = conn.execute(
        _SPEND_SQL % " AND ".join([dedup, *clauses]), dedup_params + params,
    ).fetchall()

    # One family per distinct model rather than per call: the corpus names a
    # handful of models and the lookup is a scan of each name.
    families: dict[str, str] = {}
    per_account: dict[str, list[windows.SpendRow]] = {}
    for uuid, label, ts, cost, cache_read, tokens_in, cache_create, model in rows:
        family = families.get(model)
        if family is None:
            family = families[model] = pricing.model_family(model)
        per_account.setdefault(account_display(uuid, label, aliases), []).append(
            (ts, cost, float(cache_read), float(tokens_in + cache_create + cache_read), family),
        )
    return per_account


def _as_grouped(
    row: tuple, labels: dict, aliases: dict[str, str],
    proj_aliases: dict[tuple[str, str], str],
    tiers: tier_timeline.TierTimeline | None = None,
) -> MergedRecord:
    """One grouped row as the record the aggregation understands.

    The tier comes off the group's earliest instant, as its price does. Every
    row in a group shares a day, and a plan change inside one leaves the whole
    day reading as the plan it started on — the grain a declared timeline can
    be read at, not a rounding this builder chose.
    """
    machine_id: str = row[0]
    account_uuid, account_label, project, model, day, oslo_date = row[1:len(GROUP_COLS)]
    first_ts, cost, calls, tokens_in, tokens_out, cache_create, cache_read = row[len(GROUP_COLS):]
    account = account_display(account_uuid, account_label, aliases)
    record = UsageRecord(
        message_id="",
        model=model,
        tokens=aggregate.TokenCounts(
            input=tokens_in, output=tokens_out, cache_create=cache_create, cache_read=cache_read,
        ),
        # The group's earliest instant. Every row in it shares a model and a
        # day, so it prices the group exactly where a per-record timestamp
        # would — and the dashboard's cache-reads tile asks for nothing else.
        timestamp=datetime.fromtimestamp(first_ts, tz=UTC),
        session_id="",
        project=project_display(
            project, machine_id, account_uuid, account_label, aliases, proj_aliases,
        ),
        cost_usd=cost,
        count=calls,
        account=account,
        oslo_date=date.fromisoformat(oslo_date),
    )
    record._day = day  # noqa: SLF001 - the memo is the point; see _as_merged
    return MergedRecord(
        record=record, machine=labels.get(machine_id, machine_id), account=account,
        tier=tiers.at(account_uuid, first_ts) if tiers else None,
        redacted=project is None,
    )


def account_overview(conn: sqlite3.Connection) -> list[dict]:
    """Every account with records here, its stored label, its alias and its spend.

    Deduped through _dedup_clause like every other read of this table, and here
    rather than in db.py for that reason. Two machines that share session logs
    push the same call twice; a raw SUM drew 2.2x what the dashboard drew for
    the same corpus, on a page whose whole job is to say who spent what.

    The label is the newest one that account pushed: a person who changed their
    login email has both in the table, and the older one is not who they are.
    """
    dedup, params = _dedup_clause(Filters())
    rows = conn.execute(f"""
        SELECT a.account_uuid, COUNT(*), COALESCE(SUM(a.cost), 0),
               (SELECT r.account_label FROM server_records r
                 WHERE r.account_uuid = a.account_uuid AND r.account_label IS NOT NULL
              ORDER BY r.ts DESC LIMIT 1),
               (SELECT al.alias FROM account_aliases al WHERE al.account_uuid = a.account_uuid)
          FROM server_records a
         WHERE {dedup}
      GROUP BY a.account_uuid
      ORDER BY SUM(a.cost) DESC
    """, params).fetchall()  # noqa: S608 - dedup is a literal clause; its filters bind parameters
    # The plan in force now, and how many changes are declared behind it. Off
    # the same timeline the row builders resolve against, so the column and the
    # charts cannot disagree about which plan an account is on.
    entries = db.account_tiers(conn)
    tiers = tier_timeline.TierTimeline(entries)
    declared: dict[str, int] = {}
    for e in entries:
        declared[e.account] = declared.get(e.account, 0) + 1
    now = time.time()
    return [
        {
            "account_uuid": row[0], "records": row[1], "cost": row[2],
            "label": row[3], "alias": row[4],
            "tier": tiers.at(row[0], now), "declared": declared.get(row[0], 0),
        }
        for row in rows
    ]


def account_names(conn: sqlite3.Connection) -> list[dict]:
    """Every account with records here and the newest label it pushed.

    The two fields off account_overview that naming an account needs, without
    the totals beside them: that query deduplicates the whole table to sum a
    cost, which took 2.23s of the 4.30s a detail page spent building where a
    distinct over the account column takes 0.07s, and a caller that only wants
    a name discards every figure it paid for.

    The label is the newest one that account pushed, as it is there: a person
    who changed their login email has both in the table.
    """
    rows = conn.execute("""
        SELECT a.account_uuid,
               (SELECT r.account_label FROM server_records r
                 WHERE r.account_uuid = a.account_uuid AND r.account_label IS NOT NULL
              ORDER BY r.ts DESC LIMIT 1)
          FROM (SELECT DISTINCT account_uuid FROM server_records) a
    """).fetchall()
    return [{"account_uuid": row[0], "label": row[1]} for row in rows]


def project_overview(conn: sqlite3.Connection) -> list[dict]:
    """Every (machine, project) pair with records here, its spend and its alias.

    Deduped through _dedup_clause, and here rather than in db.py for the reason
    account_overview is: one call pushed under two file paths is one call, and
    the raw sum drew 2.16x what the project page drew for the same rows.

    The surviving copy is the lowest id per (account, dedup key) across the
    whole table, so a call two machines both pushed lands on whichever one's
    copy won. That is the trade account_overview already makes and what the
    dashboard shows, and the Records column counts the same set the cost does.

    A record whose project is NULL is left out: a restricted machine stripped
    the name, and the bucket those rows fold into is named off the account.
    """
    dedup, params = _dedup_clause(Filters())
    rows = conn.execute(f"""
        SELECT a.machine_id, a.project,
               (SELECT m.label FROM machines m WHERE m.machine_id = a.machine_id),
               COUNT(*), COALESCE(SUM(a.cost), 0),
               (SELECT p.alias FROM project_aliases p
                 WHERE p.machine_id = a.machine_id AND p.project = a.project)
          FROM server_records a
         WHERE a.project IS NOT NULL AND {dedup}
      GROUP BY a.machine_id, a.project
      ORDER BY SUM(a.cost) DESC
    """, params).fetchall()  # noqa: S608 - dedup is a literal clause; its filters bind parameters
    return [
        {"machine_id": row[0], "project": row[1], "machine": row[2] or row[0],
         "records": row[3], "cost": row[4], "alias": row[5]}
        for row in rows
    ]


def nok_context(merged: list[MergedRecord], *, mva: bool = True) -> NokCtx:
    """A NokCtx over the rates this server holds for the dates in *merged*.

    The server converts, so two clients cannot disagree about the same week.
    Each record converts at its own Oslo date, which travelled with it from
    ingest — not at the date the report was asked for.
    """
    if not merged:
        return NokCtx(mva=mva)
    earliest = min(m.record.fx_date() for m in merged)
    rates = exchange.read_rates_since(earliest)
    if not rates:
        return NokCtx(mva=mva)
    return NokCtx(rates, max(rates), mva)


def _attribute(rows: list[Row], merged: list[MergedRecord], key_fn) -> None:
    """Fill each row's machine and account split.

    The same bucketing the rows came from, run once more over the pair of
    labels: a row's key decides which records belong to it, and each record
    knows which machine stored it and which account paid.
    """
    by_key: dict[str, list[MergedRecord]] = {}
    for item in merged:
        by_key.setdefault(str(key_fn(item)), []).append(item)
    for row in rows:
        for item in by_key.get(row.key, ()):
            cost = item.record.cost()
            row.machines[item.machine] = row.machines.get(item.machine, 0.0) + cost
            row.accounts[item.account] = row.accounts.get(item.account, 0.0) + cost


_KEY_FNS = {
    "day": lambda m: m.record.day_key(),
    "month": lambda m: m.record.month_key(),
    "project": lambda m: m.record.project,
    "session": lambda m: m.record.session_id,
    "account": lambda m: m.record.account,
}

KINDS = tuple(_KEY_FNS)


def build(
    merged: list[MergedRecord], kind: str, nok: NokCtx, *,
    limit: int | None = None, breakdown: bool = False,
) -> ReportRows:
    """One report's rows, with the machine and account split filled in.

    Raises:
        KeyError: *kind* is not one of KINDS.
    """
    key_fn = _KEY_FNS[kind]
    records = [m.record for m in merged]
    if kind == "day":
        report = aggregate.daily_rows(records, nok, breakdown=breakdown)
    elif kind == "month":
        report = aggregate.monthly_rows(records, nok)
    elif kind == "project":
        report = aggregate.project_rows(records, nok, limit=limit)
    elif kind == "session":
        report = aggregate.session_rows(records, nok, limit=limit)
    else:
        report = aggregate.account_rows(records, nok)
    _attribute(report.rows, merged, key_fn)
    return report
