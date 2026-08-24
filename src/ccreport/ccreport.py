"""Analyze Claude Code token usage and costs from local JSONL session logs.

AUDIT: All calculations are documented in docs/calculation-reference.md.
When changing any calculation, caching, or data format here,
update that document to match.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ccreport import (
    accounts,
    aggregate,
    cache_db,
    exchange,
    pricing,
    project_identity,
    protocol,
    tier_timeline,
)
from ccreport.accounts import AccountTimeline
from ccreport.aggregate import (
    UNKNOWN_ACCOUNT,
    AggBucket,
    NokCtx,
    ReportRows,
    TokenCounts,
    UsageRecord,
    accounts_worth_showing,
    by_cost_desc,
    record_cost,
    record_cost_nok,
    short_model,
)
from ccreport.cache_db import (
    _ACCOUNT_IDENTITY_COLS,
    _ACCOUNT_TIER_COLS,
    SOURCE_BACKFILL,
    SOURCE_CAPTURE,
    add_project_override,
    archived_file_paths,
    backfill_account_events,
    clear_adopted_account,
    clear_backfilled_accounts,
    count_ccreport_records_without_signals,
    delete_project_override,
    get_project_overrides,
    load_account_events,
    load_ccreport_archive,
    load_ccreport_file_meta,
    load_ccreport_file_meta_before,
    load_ccreport_records_in_range,
    load_ccreport_records_since,
    load_ccreport_rollups,
    load_rate_limit_snapshots,
    read_adopted_account,
    read_ccreport_rollup_fingerprint,
    read_latest_account,
    save_ccreport_archive,
    save_ccreport_rollups,
    set_adopted_account,
)
from ccreport.exchange import RateFetch, get_rate, load_rates
from ccreport.pricing import _get_projects_dirs, _local_tz, _project_dir_prefix, dedup_identity

# Reading the logs into the cache moved to scan.py so the detached push could
# refresh it without importing rich, as the account timeline moved to
# accounts.py before it. The reports call these bindings rather than scan's own
# attributes, so a test that patches one still patches what a report uses.
from ccreport.scan import (
    CACHE_VERSION,
    _deserialize_records,
    _ensure_cache_valid,
    _refresh_changed_files,
    _script_hash,
    discover_jsonl_files,
)
from ccreport.windows import (
    LIMIT_WINDOW_LABELS as _LIMIT_WINDOW_LABELS,
)
from ccreport.windows import (
    LIMIT_WINDOWS,
    ExtraIndex,
    SpendIndex,
    WindowInstance,
    WindowSpend,
    group_cache_hit,
    implausible_reset,
    instance_order,
    instance_spend,
    window_instances,
    window_types,
)

# The record model and every report's aggregation live in aggregate.py, which
# imports no rich, so the server can fold the same records with no terminal to
# draw on. These two names stay reachable here because that is where the CLI
# and its tests have always found them.
_bucket_by = aggregate.bucket_by
_accounts_worth_showing = accounts_worth_showing
record_oslo_date = aggregate.record_oslo_date
# The account timeline moved to accounts.py so the detached push client could
# read it without importing rich; it answers here where it always has.
_account_labels = accounts._account_labels  # noqa: SLF001 - the move kept the name

# Project naming and the merge/override rules are shared with pricing.py, which
# scopes the statusline's per-project costs by them; see project_identity.
_CONFIG_PATH = project_identity.CONFIG_PATH
_build_override_fn = project_identity.build_override_fn
_implied_name = project_identity.implied_name
_repo_from_path = project_identity.repo_from_path



# --- Account attribution ---


def _account_description(identity: dict) -> str:
    """One account identity on a line, for a prompt rather than a table cell.

    Deliberately not _account_labels: that decides between bare and
    org-qualified by looking at the whole log, and a confirmation prompt should
    name the organization every time — it is half of what the user is being
    asked to confirm.
    """
    who = identity["email"] or identity["account_uuid"]
    org = identity["organization_name"]
    return f"{who} ({org})" if org else who


def _same_account(a: dict, b: dict) -> bool:
    """Whether two account rows name the same account, ignoring when each was written.

    Compared on the stored identity rather than on the rendered description,
    which collapses two uuids that happen to share an address. The identity
    columns only: a row also carries the tiers that account was on, and a seat
    upgrade does not make it somebody else — a caller asking "is this the same
    account?" would otherwise get "no" from a plan change.
    """
    return all(a[col] == b[col] for col in _ACCOUNT_IDENTITY_COLS)


def load_rates_for_records(
    records: list[UsageRecord], *, mva: bool = True, prefetch: RateFetch | None = None,
) -> tuple[NokCtx, bool]:
    """Bulk-load exchange rates for all record dates.

    Returns (nok_context, has_full_coverage). The context is empty — and so
    reports as disabled — when no rates could be loaded.

    Stayed here when the aggregation moved to aggregate.py: it is how the CLI
    fills a NokCtx from a corpus it has just read off this machine's disk. The
    server holds the same rates in its own table and builds its NokCtx from
    those, so it needs the context type and not this.

    *prefetch* is an in-flight request main() started before loading the corpus;
    load_rates joins it, so the API call and the corpus load overlap.
    """
    if not records:
        return NokCtx(mva=mva), False
    dates: set[date] = {record_oslo_date(r) for r in records}
    rates = load_rates(dates, prefetch)
    if not rates:
        return NokCtx(mva=mva), False
    max_rate_date = max(rates)
    # Check coverage: every unique date must resolve via walkback
    missing = 0
    for d in dates:
        rate, _ = get_rate(rates, d, _max_date=max_rate_date)
        if rate is None:
            missing += 1
    return NokCtx(rates, max_rate_date, mva), missing == 0


def _keep(
    rec: UsageRecord,
    *,
    since: datetime | None,
    until: datetime | None,
    project_filter: str | None,
    account_filter: str | None,
    seen_keys: set[str],
    override: "Callable[[str | None, str | None, str], str] | None",
    accounts: "AccountTimeline | None",
) -> bool:
    """Whether this record belongs in the report.

    Three side effects, all deliberate: the override renames *rec*'s project
    and the timeline stamps its account, both before the matching filter sees
    them, and a first-seen dedup key is added to *seen_keys*. Live and
    purged-file records go through this one copy, so a filter added here cannot
    silently miss the older half of the corpus.

    The dedup key is pricing.dedup_identity, shared with the cost readers so
    the two cannot drift on which records are the same message.
    """
    if override:
        rec.project = override(rec.repo, rec.cwd, rec.project)
    if accounts is not None:
        rec.account = accounts.label_at(rec.timestamp)
    if since and rec.timestamp < since:
        return False
    if until and rec.timestamp > until:
        return False
    if project_filter and project_filter.lower() not in rec.project.lower():
        return False
    if account_filter and account_filter.lower() not in rec.account.lower():
        return False
    key = dedup_identity(
        rec.dedup_key, rec.message_id, rec.session_id,
        rec.timestamp.timestamp(), rec.model,
        (rec.tokens.input, rec.tokens.output,
         rec.tokens.cache_create, rec.tokens.cache_read),
    )
    if key is not None:
        if key in seen_keys:
            return False
        seen_keys.add(key)
    return True


def _keep_filters(
    since: datetime | None,
    until: datetime | None,
    project_filter: str | None,
    account_filter: str | None,
    *,
    accounts: "AccountTimeline | None" = None,
) -> dict:
    """The keyword bundle every _keep call in a load shares.

    One per load, because ``seen_keys`` is the run's dedup state: two bundles
    would dedup the live and the purged half of the corpus independently.

    *accounts* lets a caller that has already read the change log — the rollup
    fingerprint does — hand the timeline over instead of paying for a second
    read of it.
    """
    return {
        "since": since, "until": until, "project_filter": project_filter,
        "account_filter": account_filter,
        "seen_keys": set(), "override": _build_override_fn(),
        # One read of the change log for the run; every record is stamped from
        # it, cached and freshly parsed alike.
        "accounts": accounts if accounts is not None
        else AccountTimeline(load_account_events()),
    }


def load_all_records(
    since: datetime | None = None,
    until: datetime | None = None,
    project_filter: str | None = None,
    account_filter: str | None = None,
    *,
    use_rollups: bool = False,
) -> list[UsageRecord]:
    """Load and deduplicate all usage records.

    Uses a SQLite cache keyed by (mtime_ns, size) to avoid re-parsing
    unchanged files.  Deduplication uses a composite key of message_id +
    request_id (matching ccusage).  First occurrence wins.

    *use_rollups* serves the days older than the cutoff from precomputed
    aggregates instead of their records; see _load_with_rollups. Off by
    default, and only ever on for a whole-corpus report — every caller that
    needs record-level detail (a filter, --json, adopt) gets the full stream
    without having to know rollups exist.

    Raises ValueError when *use_rollups* arrives with any of *since*, *until*,
    *project_filter* or *account_filter*: a rollup row is one day of one session
    and has aggregated away what those four select on.
    """
    files = discover_jsonl_files()
    live_paths = {str(p) for p in files}
    _ensure_cache_valid(live_paths)
    if use_rollups:
        if since or until or project_filter or account_filter:
            raise ValueError(
                "rollups aggregate away what a filter selects on; "
                "load the full record stream instead"
            )
        return _load_with_rollups(files, live_paths)
    return _load_full(files, live_paths, since, until, project_filter, account_filter)


def _load_full(
    files: list[Path],
    live_paths: set[str],
    since: datetime | None,
    until: datetime | None,
    project_filter: str | None,
    account_filter: str | None,
    *,
    refreshed: tuple[dict[str, list[UsageRecord]], set[str]] | None = None,
    accounts: "AccountTimeline | None" = None,
) -> list[UsageRecord]:
    """Every record the cache and the live files hold, filtered and deduped.

    *refreshed* is the (fresh, unreadable) pair from a _refresh_changed_files
    the caller already ran. The rollup rebuild path has just statted every
    session log and re-parsed the changed ones, and doing that a second time was
    the bulk of what a rebuild cost. *accounts* is the same deal for the change
    log the rollup fingerprint already read.
    """
    filters = _keep_filters(
        since, until, project_filter, account_filter, accounts=accounts,
    )
    all_records: list[UsageRecord] = []

    if refreshed is None:
        fresh, unreadable = _refresh_changed_files(files, load_ccreport_file_meta())
    else:
        fresh, unreadable = refreshed

    # A date filter goes to SQL rather than to _keep: a report of one day used
    # to build a UsageRecord, a TokenCounts and a datetime for all ~100k cached
    # rows and then drop 99% of them. project/account cannot follow it — both
    # are decided at read time by rules that are not in the table.
    records_by_file = load_ccreport_records_in_range(
        since.timestamp() if since else None,
        until.timestamp() if until else None,
    )

    for path in files:
        key = str(path)
        # Popped before the unreadable check so that whatever is left below is
        # exactly the orphans, and so the raw rows are freed as they are
        # consumed rather than held until the loop ends.
        raw = records_by_file.pop(key, None)
        if key in unreadable:
            continue
        records = fresh.pop(key, None)
        if records is None:
            records = _deserialize_records(raw) if raw else []
        all_records += [r for r in records if _keep(r, **filters)]

    # Records from files purged off disk but still cached: the query above
    # already returned them, so no second query — it covers every cached file,
    # and anything not on disk this run is by definition an orphan.
    orphaned = _deserialize_records(
        [r for fp, recs in records_by_file.items() if fp not in live_paths for r in recs]
    )
    all_records += [r for r in orphaned if _keep(r, **filters)]

    all_records.sort(key=lambda r: r.timestamp)
    # The archive first, as _load_with_rollups puts its rollup groups first:
    # every archived day ends before the oldest record left in the table, since
    # `ccreport archive` only ever folds whole purged files behind its cutoff.
    return _archived_for_load(filters) + all_records


# --- Per-day rollups for the days that can no longer change ---

ROLLUP_WINDOW_DAYS = 14
"""How many days back from local midnight stay on the record path.

Everything older is served from ccreport_rollups. Deliberately the same span as
the monthly report's trailing-day projection: that window starts at exactly
this cutoff, so it reads live records only and never has to make sense of a
day-sized aggregate. Moving one of the two means moving the other.
"""


def _rollup_cutoff() -> datetime:
    """The oldest instant still served from records: local midnight, minus the window.

    Rolls forward once a day, which costs one rebuild per day.
    """
    today = datetime.now().astimezone()
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=ROLLUP_WINDOW_DAYS)


def _pricing_hash() -> str:
    """SHA256 of pricing.py, for the rollup fingerprint only.

    A rollup freezes each record's cost() at build time, and nothing recomputes
    a frozen sum — so a price edit has to invalidate the rollups. _script_hash
    deliberately leaves pricing out for the opposite reason: a record cache
    stores names, not costs, and re-parsing the corpus every time a model is
    added would cost far more than it saves.
    """
    try:
        return hashlib.sha256(Path(pricing.__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


def _rollup_fingerprint(
    cutoff: datetime, orphans: set[str], events: list[dict],
) -> str:
    """Digest of every input a stored rollup row froze an answer about.

    Any mismatch rebuilds, so a part missing here is silently wrong numbers.
    The account log and the override rules are in it because both re-attribute
    history at read time with no re-parse — the very thing a rollup would
    otherwise hide. The orphan set is in it because a file being purged moves
    it to the back of the dedup order, which can hand a duplicated message's
    surviving copy a different project.

    The log goes in whole, tier columns included, so an event that changed only
    a tier rebuilds rollups no attribution moved. Deliberate: over-invalidating
    costs one rebuild on the next run, and narrowing this to the fields that
    happen to matter today is how a later one starts mattering unnoticed.
    """
    parts: list[str] = [
        # The record cache's own contract: a version bump or a naming change
        # re-parses the corpus the rollups were built from.
        f"{CACHE_VERSION}:{_script_hash()}",
        _pricing_hash(),
        # Through the module attribute, which is how project_identity reaches
        # the same table — the rules the fingerprint covers are then the rules
        # the load will actually apply, under a stub as much as in production.
        repr(cache_db.get_project_overrides()),
        repr(events),
        # Days are bucketed in local time, and the FX date in Oslo time; a
        # machine that moves zone re-buckets every day it has ever recorded.
        str(_local_tz()),
        cutoff.strftime("%Y-%m-%d"),
    ]
    parts += [
        f"{path}\0{mtime_ns}\0{size}"
        for path, mtime_ns, size in load_ccreport_file_meta_before(cutoff.timestamp())
    ]
    parts.append("orphans")
    parts += sorted(orphans)
    # A rollup built before an archive run froze totals over records that have
    # since been folded away, and the load that rebuilds it reads them from the
    # archive instead. save_ccreport_archive drops the stored fingerprint too;
    # this is what covers a database restored from a snapshot on either side.
    parts.append(cache_db.archive_stamp())
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode())
        h.update(b"\n")
    return h.hexdigest()


def _build_rollups(
    records: list[UsageRecord], cutoff_ts: float, fingerprint: str,
) -> None:
    """Aggregate the pre-cutoff half of *records* into the rollup table.

    Fed the post-_keep stream of a full load — deduplicated, renamed by the
    override rules, stamped with an account — never a GROUP BY over
    ccreport_records, which would count the duplicate rows _keep drops (more
    than half the table) and would freeze the two attributes that are read-time
    by design.
    """
    rows: dict[tuple, list] = {}
    for rec in records:
        ts = rec.timestamp.timestamp()
        if ts >= cutoff_ts:
            continue
        key = (
            rec.day_key(),
            rec.fx_date().isoformat(),
            rec.session_id, rec.project, rec.model, rec.account,
        )
        t = rec.tokens
        row = rows.get(key)
        if row is None:
            rows[key] = [ts, ts, t.input, t.output, t.cache_create, t.cache_read,
                         rec.cost(), rec.count]
            continue
        row[0] = min(row[0], ts)
        row[1] = max(row[1], ts)
        row[2] += t.input
        row[3] += t.output
        row[4] += t.cache_create
        row[5] += t.cache_read
        row[6] += rec.cost()
        row[7] += rec.count
    save_ccreport_rollups([(*key, *row) for key, row in rows.items()], fingerprint)


def _rollup_records(rows: list[tuple]) -> list[UsageRecord]:
    """Rollup rows as one synthetic record each, oldest group first.

    These never go through _keep: they were deduped, renamed and attributed
    when the rollup was built, and running them through it again would dedup a
    whole day of a session down to one call.

    Ordered by min_ts rather than by the timestamp they carry, so the session
    report picks the same "first" bucket a full load would. The timestamp is
    the group's newest, which is what the session report shows as "last"; both
    fall on the same local day, since the day is part of the key.
    """
    pairs: list[tuple[float, UsageRecord]] = []
    for (_day, oslo_date, sid, project, model, account,
         min_ts, max_ts, tin, tout, tcc, tcr, cost, n) in rows:
        pairs.append((min_ts, UsageRecord(
            message_id="",
            model=model,
            tokens=TokenCounts(input=tin, output=tout,
                               cache_create=tcc, cache_read=tcr),
            timestamp=datetime.fromtimestamp(max_ts, tz=UTC),
            session_id=sid,
            project=project,
            # The frozen sum. cost_usd normally means "the log said so" and is
            # what _serialize_records persists, which is safe here only because
            # a rollup record never reaches the record cache.
            cost_usd=cost,
            account=account,
            count=n,
            oslo_date=date.fromisoformat(oslo_date),
        )))
    pairs.sort(key=lambda pair: pair[0])
    return [rec for _min_ts, rec in pairs]


def _archive_records(
    rows: list[tuple],
    *,
    since: datetime | None,
    until: datetime | None,
    project_filter: str | None,
    account_filter: str | None,
    override: "Callable[[str | None, str | None, str], str] | None",
    accounts: "AccountTimeline | None",
) -> list[UsageRecord]:
    """Archive rows as one synthetic record each, oldest group first.

    Not run through _keep, for the reason _rollup_records is not: these rows
    were deduped when they were folded, and deduping a whole day of a session
    again would leave one call of it. The two read-time attributions do run —
    the override renames the raw project the row stored and the change log
    stamps the account off min_ts — which is what makes `ccreport merge` and
    `ccreport adopt` still reach an archived day.

    The account comes from min_ts rather than from the timestamp the record
    carries, because `ccreport archive` refuses a file whose span holds a change
    log event: both ends of a row therefore resolve to the same account, and
    min_ts is the end that decides which side of an adoption the row is on.
    """
    pairs: list[tuple[float, UsageRecord]] = []
    for (_day, oslo_date, sid, project, model, cwd, repo, _prefix,
         min_ts, max_ts, tin, tout, tcc, tcr, cost, n) in rows:
        rec = UsageRecord(
            message_id="",
            model=model,
            tokens=TokenCounts(input=tin, output=tout,
                               cache_create=tcc, cache_read=tcr),
            timestamp=datetime.fromtimestamp(max_ts, tz=UTC),
            session_id=sid,
            project=project,
            cwd=cwd or None,
            repo=repo or None,
            # The frozen sum, as a rollup record carries it; see _rollup_records.
            cost_usd=cost,
            count=n,
            oslo_date=date.fromisoformat(oslo_date),
        )
        if override:
            rec.project = override(rec.repo, rec.cwd, rec.project)
        if accounts is not None:
            rec.account = accounts.label_at(datetime.fromtimestamp(min_ts, tz=UTC))
        if since and rec.timestamp < since:
            continue
        if until and rec.timestamp > until:
            continue
        if project_filter and project_filter.lower() not in rec.project.lower():
            continue
        if account_filter and account_filter.lower() not in rec.account.lower():
            continue
        pairs.append((min_ts, rec))
    pairs.sort(key=lambda pair: pair[0])
    return [rec for _min_ts, rec in pairs]


def _archived_for_load(filters: dict) -> list[UsageRecord]:
    """The archive, filtered the way *filters* would have filtered its records.

    Takes the same bundle _keep does so a filter added to one is a filter the
    other is handed too; seen_keys is the one key it drops, on purpose.
    """
    return _archive_records(
        load_ccreport_archive(),
        since=filters["since"], until=filters["until"],
        project_filter=filters["project_filter"],
        account_filter=filters["account_filter"],
        override=filters["override"], accounts=filters["accounts"],
    )


def _load_with_rollups(files: list[Path], live_paths: set[str]) -> list[UsageRecord]:
    """The whole corpus, with everything past the cutoff served as rollup rows.

    Returns the same aggregate totals a full load does; what it does not return
    is one record per API call for the old days, which is why only the
    unfiltered report path may ask for it.
    """
    cutoff = _rollup_cutoff()
    cutoff_ts = cutoff.timestamp()

    # Before the fingerprint rather than after: the fingerprint is built from
    # cached file metadata, and an appended or newly discovered file can carry
    # records older than the cutoff. Catching up first means a change shows up
    # on the run that saw it, not on the one after.
    file_meta = load_ccreport_file_meta()
    refreshed = _refresh_changed_files(files, file_meta)
    unreadable = refreshed[1]

    # Cached files no longer on disk. Taken from the pre-refresh metadata,
    # which is complete for the question: anything the refresh added is a file
    # that exists.
    orphans = set(file_meta) - live_paths
    # Read once and used twice: the fingerprint hashes the change log because
    # it re-attributes history at read time, and the load stamps every record
    # from that same log.
    events = load_account_events()
    fingerprint = _rollup_fingerprint(cutoff, orphans, events)
    if read_ccreport_rollup_fingerprint() != fingerprint:
        # Costs this run what the run before it cost — the files are already
        # parsed and saved, so the full load below is a pure cache read, and it
        # is handed the refresh and the timeline rather than redoing both.
        records = _load_full(
            files, live_paths, None, None, None, None,
            refreshed=refreshed, accounts=AccountTimeline(events),
        )
        _build_rollups(records, cutoff_ts, fingerprint)
        return records

    filters = _keep_filters(None, None, None, None, accounts=AccountTimeline(events))
    by_file = load_ccreport_records_since(cutoff_ts)
    recent: list[UsageRecord] = []
    # Live files in the same sorted order as a full load, then the orphans, so
    # a duplicated message's first occurrence — the copy that wins, with its
    # project — is the same one either way.
    for path in files:
        key = str(path)
        # As a full load does. Its pre-cutoff half still comes from the rollup,
        # which a full load would have dropped along with the rest — the run
        # under-reports the file either way, this one by less.
        if key in unreadable:
            continue
        recent += [
            r for r in _deserialize_records(by_file.get(key, []))
            if _keep(r, **filters)
        ]
    for file_path, raw in by_file.items():
        if file_path in live_paths:
            continue
        recent += [r for r in _deserialize_records(raw) if _keep(r, **filters)]

    recent.sort(key=lambda r: r.timestamp)
    # Two already-sorted runs that cannot interleave: every rollup group ends
    # before the cutoff and every record here starts at it.
    return _rollup_records(load_ccreport_rollups()) + recent


# --- Formatting ---

console = Console(soft_wrap=True)
NARROW_WIDTH = 100


def _is_narrow() -> bool:
    return console.width < NARROW_WIDTH


def fmt_tokens(n: int) -> str:
    """Format token count with K/M suffix. Past 100 the decimal is noise."""
    for suffix, size in (("M", 1_000_000), ("K", 1_000)):
        if n >= size:
            scaled = n / size
            # Branch on the rounded value, else 99.96 renders as "100.0K".
            return f"{scaled:.0f}{suffix}" if round(scaled, 1) >= 100 else f"{scaled:.1f}{suffix}"
    return str(n)


def fmt_cost(c: float) -> str:
    """Format cost in USD.

    Cents stop mattering above $10; sub-10-cent amounts keep extra precision so
    small costs don't render as $0.0.
    """
    if round(c, 2) >= 10.0:  # rounded, else $9.996 renders as "$10.00"
        return f"${c:.0f}"
    if c >= 1.0:
        return f"${c:.2f}"
    if c >= 0.1:
        return f"${c:.1f}"
    if c == 0.0:
        return "$0.0"
    return f"${c:.4f}"


def fmt_nok(c: float, estimated: bool = False) -> str:
    """Format cost in NOK incl. MVA. Appends * when rate is estimated."""
    star = "*" if estimated else ""
    if c >= 10.0:
        return f"kr {c:.0f}{star}"
    if c >= 1.0:
        return f"kr {c:.1f}{star}"
    return f"kr {c:.2f}{star}"


def fmt_pct(cost: float, total: float) -> str:
    if total <= 0:
        return ""
    pct = cost / total * 100
    if pct >= 10:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def cost_style(c: float) -> str:
    if c >= 50:
        return "bold red"
    if c >= 10:
        return "yellow"
    if c >= 1:
        return "green"
    return "dim green"


MODELS_MIN_WIDTH = 12
"""Below this a Models column shows nothing but an ellipsis, so drop it."""


def _flex_cell(text: str) -> Text:
    """Build a cell for the Models column, the only column Rich may shrink.

    Rich takes width from wrappable columns first. When every column is no_wrap
    it instead shaves all of them evenly, which is what turned the numbers into
    '14.…'. The cell keeps no_wrap so a shrunk column truncates on one line
    rather than wrapping onto two.
    """
    return Text(text, no_wrap=True, overflow="ellipsis")


def _models_cell(models: dict[str, float]) -> Text:
    """Render a bucket's models, each with its cost, as one truncatable cell."""
    ordered = sorted(models.items(), key=lambda kv: by_cost_desc(*kv))
    return _flex_cell(", ".join(f"{short_model(m)} ({fmt_cost(c)})" for m, c in ordered))


def _column_width(column) -> int:
    """Natural width of a column: its widest cell, header included."""
    widths = [Text.from_markup(str(column.header)).cell_len]
    widths += [
        cell.cell_len if isinstance(cell, Text) else Text.from_markup(str(cell)).cell_len
        for cell in column._cells  # noqa: SLF001 - Rich exposes no public accessor
    ]
    return max(widths)


def _natural_width(table: Table, columns=None) -> int:
    """How wide *table* wants to be: every cell at full width, plus the box.

    *columns* narrows the question to a subset — what the table would take with
    the rest dropped.
    """
    padding = table.padding[1] + table.padding[3]
    cells = sum(_column_width(c) + padding
                for c in (table.columns if columns is None else columns))
    return cells + table._extra_width  # noqa: SLF001


def _fit_columns(table: Table, droppable: Sequence[str]) -> None:
    """Drop *droppable* columns, in that order, until the table fits the console.

    For a table with more columns than a terminal has room for. Rich's own
    answer is to shave every column by a character or two, which turns each of
    them into an ellipsis and loses the whole table rather than one column of
    it. Dropping in a stated order means the report decides what goes.

    Rich keys a column's padding off its position, so the survivors are
    renumbered; a column removed from the middle otherwise leaves the table
    with no last column and an over-padded right edge.
    """
    for header in droppable:
        if _natural_width(table) <= console.width:
            return
        table.columns[:] = [c for c in table.columns if str(c.header) != header]
    for index, column in enumerate(table.columns):
        column._index = index  # noqa: SLF001


def _print_report(table: Table) -> None:
    """Print a report table, dropping Models when the terminal is too narrow.

    Rich empties the wrappable Models column before shaving anything else, but it
    takes it all the way to zero and then shaves the numbers regardless, leaving a
    dead column behind. Removing it first keeps the rest of the table readable.
    """
    if table.columns and str(table.columns[-1].header) == "Models":
        fixed = _natural_width(table, table.columns[:-1])
        if console.width - fixed < MODELS_MIN_WIDTH:
            table.columns.pop()
    console.print()
    console.print(table)
    console.print()


def _make_report_table(
    title: str,
    label_col: str,
    *,
    narrow: bool = False,
    compact: bool = False,
    label_style: str = "white",
    nok: NokCtx,
) -> Table:
    """Create a standard report table with label + token + optional Models columns."""
    table = Table(title=title, title_style="bold", box=box.ROUNDED, expand=False, show_lines=False)
    table.add_column(label_col, style=label_style, no_wrap=True)
    _add_token_columns(table, compact=compact, narrow=narrow, nok=nok)
    if not narrow:
        # The only wrappable column, so Rich takes width from here first.
        table.add_column("Models", style="dim")
    return table


def _add_token_columns(table: Table, *, compact: bool = False, narrow: bool = False, nok: NokCtx) -> None:
    cost_label = "USD" if nok.enabled else "Cost"
    if narrow:
        table.add_column(cost_label, justify="right", no_wrap=True)
        if nok.enabled:
            table.add_column(nok.label, justify="right", style="cyan", no_wrap=True)
        table.add_column("Tokens", justify="right", style="bold", no_wrap=True)
        table.add_column("Calls", justify="right", style="dim", no_wrap=True)
        return
    table.add_column("Input", justify="right", style="cyan", no_wrap=True)
    table.add_column("Output", justify="right", style="cyan", no_wrap=True)
    if not compact:
        table.add_column("Cache W", justify="right", style="blue", no_wrap=True)
        table.add_column("Cache R", justify="right", style="blue", no_wrap=True)
    table.add_column("Total", justify="right", style="bold", no_wrap=True)
    table.add_column(cost_label, justify="right", no_wrap=True)
    if nok.enabled:
        table.add_column(nok.label, justify="right", style="cyan", no_wrap=True)
    table.add_column("%", justify="right", style="dim", no_wrap=True)
    table.add_column("Calls", justify="right", style="dim", no_wrap=True)


def _fmt_cache_read(t: TokenCounts) -> str:
    """Format cache read tokens with hit rate: '9.0M (87%)'."""
    s = fmt_tokens(t.cache_read)
    total_input = t.input + t.cache_create + t.cache_read
    if total_input > 0 and t.cache_read > 0:
        pct = t.cache_read / total_input * 100
        s += f" ({pct:.0f}%)"
    return s


def _token_row(
    b: "AggBucket", total_cost: float = 0.0, *,
    compact: bool = False, narrow: bool = False, nok: NokCtx,
) -> list:
    cost_text = Text(fmt_cost(b.cost), style=cost_style(b.cost))
    if narrow:
        cells = [cost_text]
        if nok.enabled:
            cells.append(Text(fmt_nok(b.cost_nok, b.nok_estimated), style="cyan"))
        cells += [fmt_tokens(b.tokens.total), str(b.count)]
        return cells
    row: list[str | Text] = [
        fmt_tokens(b.tokens.input),
        fmt_tokens(b.tokens.output),
    ]
    if not compact:
        row += [fmt_tokens(b.tokens.cache_create), _fmt_cache_read(b.tokens)]
    row += [
        fmt_tokens(b.tokens.total),
        cost_text,
    ]
    if nok.enabled:
        row.append(Text(fmt_nok(b.cost_nok, b.nok_estimated), style="cyan"))
    row += [
        fmt_pct(b.cost, total_cost),
        str(b.count),
    ]
    return row


# --- Reports ---


def _shown_label(report: ReportRows, limit: int | None) -> str:
    """What a title says it is showing: "top 20 of 340", or just "340"."""
    if limit and report.n_all > limit:
        return f"top {limit} of {report.n_all}"
    return str(report.n_all)


def _summary_row(
    table: Table,
    label: str,
    cost: float,
    *,
    narrow: bool,
    nok: NokCtx,
    nok_cost: float = 0.0,
    nok_estimated: bool = False,
    lead: Sequence = (),
    after: Sequence = ("", ""),
    note: str | Text = "",
    style: str = "dim",
    label_style: str = "dim bold",
) -> None:
    """Append one padded summary row — AVG, PROJECTED, AVERAGE across all.

    The run of empty cells between the label and the cost comes from
    ``len(table.columns)``, so a column added anywhere shifts every summary row
    at once instead of leaving hand-counted padding to re-derive per caller.

    *lead* fills label columns after the first (Session/Project/Date tables);
    *after* the two cells past the money block (%/Calls, or Tokens/Calls when
    narrow); *note* the trailing Models cell, which narrow tables do not have.
    """
    head: list = [Text(label, style=label_style), *lead]
    money: list = [Text(fmt_cost(cost), style=cost_style(cost))]
    if nok.enabled:
        money.append(Text(fmt_nok(nok_cost, nok_estimated), style=f"{style} cyan"))
    tail: list = [] if narrow else [note]
    pad = len(table.columns) - len(head) - len(money) - len(after) - len(tail)
    table.add_row(*head, *[""] * pad, *money, *after, *tail, style=style)


def _add_summary_rows(
    table: Table,
    total_agg: "AggBucket",
    n_buckets: int,
    *,
    narrow: bool,
    compact: bool = False,
    avg_label: str = "",
    nok: NokCtx,
) -> None:
    """Append TOTAL and optional AVG rows to a report table."""
    table.add_section()
    total_row = [Text("TOTAL", style="bold"), *_token_row(total_agg, compact=compact, narrow=narrow, nok=nok)]
    if not narrow:
        total_row.append(_flex_cell(f"{len(total_agg.models)} models"))
    table.add_row(*total_row, style="bold")
    if n_buckets > 1 and (narrow or avg_label):
        _summary_row(
            table, "AVG" if narrow else "AVERAGE", total_agg.cost / n_buckets,
            narrow=narrow, nok=nok,
            nok_cost=total_agg.cost_nok / n_buckets if nok.enabled else 0.0,
            nok_estimated=total_agg.nok_estimated,
            note=_flex_cell(avg_label),
        )


def report_daily(records: list[UsageRecord], breakdown: bool = False, *, nok: NokCtx) -> None:
    render_daily(aggregate.daily_rows(records, nok, breakdown=breakdown), nok=nok)


def render_daily(report: ReportRows, *, nok: NokCtx) -> None:
    """Draw the daily table from rows that are already aggregated.

    Split from report_daily so `ccreport --server` can render rows the server
    folded: a merged report then goes through the same builder as a local one
    and looks like one.
    """
    narrow = _is_narrow()

    table = _make_report_table(f"Daily Usage ({report.n_all} days)", "Date", narrow=narrow, nok=nok)

    total_cost = report.total.cost
    for row in report.rows:
        cells = [row.key, *_token_row(row.agg, total_cost, narrow=narrow, nok=nok)]
        if not narrow:
            cells.append(_models_cell(row.agg.models))
        table.add_row(*cells)

        for sub in row.breakdown:
            brow = [
                f"  [dim]{short_model(sub.key)}[/dim]",
                *_token_row(sub.agg, total_cost, narrow=narrow, nok=nok),
            ]
            if not narrow:
                brow.append("")
            table.add_row(*brow)

    _add_summary_rows(table, report.total, report.n_all, narrow=narrow, avg_label="per day", nok=nok)

    _print_report(table)


def report_monthly(records: list[UsageRecord], *, nok: NokCtx) -> None:
    report = aggregate.monthly_rows(records, nok)
    render_monthly(report, aggregate.month_projection(records, report, nok), nok=nok)


def render_monthly(
    report: ReportRows, projection: "aggregate.MonthProjection | None", *, nok: NokCtx,
) -> None:
    """Draw the monthly table, projection block included.

    The projection arrives separately because it needs the record stream, not
    the rows: its trailing figure averages a window the monthly buckets have
    already aggregated away.
    """
    narrow = _is_narrow()

    table = _make_report_table(f"Monthly Usage ({report.n_all} months)", "Month", narrow=narrow, nok=nok)

    total_cost = report.total.cost
    for row in report.rows:
        cells = [row.key, *_token_row(row.agg, total_cost, narrow=narrow, nok=nok)]
        if not narrow:
            cells.append(_models_cell(row.agg.models))
        table.add_row(*cells)

    _add_summary_rows(table, report.total, report.n_all, narrow=narrow, avg_label="per month", nok=nok)

    if projection:
        table.add_section()
        _add_projection_rows(table, projection, narrow=narrow, nok=nok)

    _print_report(table)


def _add_projection_rows(
    table: Table, proj: "aggregate.MonthProjection", *, narrow: bool, nok: NokCtx,
) -> None:
    """Append the monthly report's two PROJECTED lines.

    Both are absent on the last day of the month, where the section separator
    above is all that is left of the block.
    """
    if proj.month_to_date is None:
        return
    _summary_row(
        table, "PROJ" if narrow else "PROJECTED", proj.month_to_date.cost,
        narrow=narrow, nok=nok,
        nok_cost=proj.month_to_date.cost_nok, nok_estimated=proj.month_to_date.nok_estimated,
        note=f"({proj.days_elapsed}/{proj.days_in_month} days in {proj.month_name})",
        label_style="dim bold italic",
    )
    if proj.trailing is not None:
        _summary_row(
            table, f"PROJ {proj.window_days}d" if narrow else "PROJECTED", proj.trailing.cost,
            narrow=narrow, nok=nok,
            nok_cost=proj.trailing.cost_nok, nok_estimated=proj.trailing.nok_estimated,
            note=_flex_cell(f"Last {proj.window_days} days avg"),
            label_style="dim bold italic",
        )


def report_project(records: list[UsageRecord], limit: int | None = 20, *, nok: NokCtx) -> None:
    render_project(aggregate.project_rows(records, nok, limit=limit), limit, nok=nok)


def render_project(report: ReportRows, limit: int | None = 20, *, nok: NokCtx) -> None:
    """Draw the project table. *limit* only decides what the title claims."""
    narrow = _is_narrow()

    table = _make_report_table(
        f"Projects ({_shown_label(report, limit)})", "Project",
        narrow=narrow, compact=True, label_style="magenta", nok=nok,
    )

    total_cost = report.total.cost
    for row in report.rows:
        cells = [row.key, *_token_row(row.agg, total_cost, compact=True, narrow=narrow, nok=nok)]
        if not narrow:
            cells.append(_models_cell(row.agg.models))
        table.add_row(*cells)

    _add_summary_rows(table, report.total, len(report.rows), narrow=narrow, compact=True,
                      avg_label=f"per project (top {len(report.rows)})", nok=nok)
    # Average across ALL projects
    all_n = report.n_all
    all_any_est = report.all_total.nok_estimated if nok.enabled else False
    if all_n > 1:
        _summary_row(
            table, "AVG" if narrow else "AVERAGE", report.all_total.cost / all_n,
            narrow=narrow, nok=nok,
            nok_cost=report.all_total.cost_nok / all_n if nok.enabled else 0.0,
            nok_estimated=all_any_est,
            after=(f"all {all_n}", "") if narrow else ("", ""),
            note=_flex_cell(f"per project (all {all_n})"),
        )

    _print_report(table)


def report_account(records: list[UsageRecord], *, nok: NokCtx) -> None:
    """Print per-account usage report.

    No --limit knob, unlike the project report: an account is a login, so a
    machine has two or three and there is nothing to cut off.
    """
    render_account(aggregate.account_rows(records, nok), nok=nok)


def render_account(report: ReportRows, *, nok: NokCtx) -> None:
    """Draw the account table from rows that are already aggregated."""
    narrow = _is_narrow()

    table = _make_report_table(
        f"Accounts ({report.n_all})", "Account",
        narrow=narrow, compact=True, label_style="green", nok=nok,
    )

    total_cost = report.total.cost
    for row in report.rows:
        cells = [row.key, *_token_row(row.agg, total_cost, compact=True, narrow=narrow, nok=nok)]
        if not narrow:
            cells.append(_models_cell(row.agg.models))
        table.add_row(*cells)

    _add_summary_rows(table, report.total, report.n_all, narrow=narrow,
                      compact=True, avg_label="per account", nok=nok)

    _print_report(table)


def report_session(records: list[UsageRecord], limit: int | None = 20, *, nok: NokCtx) -> None:
    render_session(aggregate.session_rows(records, nok, limit=limit), limit, nok=nok)


def render_session(report: ReportRows, limit: int | None = 20, *, nok: NokCtx) -> None:
    """Draw the session table. *limit* only decides what the title claims."""
    narrow = _is_narrow()
    shown = _shown_label(report, limit)

    table = Table(
        title=f"Sessions ({shown})", title_style="bold", box=box.ROUNDED,
        expand=False, show_lines=False,
    )
    if narrow:
        table.add_column("Project", style="magenta", no_wrap=True)
        table.add_column("Date", style="white", no_wrap=True)
        _add_token_columns(table, narrow=True, nok=nok)
    else:
        table.add_column("Session", style="dim", no_wrap=True)
        table.add_column("Project", style="magenta", no_wrap=True)
        table.add_column("Date", style="white", no_wrap=True)
        # Same token columns as every other wide report, minus the cache pair.
        _add_token_columns(table, compact=True, nok=nok)
        table.add_column("Models", style="dim")

    total_cost = report.total.cost
    total_agg = report.total
    for row in report.rows:
        sid, b, last = row.key, row.agg, row.last
        assert last is not None  # noqa: S101 - session_rows sets it for every row
        cost_text = Text(fmt_cost(b.cost), style=cost_style(b.cost))
        if narrow:
            cells = [
                row.project,
                last.astimezone().strftime("%m-%d %H:%M"),
                cost_text,
            ]
            if nok.enabled:
                cells.append(Text(fmt_nok(b.cost_nok, b.nok_estimated), style="cyan"))
            cells += [fmt_tokens(b.tokens.total), str(b.count)]
            table.add_row(*cells)
        else:
            short_sid = sid[-8:] if len(sid) > 8 else sid
            models_str = _models_cell(b.models)
            cells = [
                short_sid,
                row.project,
                last.astimezone().strftime("%Y-%m-%d %H:%M"),
                fmt_tokens(b.tokens.input),
                fmt_tokens(b.tokens.output),
                fmt_tokens(b.tokens.total),
                cost_text,
            ]
            if nok.enabled:
                cells.append(Text(fmt_nok(b.cost_nok, b.nok_estimated), style="cyan"))
            cells += [
                fmt_pct(b.cost, total_cost),
                str(b.count),
                models_str,
            ]
            table.add_row(*cells)

    table.add_section()
    total_cost_text = Text(fmt_cost(total_agg.cost), style=cost_style(total_agg.cost))
    if narrow:
        cells = [
            Text("TOTAL", style="bold"),
            f"({shown})",
            total_cost_text,
        ]
        if nok.enabled:
            cells.append(Text(fmt_nok(total_agg.cost_nok, total_agg.nok_estimated), style="bold cyan"))
        cells += [fmt_tokens(total_agg.tokens.total), str(total_agg.count)]
        table.add_row(*cells, style="bold")
    else:
        cells = [
            Text("TOTAL", style="bold"),
            "",
            f"({shown})",
            fmt_tokens(total_agg.tokens.input),
            fmt_tokens(total_agg.tokens.output),
            fmt_tokens(total_agg.tokens.total),
            total_cost_text,
        ]
        if nok.enabled:
            cells.append(Text(fmt_nok(total_agg.cost_nok, total_agg.nok_estimated), style="bold cyan"))
        cells += ["", str(total_agg.count), ""]
        table.add_row(*cells, style="bold")
    n = len(report.rows)
    if n > 1:
        _summary_row(
            table, "AVG" if narrow else "AVERAGE", total_agg.cost / n,
            narrow=narrow, nok=nok,
            nok_cost=total_agg.cost_nok / n if nok.enabled else 0.0,
            nok_estimated=total_agg.nok_estimated,
            lead=("",), note=_flex_cell(f"per session (top {n})"),
        )
    # Average across ALL sessions
    all_n = report.n_all
    all_any_est = report.all_total.nok_estimated if nok.enabled else False
    if all_n > 1:
        _summary_row(
            table, "AVG" if narrow else "AVERAGE", report.all_total.cost / all_n,
            narrow=narrow, nok=nok,
            nok_cost=report.all_total.cost_nok / all_n if nok.enabled else 0.0,
            nok_estimated=all_any_est,
            lead=(f"all {all_n}",) if narrow else ("",),
            note=_flex_cell(f"per session (all {all_n})"),
        )

    _print_report(table)


def _json_entry(rec: UsageRecord, nok: NokCtx) -> dict[str, Any]:
    """One record as the object --json prints for it."""
    cost = record_cost(rec)
    entry: dict[str, Any] = {
        "message_id": rec.message_id,
        "model": rec.model,
        "timestamp": rec.timestamp.isoformat(),
        "session_id": rec.session_id,
        "project": rec.project,
        "account": rec.account,
        "input_tokens": rec.tokens.input,
        "output_tokens": rec.tokens.output,
        "cache_creation_tokens": rec.tokens.cache_create,
        "cache_read_tokens": rec.tokens.cache_read,
        "total_tokens": rec.tokens.total,
        "cost_usd": round(cost, 6),
    }
    if nok.enabled:
        amount, estimated = record_cost_nok(rec, cost, nok)
        if amount is not None:
            entry["cost_nok"] = round(amount, 4)
            if estimated:
                entry["cost_nok_estimated"] = True
    return entry


def report_json(records: list[UsageRecord], *, nok: NokCtx) -> None:
    """Output all records as JSON for programmatic use.

    Emitted one record at a time. Collecting the entries into a list and
    handing that to json.dumps held a 12-key dict per record alongside the
    UsageRecord it came from, and then the whole serialized document as a
    single string alongside both — three copies of the whole corpus, on the
    one code path that never gets to use the rollups.
    Byte-for-byte what dumps(list, indent=2) produced: the array's own newlines
    here, each entry's body shifted in under it.
    """
    out = sys.stdout
    out.write("[")
    for i, rec in enumerate(records):
        out.write(",\n" if i else "\n")
        out.write("  " + json.dumps(_json_entry(rec, nok), indent=2).replace("\n", "\n  "))
    out.write("\n]\n" if records else "]\n")


_REMOTE_KINDS = {
    "daily": "day", "monthly": "month",
    "project": "project", "session": "session", "account": "account",
}
"""Subcommand name → the report kind the server answers under."""


def _remote_filters(args) -> dict:
    """The query a merged report is asked with, from the flags already parsed."""
    return {
        "since": getattr(args, "since", None),
        "until": getattr(args, "until", None),
        "project": getattr(args, "project", None),
        "account": getattr(args, "account", None),
        "machine": getattr(args, "machine", None),
    }


def _render_remote_report(payload: dict, kind: str, limit: int | None) -> None:
    """Draw one server-computed report through the local builders."""
    report = aggregate.rows_from_json(payload)
    nok = aggregate.display_nok(
        enabled=payload.get("nok", {}).get("enabled", False),
        mva=payload.get("nok", {}).get("label") != "NOK",
    )
    if kind == "day":
        render_daily(report, nok=nok)
    elif kind == "month":
        render_monthly(
            report, aggregate.month_projection_from_json(payload.get("projection")), nok=nok,
        )
    elif kind == "project":
        render_project(report, limit, nok=nok)
    elif kind == "session":
        render_session(report, limit, nok=nok)
    else:
        render_account(report, nok=nok)


def cmd_server_report(args) -> None:
    """Render `ccreport --server URL <report>` and nothing from the local cache.

    An unreachable server exits non-zero after printing the URL it tried. It
    deliberately does not fall back: a merged report and a single-machine
    report differ by exactly the thing being asked for, so the quiet answer
    would be the wrong one.
    """
    from ccreport.remote import RemoteError, fetch_report

    kinds = ([_REMOTE_KINDS[args.command]] if args.command in _REMOTE_KINDS
             else ["day", "month", "project", "session", "account"])
    limit = getattr(args, "limit", None) or None
    filters = _remote_filters(args)
    try:
        payloads = [
            fetch_report(
                args.server, kind, **filters,
                limit=limit if kind in ("project", "session") else None,
                breakdown=getattr(args, "breakdown", False) or getattr(args, "models", False),
            )
            for kind in kinds
        ]
    except RemoteError as exc:
        print(f"ccreport: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(payloads if len(payloads) > 1 else payloads[0], indent=2))
        return
    for kind, payload in zip(kinds, payloads, strict=True):
        _render_remote_report(payload, kind, limit)


def _forecast_row(label: str, projection) -> str:
    """One horizon as a line: what it ends at, and against what.

    Display only. An account with no ceiling shows the projection alone, which
    is the whole answer for someone who never set one.
    """
    body = f"  {label:<14} {fmt_cost(projection.projected)}"
    body += f" projected, {fmt_cost(projection.spent)} so far"
    body += f" ({projection.elapsed:.0f}/{projection.total:.0f} days)"
    if projection.ceiling:
        share = (projection.share or 0) * 100
        body += f" — {share:.0f}% of {fmt_cost(projection.ceiling)}"
    return body


def cmd_budget(args) -> None:
    """Set, clear or list the per-account spend ceilings, with the projections.

    Personal and work are separate money, so the ceiling is per account rather
    than per machine. Nothing here notifies or changes colour: the number is
    what was asked for.
    """
    from ccreport import forecast

    now_local = datetime.now().astimezone()
    if getattr(args, "budget_command", None) == "set":
        cache_db.save_budget(args.account, args.amount, args.renewal_day, time.time())
        console.print(f"Budget for [bold]{args.account}[/bold] saved.")
        return
    if getattr(args, "budget_command", None) == "clear":
        if not cache_db.clear_budget(args.account):
            print(f"ccreport: no budget set for {args.account!r}.", file=sys.stderr)
            sys.exit(1)
        console.print(f"Budget for [bold]{args.account}[/bold] cleared.")
        return

    budgets = cache_db.load_budgets()
    # Two months back, so the billing period the renewal day anchors is whole
    # however late in the month it falls. Through load_all_records like every
    # report, so the dedup, the merge rules and the account stamping are the
    # ones the tables already agree on.
    by_account = forecast.daily_costs(
        load_all_records(since=now_local - timedelta(days=70)),
    )
    if not by_account:
        console.print("No records in the last 70 days to project from.")
        return
    for account in sorted(by_account, key=lambda a: -sum(by_account[a].values())):
        ceiling, renewal_day = budgets.get(account, (None, None))
        console.print(f"[bold green]{account}[/bold green]")
        costs = by_account[account]
        month = forecast.month_forecast(costs, now_local, ceiling)
        console.print(_forecast_row("calendar month", month) if month
                      else "  calendar month  too few active days to project")
        if renewal_day:
            billing = forecast.billing_forecast(costs, now_local, renewal_day, ceiling)
            console.print(_forecast_row(f"billing (d{renewal_day})", billing) if billing
                          else f"  billing (d{renewal_day})  too few active days to project")
        for line in _window_forecast_lines():
            console.print(line)


def _window_forecast_lines() -> list[str]:
    """The session and week windows as projected cost, from the usage row.

    Read from the cached usage row rather than recomputed: the status line has
    already priced both windows against the bounds it tracks, and pricing them
    a second time here could only disagree with the line on screen.
    """
    from ccreport import forecast
    from ccreport.pricing import SESSION_WINDOW_S, WEEK_WINDOW_S, window_start_epoch

    row = cache_db.read_usage_cache() or {}
    now = time.time()
    lines = []
    for name, cost_key, reset_key, span in (
        ("session window", "session_window_cost", "session_reset", SESSION_WINDOW_S),
        ("week window", "week_cost", "week_reset", WEEK_WINDOW_S),
    ):
        start = window_start_epoch(str(row.get(reset_key) or ""), span, now)
        if start is None:
            continue
        projection = forecast.window_forecast(
            name, float(row.get(cost_key) or 0.0), start, span, now,
        )
        if projection:
            lines.append(
                f"  {name:<14} {fmt_cost(projection.projected)} projected, "
                f"{fmt_cost(projection.spent)} so far",
            )
    return lines


def _resolved_projects(names: Sequence[str]) -> list[str]:
    """Project names as this machine's merge rules group them.

    An alias in --opt-in-repos has to match the name a record actually carries
    after `ccreport merge` has had its say, or a project would be opted in
    under a name no record ever uses and quietly stay redacted.
    """
    override = _build_override_fn()
    return sorted({override(None, None, name.strip()) if override else name.strip()
                   for name in names if name.strip()})


def _config_dir_is_shared(path: Path) -> bool:
    """Whether anyone but the owner can write the directory holding the token."""
    parent = path.parent
    if not parent.exists():
        return False
    return bool(parent.stat().st_mode & 0o022)


def cmd_server_connect(args) -> None:
    """Write this machine's entry for one server, after proving the token works.

    The token is checked against /v1/health first, so a mistyped one fails here
    rather than silently at a background push half an hour later.
    """
    from ccreport import push
    from ccreport.remote import RemoteError, fetch_health

    path = Path(args.config or push.CONFIG_PATH)
    if _config_dir_is_shared(path):
        print(f"ccreport: {path.parent} is group- or world-writable; "
              f"chmod 700 it before storing a token there.", file=sys.stderr)
        sys.exit(1)
    try:
        health = fetch_health(args.url, args.token)
    except RemoteError as exc:
        print(f"ccreport: {exc}", file=sys.stderr)
        sys.exit(1)
    # At setup, not at the first background push half an hour later — the same
    # reason the token is checked here at all. Only the direction that loses
    # data stops: a server ahead of this machine reads everything it sends.
    theirs = health.get("protocol", protocol.PRE_VERSIONING)
    if theirs < protocol.PROTOCOL_VERSION:
        print(f"ccreport: {args.url} — {protocol.describe(theirs)}.\n"
              f"          Nothing was written to {path}.", file=sys.stderr)
        sys.exit(1)

    existing = push.read_raw(path).get(args.url, {})
    fields: dict[str, Any] = {
        "token": args.token,
        "label": health.get("label") or os.uname().nodename,
        "machine_id": health.get("machine_id") or "",
    }
    if args.opt_in_repos is not None:
        fields["restricted"] = True
        fields["allow"] = _resolved_projects(args.opt_in_repos.split(","))
        # Generated once and kept: regenerating it would re-pseudonymize every
        # project already on the server, so the old rows would never merge with
        # the new ones.
        fields["salt"] = existing.get("salt") or push.new_salt()
    if args.exclude_repos is not None:
        fields["exclude"] = _resolved_projects(args.exclude_repos.split(","))
    if args.only_on_network is not None:
        fields["networks"] = [
            cidr.strip() for cidr in args.only_on_network.split(",") if cidr.strip()
        ]
    if args.interval_minutes is not None:
        if args.interval_minutes < 1:
            print("ccreport: --interval-minutes takes a positive number of minutes.",
                  file=sys.stderr)
            sys.exit(1)
        fields["interval_minutes"] = args.interval_minutes
    push.write_server(path, args.url, fields)
    console.print(
        f"Connected to [bold]{args.url}[/bold] as "
        f"[bold]{fields['label']}[/bold] ({fields['machine_id']}).",
    )
    console.print(f"Wrote {path} (mode 0600).")
    if fields.get("restricted"):
        allowed = ", ".join(fields["allow"]) or "nothing"
        console.print(f"Restricted: only {allowed} will be identified by name.")
    if "exclude" in fields:
        hidden = ", ".join(fields["exclude"])
        console.print(f"Excluded: {hidden} will be redacted." if hidden
                      else "Excluded: nothing; every project keeps its name.")
    if "interval_minutes" in fields:
        console.print(f"Pushing every {fields['interval_minutes']} min.")


def cmd_server_disconnect(args) -> None:
    """Stop pushing to one server, and clear every local row keyed on it.

    The server keeps everything it holds. What goes is this machine's side: the
    entry in push.toml, the file watermark, the per-server meta keys, and the
    pulled cost tables — those last because `-A` and the status line's merged
    windows would otherwise go on adding a server nobody pushes to.
    """
    from ccreport import push

    path = Path(getattr(args, "config", None) or push.CONFIG_PATH)
    entries = push.read_raw(path)
    if not entries:
        print(f"No server in {path} — nothing to disconnect.", file=sys.stderr)
        sys.exit(1)
    url = args.url or (next(iter(entries)) if len(entries) == 1 else None)
    if url is None:
        print(f"ccreport: name the server — {path} has {', '.join(sorted(entries))}.",
              file=sys.stderr)
        sys.exit(1)
    if url not in entries:
        print(f"ccreport: {url} is not in {path}.", file=sys.stderr)
        sys.exit(1)

    counts = cache_db.count_server_rows(url)
    pulled = counts["remote_window_costs"] + counts["remote_day_costs"]
    console.print(f"Disconnect [bold]{url}[/bold]:")
    console.print(f"  remove its entry from {path}")
    console.print(f"  forget {counts['push_state']} acknowledged file(s) and "
                  f"{counts['meta']} stored key(s) of push history")
    if pulled:
        console.print(f"  drop {pulled} pulled cost row(s) — [yellow]-A and the status "
                      "line stop counting that server's machines[/yellow]")
    console.print("  [dim]the server keeps its records; revoking the token or "
                  "deleting the machine is the web UI's job[/dim]")
    console.print("  [yellow]the token's plaintext is nowhere else — reconnecting "
                  "needs one minted afresh in the web UI[/yellow]")
    if not args.yes and not _confirm("Proceed?"):
        print("Aborted.")
        return

    push.remove_server(path, url)
    gone = cache_db.forget_server(url)
    console.print(f"Disconnected. Removed {gone['push_state']} watermark row(s), "
                  f"{gone['remote_window_costs'] + gone['remote_day_costs']} "
                  f"pulled cost row(s) and {gone['meta']} stored key(s).")


def _split_allow_targets(targets: Sequence[str], entries: dict, path: Path) -> tuple[str, list[str]]:
    """Read a server URL and the project names out of one variadic argument list.

    A leading URL is optional, so a name that push.toml does not carry is a
    project. A URL push.toml does not carry is not: it stays the error it was,
    because taking it for a project name would silently allow a project nobody
    has.
    """
    first = targets[0]
    if first in entries:
        return first, list(targets[1:])
    if first.startswith(("http://", "https://")):
        print(f"ccreport: {first} is not in {path}.", file=sys.stderr)
        sys.exit(1)
    if len(entries) == 1:
        return next(iter(entries)), list(targets)
    if entries:
        print(f"ccreport: name the server — {path} has {', '.join(sorted(entries))}.",
              file=sys.stderr)
    else:
        print(f"ccreport: no server in {path} — run `ccreport server connect` first.",
              file=sys.stderr)
    sys.exit(1)


def cmd_server_allow(args) -> None:
    """Add or remove projects from a server's allow list, and force the re-push.

    The re-push is not optional: the files that named a project are closed logs
    that will never change again, so nothing else would take the name back off
    the server.
    """
    from ccreport import push

    path = Path(args.config or push.CONFIG_PATH)
    entries = push.read_raw(path)
    url, projects = _split_allow_targets(args.targets, entries, path)
    if not projects:
        print(f"ccreport: name a project to {args.command} on {url}.", file=sys.stderr)
        sys.exit(1)
    current = list(entries[url].get("allow") or ())
    resolved = _resolved_projects(projects)
    if args.command == "allow":
        updated = sorted(set(current) | set(resolved))
    else:
        updated = sorted(set(current) - set(resolved))
    push.write_server(path, url, {"allow": updated})
    cache_db.clear_push_state(url)
    console.print(f"{url}: now identifying {', '.join(updated) or 'nothing'}.")
    console.print("The watermark was cleared; the next push re-sends everything.")


def cmd_server_exclude(args) -> None:
    """Add or remove projects from a server's exclude list.

    The watermark is left alone, unlike `allow`, which clears it. The next push
    reads the moved names out of the policy digest and re-offers the files that
    resolve to them, which is every file whose bytes this changes — see
    push.repush_scope. A closed log still never changes on its own, so the
    re-push is no less obligatory here; it is only narrower.
    """
    from ccreport import push

    path = Path(args.config or push.CONFIG_PATH)
    entries = push.read_raw(path)
    url, projects = _split_allow_targets(args.targets, entries, path)
    if not projects:
        print(f"ccreport: name a project to {args.command} on {url}.", file=sys.stderr)
        sys.exit(1)
    current = list(entries[url].get("exclude") or ())
    resolved = _resolved_projects(projects)
    if args.command == "exclude":
        updated = sorted(set(current) | set(resolved))
    else:
        updated = sorted(set(current) - set(resolved))
    push.write_server(path, url, {"exclude": updated})
    console.print(f"{url}: now redacting {', '.join(updated) or 'nothing'}.")
    console.print("The next push re-sends the files naming what moved.")


def cmd_server_status(args) -> None:
    """Print what each configured server knows this machine as, and under what policy.

    A bare `ccreport server` lands here too, and it parsed no subparser, so
    --config is absent from that namespace rather than None.
    """
    from ccreport import push
    from ccreport.remote import RemoteError, fetch_health

    path = Path(getattr(args, "config", None) or push.CONFIG_PATH)
    servers = push.load_config(path)
    if not servers:
        console.print(f"No {path} — run `ccreport server connect <url> --token ...` first.")
        return
    for server in servers:
        console.print(f"[bold]{server.url}[/bold]")
        drift = None
        try:
            health = fetch_health(server.url, server.token)
            known_as = f"{health.get('label')} ({health.get('machine_id')})"
            holding = f"{health.get('records', 0)} records"
            drift = protocol.describe(
                health.get("protocol", protocol.PRE_VERSIONING),
            )
        except RemoteError as exc:
            known_as, holding = "unreachable", str(exc)
        console.print(f"  known as     {known_as}")
        console.print(f"  holding      {holding}")
        # None means the health call never landed, which is not the same claim
        # as "agreed": nothing was compared.
        agreement = "not compared" if drift is None else (drift or "agreed")
        console.print(f"  protocol     {protocol.PROTOCOL_VERSION} on this machine")
        console.print("               "
                      + (f"[yellow]{agreement}[/yellow]" if drift else agreement))
        console.print(f"  restricted   {'yes' if server.restricted else 'no'}")
        if server.restricted:
            console.print(f"  identifying  {', '.join(server.allow) or 'nothing'}")
        if server.exclude:
            console.print(f"  redacting    {', '.join(server.exclude)}")
        console.print(f"  network gate {', '.join(server.networks) or 'none'}")
        if server.networks and not push.on_allowed_network(server.networks):
            console.print("               [yellow]off-network: pushes are held[/yellow]")
        console.print(f"  interval     {server.interval_s // 60} min")
        attempt, failures, stopped = cache_db.read_push_attempt(server.url)
        success, reason = cache_db.read_push_outcome(server.url)
        console.print(f"  last push    {_fmt_epoch(success) if success else 'never'}")
        if stopped:
            # The stored reason, not a fixed sentence: a refused token and a
            # protocol the server cannot read are both terminal, and which one
            # happened is the whole of what a person needs here.
            console.print(f"  last attempt [red]stopped: "
                          f"{reason or 'the token was refused'}[/red]")
        elif failures and attempt:
            # The failure and its reason, not a count beside the attempt stamp:
            # the stamp moves whatever happened, so on its own it reads as a
            # push that went through.
            console.print(f"  last attempt [red]failed[/red] {_fmt_epoch(attempt)}, "
                          f"{failures} in a row")
            if reason:
                console.print(f"               [red]{reason}[/red]")


def cmd_push(args) -> None:
    """Send this machine's records to every configured server, and say what happened.

    Manual, so it ignores the interval the detached spawn respects — someone
    who typed this is watching, and waiting out a backoff they cannot see is
    the one thing that would make the command look broken.
    """
    from ccreport import push

    config = Path(args.config) if getattr(args, "config", None) else None
    if not push.configured(config):
        print(f"No {config or push.CONFIG_PATH} — "
              "run `ccreport server connect <url> --token ...` first.", file=sys.stderr)
        sys.exit(1)

    results = push.run_once(
        full=getattr(args, "full", False), only=args.server, config_path=config,
        force=True, pull=getattr(args, "server_command", None) == "sync",
    )
    if not results:
        print(f"No server matched {args.server!r}.", file=sys.stderr)
        sys.exit(1)
    failed = False
    for result in results:
        if result.blocked:
            # Named, not silent: a gated machine that pushes nothing all week
            # looks broken unless it says which network it was waiting for.
            console.print(
                f"[bold]{result.server}[/bold]: skipped, this machine holds no address in "
                f"{', '.join(result.blocked_by)}",
            )
            continue
        # Samples only when there were some: a machine whose windows have not
        # moved since the last push has nothing to report there, and a "0
        # samples" on every line reads as a broken feature.
        samples = f", {result.samples} samples" if result.samples else ""
        pulled = f", {result.pulled} machines pulled" if result.pulled else ""
        declared = f", {result.declared} plan changes" if result.declared else ""
        console.print(
            f"[bold]{result.server}[/bold]: {len(result.accepted)} sent, "
            f"{len(result.skipped)} unchanged, {len(result.rejected)} rejected "
            f"({result.records} records{samples}{pulled}{declared})",
        )
        for path, detail in result.rejected:
            failed = True
            console.print(f"  [red]rejected[/red] {path or '(the request)'}: {detail}")
    if failed:
        sys.exit(1)


REMOTE_MODEL = "<remote>"
"""The model a pulled day row carries. It has none: the server folded the hour
and the model away to answer at day grain. A `<...>` pseudo-model is what every
report already leaves out of the Models column, and its cost travels stated so
nothing prices it from tokens."""


class RemoteSpend(NamedTuple):
    """What the pulled tables hold for one report's filters."""

    records: list[UsageRecord]
    machines: set[str]
    """Which machines contributed a record that survived the filters.

    Counted here rather than off the records: a record's project is the name its
    machine pushed, and two machines working on one repo push the same one. The
    only row that carries a machine in its name is a redacted one, so counting
    project prefixes said 48 where one machine had 48 projects."""


def _remote_records(
    since: datetime | None, until: datetime | None,
    project_filter: str | None, account_filter: str | None,
) -> RemoteSpend:
    """Pulled remote day rows as synthetic records, for `-A`.

    Scoped to the account signed in right now, on the read side as much as on
    the write side: rows for a previous login stay in the table and a switch
    back finds them, but nothing selects them meanwhile.

    Never deduped against the local corpus and never needing to be — the server
    excluded by dedup identity everything this machine pushed, so what these
    rows carry is spend that is genuinely elsewhere.
    """
    account = cache_db.read_latest_account() or {}
    uuid = account.get("account_uuid")
    if not uuid:
        return RemoteSpend([], set())
    label = account.get("email") or uuid
    labels = {
        row["machine_id"]: row["label"]
        for row in cache_db.load_remote_window_costs(uuid)
    }
    out: list[UsageRecord] = []
    machines: set[str] = set()
    for row in cache_db.load_remote_day_costs(uuid):
        day = date.fromisoformat(row["day"])
        # Local noon, so the day this buckets under is the machine's own and the
        # Oslo date it converts at straddles as little as a day rows can.
        when = datetime(day.year, day.month, day.day, 12, tzinfo=UTC).astimezone()
        machine = labels.get(row["machine_id"], row["machine_id"])
        project = row["project"] or f"{machine}/aggregated"
        rec = UsageRecord(
            message_id="",
            model=REMOTE_MODEL,
            tokens=TokenCounts(
                input=row["input_tokens"], output=row["output_tokens"],
                cache_create=row["cache_create"], cache_read=row["cache_read"],
            ),
            timestamp=when,
            session_id="",
            project=project,
            cost_usd=row["cost"],
            account=label,
            count=row["n"],
        )
        rec._day = row["day"]  # noqa: SLF001 - the stored day is the machine's own
        if since and rec.timestamp < since:
            continue
        if until and rec.timestamp > until:
            continue
        if project_filter and project_filter.lower() not in rec.project.lower():
            continue
        if account_filter and account_filter.lower() not in rec.account.lower():
            continue
        out.append(rec)
        machines.add(machine)
    return RemoteSpend(out, machines)


def _remote_note(remote: RemoteSpend) -> str:
    """One line naming the spend this table leaves out, or "".

    Printed under a table that `-A` could have merged, so the figure and the
    figure `-A` then prints come from the same rows and cannot disagree.
    """
    if not remote.records:
        return ""
    total = sum(rec.cost() for rec in remote.records)
    n = len(remote.machines)
    machine = "machine" if n == 1 else "machines"
    return (
        f"[dim]Not shown: {fmt_cost(total)} spent on this account from "
        f"{n} other {machine}. `ccreport -A` merges it.[/dim]"
    )


def _session_note(merge_all: bool, remote: RemoteSpend) -> None:
    """Why the session table is the one -A cannot merge.

    A restricted machine strips the session id before it pushes, and a pulled
    row is one day of one machine rather than one session either way. Saying so
    beats printing a short list as if it were the whole of one.
    """
    if not remote.records:
        return
    if merge_all:
        console.print(
            "[dim]This table is this machine's sessions alone: a pulled row is "
            "one day of one machine, and a restricted machine strips the session "
            "id before it pushes.[/dim]"
        )
        return
    console.print(_remote_note(remote))


def cmd_pull(args) -> None:
    """Fetch what this account's other machines spent, and store it locally.

    The half of `server sync` that only reads. Nothing belonging to this machine
    comes back: the server drops its copy of what this machine pushed and then
    drops any remaining record whose dedup key this machine also pushed, so what
    arrives is the remainder that is genuinely elsewhere.
    """
    from ccreport import push

    config = Path(args.config) if getattr(args, "config", None) else None
    if not push.configured(config):
        print(f"No {config or push.CONFIG_PATH} — "
              "run `ccreport server connect <url> --token ...` first.", file=sys.stderr)
        sys.exit(1)
    servers = [
        server for server in push.load_config(config)
        if not args.server or server.url == args.server
    ]
    if not servers:
        print(f"No server matched {args.server!r}.", file=sys.stderr)
        sys.exit(1)
    failed = False
    for server in servers:
        try:
            result = push.pull_from(server)
        except push.PushError as exc:
            failed = True
            console.print(f"[bold]{server.url}[/bold]: [red]{exc}[/red]")
            continue
        declared = (
            f", {result.declared} declared plan change(s)" if result.declared else ""
        )
        console.print(
            f"[bold]{server.url}[/bold]: {result.pulled} other machine(s) "
            f"on this account{declared}",
        )
    if failed:
        sys.exit(1)


def parse_date(s: str) -> datetime:
    """Parse YYYYMMDD or YYYY-MM-DD into a timezone-aware datetime (local midnight)."""
    from zoneinfo import ZoneInfo

    s = s.replace("-", "")
    dt = datetime.strptime(s, "%Y%m%d")  # noqa: DTZ007 - tz attached below, once known
    try:
        from ccreport.pricing import _local_tz
        tz = _local_tz()
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return dt.replace(tzinfo=tz)


def _warn_unreachable_history(kind: str, value: str, target: str) -> None:
    """Warn that a remote/cwd_prefix rule reaches purged history by name only."""
    n = count_ccreport_records_without_signals()
    if not n:
        return
    implied = _implied_name(kind, value)
    reach = f"only those stored as {implied!r}" if implied else "none of them"
    print(
        f"note: {n} cached record(s) from purged logs carry no {kind} to match "
        f"on, so this rule reaches {reach}.\n"
        f"      Any older usage still grouped elsewhere: "
        f"ccreport merge <that-name> {target}",
        file=sys.stderr,
    )


def cmd_migrate(args) -> None:
    """Move the cache, snapshots and config off the paths they used to have.

    Every command does this for itself the first time it opens a DB that isn't
    there yet, so this exists to make the move explicit and to say what it did.
    Running it twice is not an error: the second run finds the destinations in
    place and reports that, which is also what a machine that never held the old
    layout sees.
    """
    if args.dry_run:
        pending = [
            (src, dst)
            for src, dst in (
                (cache_db._LEGACY_XDG_CACHE_DIR / "cache.db", cache_db.DB_PATH),  # noqa: SLF001
                (cache_db._LEGACY_CACHE_DIR / "cache.db", cache_db.DB_PATH),  # noqa: SLF001
                (cache_db._LEGACY_SNAPSHOT_DIR, cache_db._DEFAULT_SNAPSHOT_DIR),  # noqa: SLF001
                (project_identity.LEGACY_CONFIG_PATH, project_identity.CONFIG_PATH),
            )
            if src.exists() and not dst.exists()
        ]
        if not pending:
            print("Nothing to migrate.")
            return
        for src, dst in pending:
            print(f"would move {src} -> {dst}")
        return

    moved = cache_db.relocate_legacy_paths()
    if not moved:
        print("Nothing to migrate.")
        return
    for line in moved:
        print(f"moved {line}")


def _pull_ff_only(root: Path) -> int:
    """Fast-forward the checkout and echo what git said. Returns git's exit code.

    `--ff-only` and nothing else. A merge or a rebase here would resolve someone
    else's conflicts inside a reporting tool; refusing leaves the user in a tree
    they can still reason about, with git's own message saying why.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Could not run git pull: {exc}", file=sys.stderr)
        return 1
    for stream, sink in ((out.stdout, sys.stdout), (out.stderr, sys.stderr)):
        text = stream.strip()
        if text:
            print(text, file=sink)
    if out.returncode != 0:
        print("Fast-forward refused — the checkout has commits of its own, "
              "or the pull needs a merge. Resolve it with git.", file=sys.stderr)
    return out.returncode


def cmd_update(args) -> None:
    """Report how far origin's master has moved past this checkout.

    The status line renders the same number, but from an answer a detached
    child refreshes twice a day. Asking here runs the check live, because the
    user asked now, and writes the result back through the same meta keys — so
    a check from the CLI also paces the next spawn and refreshes the segment.

    Every outcome the check itself can reach exits 0, including the ones that
    could not answer: not knowing is not a failure of the command. Only a
    refused `--pull` exits non-zero, with git's own code.
    """
    from ccreport import update_check

    root = update_check.checkout_root()
    if root is None:
        print("Installed as a package — there is no checkout here to update.")
        return

    upstream = f"origin/{update_check.UPSTREAM_BRANCH}"
    sha = update_check.local_head_sha(root)
    if sha is None:
        cache_db.write_update_check("", None, time.time())
        print(f"Could not read HEAD in {root}.")
        return

    slug = update_check.remote_slug(root)
    behind = update_check.commits_behind(slug, sha) if slug else None
    cache_db.write_update_check(sha, behind, time.time())

    if slug is None:
        print("origin is not a GitHub remote, so there is nothing to compare against.")
        return
    if behind is None:
        print(f"Could not reach GitHub to compare against {upstream}.")
        return
    if behind == 0:
        print(f"Up to date with {upstream}.")
        return

    commits = "commit" if behind == 1 else "commits"
    print(f"{behind} {commits} behind {upstream}.")
    if not args.pull:
        print("Pull them: ccreport update --pull")
        return
    code = _pull_ff_only(root)
    if code != 0:
        sys.exit(code)


def cmd_overrides(args) -> None:
    """Manage the local project-grouping override rules."""
    if args.command == "merge":
        add_project_override(args.kind, args.source, args.target)
        label = args.source if args.kind == "name" else f"{args.kind}:{args.source}"
        print(f"Grouping {label} -> {args.target}")
        if args.kind in ("remote", "cwd_prefix"):
            _warn_unreachable_history(args.kind, args.source, args.target)
        return
    if args.command == "unmerge":
        n = delete_project_override(args.source, args.kind)
        print(f"Removed {n} rule(s) matching {args.source!r}")
        return
    # A bare "overrides" lists the rules.
    rules = get_project_overrides()
    if not rules:
        print("No override rules. Add one with: ccreport merge <from> <into>")
        return
    width = max(len(r["match_value"]) for r in rules)
    for r in rules:
        kind = "" if r["match_kind"] == "name" else f"[{r['match_kind']}] "
        print(f"  {kind}{r['match_value']:<{width}}  ->  {r['target']}")


def _confirm(question: str) -> bool:
    """Ask *question* on stdin. Anything but an explicit yes is a no.

    A closed or non-interactive stdin answers no rather than raising: a run
    that meant to go through unattended has --yes to say so.
    """
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _pre_capture_records(
    records: list[UsageRecord], events: list[dict],
) -> list[UsageRecord]:
    """The records an adoption row covers: those older than the first capture.

    Not "the records currently reporting as unknown", which is the same set
    only until the first adoption and reads as empty afterwards — so a preview
    built on it would tell a user re-adopting that there is nothing to adopt.

    Captures only. A backfilled row is a claim about the same pre-capture
    stretch this covers, so counting one as the boundary would shrink the
    adoption to the records older than a row that is itself an adoption of a
    kind.
    """
    captures = [e["ts"] for e in events if e["source"] == SOURCE_CAPTURE]
    if not captures:
        return []
    first = min(captures)
    return [r for r in records if r.timestamp.timestamp() < first]


def cmd_adopt(args) -> None:
    """Attribute the history that predates account capture, or undo that.

    One backdated row does the whole job, because attribution takes the newest
    event at or before each record: an event older than every record is the one
    every otherwise-unattributed record lands on. Nothing is rewritten, no
    record cache is invalidated, and undoing it is a single DELETE.

    Exits 1 when the capture log is empty — there is no identity to adopt under.
    Every other outcome, refusal and abort included, returns.
    """
    if args.remove:
        if clear_adopted_account():
            print(f"Removed. Pre-capture history reads as {UNKNOWN_ACCOUNT!r} again.")
        else:
            print("Nothing to remove: pre-capture history is not adopted.")
        return

    identity = read_latest_account()
    if identity is None:
        print(
            "No account has been captured yet, so there is nothing to adopt "
            "history under.\n"
            "The status line records the signed-in account on its next render; "
            "try again after that.",
            file=sys.stderr,
        )
        sys.exit(1)

    existing = read_adopted_account()
    if existing is not None and _same_account(existing, identity):
        print(f"Pre-capture history is already adopted under "
              f"{_account_description(existing)}.")
        return

    records = load_all_records()
    covered = _pre_capture_records(records, load_account_events())
    cost = sum(record_cost(r) for r in covered)

    if not covered:
        print("No records predate the first captured account; nothing to adopt.")
        return

    if existing is not None:
        print(f"Currently adopted under {_account_description(existing)}.")
    print(
        f"Adopt {len(covered)} record(s) ({fmt_cost(cost)}) predating account "
        f"capture\n  under {_account_description(identity)}"
    )
    if not args.yes and not _confirm("Proceed?"):
        print("Aborted.")
        return

    # Identity copied from the newest capture, tiers deliberately blank: the row
    # claims who paid for pre-capture history, and which tier they were on back
    # then is not something today's login can be asked. A copied tier would read
    # as a reading and would date a tier change to the wrong side of it.
    set_adopted_account({**identity, **dict.fromkeys(_ACCOUNT_TIER_COLS)})
    print(f"Adopted. Those records now report as {_account_description(identity)}.")
    print("Undo with: ccreport adopt --remove")


def _known_accounts(events: list[dict]) -> dict[str, dict]:
    """Every account the log has seen, reachable by uuid and by login email.

    Both keys, because a person writing a timeline off their receipts has the
    email in front of them and the uuid nowhere. An email that two accounts
    have used maps to neither — the same address billing through work and
    personally is exactly the case the organization fields exist to separate,
    and guessing between them would attribute a plan change to the wrong one.
    """
    by_uuid: dict[str, dict] = {}
    by_email: dict[str, list[dict]] = {}
    for e in events:
        by_uuid[e["account_uuid"]] = e
        if e["email"]:
            seen = by_email.setdefault(e["email"], [])
            if not any(s["account_uuid"] == e["account_uuid"] for s in seen):
                seen.append(e)
    known = dict(by_uuid)
    for email, matches in by_email.items():
        if len(matches) == 1 and email not in known:
            known[email] = matches[0]
    return known


def _resolve_entries(entries: list, events: list[dict]) -> tuple[list[dict], list[str]]:
    """Declared entries as account_events rows, plus the accounts skipped.

    Identity is copied from the log rather than taken from the file: a plan
    change says nothing about who the account is, and a timeline that could
    introduce one could introduce a typo of one — which reads as a second
    account for as long as the rows stand.

    An account this machine has never signed in to is skipped rather than
    refused, because one timeline covers a person's accounts and each of their
    machines has seen some of them. The names skipped are returned so the
    caller can print them: a typo and a machine that legitimately does not know
    an account look identical here, and only the person reading can tell which
    one it was.
    """
    known = _known_accounts(events)
    rows, skipped = [], []
    for e in entries:
        identity = known.get(e.account)
        if identity is None:
            if e.account not in skipped:
                skipped.append(e.account)
            continue
        rows.append({
            "ts": e.ts,
            **{c: identity[c] for c in _ACCOUNT_IDENTITY_COLS},
            **e.tiers(),
        })
    return rows, skipped


def _tier_line(row: dict) -> str:
    """One resolved entry as a preview line: when, who, and what it became."""
    when = _fmt_epoch(row["ts"])
    tier = tier_timeline.effective_tier(row) or _ABSENT
    seat = row.get("seat_tier")
    return f"  {when}  {_account_description(row)}  {tier}" + (f" (seat {seat})" if seat else "")


def cmd_tiers(args) -> None:
    """Declare plan changes the capture log never saw, list them, or drop them.

    The receipts are the source: `account_events` holds a tier only where a
    render happened to catch one, so a machine that started reporting after its
    first record has no entitlement behind most of its history, and
    `ccreport limits` prints a fill rate with nothing to set it beside.

    Exits 1 on an unreadable file or an account the log does not know. Every
    other outcome, abort included, returns.
    """
    from ccreport import push

    if args.remove:
        gone = clear_backfilled_accounts()
        print(f"Removed {gone} declared plan change(s)." if gone
              else "Nothing to remove: no plan changes are declared.")
        return

    events = load_account_events()
    if args.file is None:
        declared = [e for e in events if e["source"] == SOURCE_BACKFILL]
        if not declared:
            print("No plan changes are declared. Pass a TOML file to declare some.")
            return
        print(f"{len(declared)} declared plan change(s):")
        for row in declared:
            print(_tier_line(row))
        return

    try:
        entries = tier_timeline.parse(Path(args.file).read_text())
        rows, skipped = _resolve_entries(entries, events)
    except (OSError, ValueError) as e:
        print(f"{args.file}: {e}", file=sys.stderr)
        sys.exit(1)

    for name in skipped:
        print(f"Skipping {name!r}: this machine has never signed in to it.")
    if not rows:
        print(f"{args.file} declares nothing this machine can place.")
        return

    existing = sum(1 for e in events if e["source"] == SOURCE_BACKFILL)
    print(f"Declare {len(rows)} plan change(s):")
    for row in sorted(rows, key=lambda r: r["ts"]):
        print(_tier_line(row))
    if existing:
        print(f"{existing} already declared; any at these same moments are replaced.")
    if not args.yes and not _confirm("Proceed?"):
        print("Aborted.")
        return

    backfill_account_events(rows)
    print("Declared. `ccreport limits` reads these as the tier in force.")
    if push.configured():
        print("A server that declares one of these accounts replaces its rows here on "
              "the next pull. Type the timeline there too, or it comes back as the "
              "server has it.")
    print("Undo with: ccreport tiers --remove")


# --- Rate limit utilization history ---

# The four windows the statusline can sample, in the order it offers them and
# the order this report prints them. Also the --window choices.
#
# Where a cell has nothing to show: a tier no event recorded, a scoped sample
# that named no model. Spelled rather than left empty so the gap reads as "not
# recorded" instead of as a rendering fault.
_ABSENT = "—"


def _instance_extra(
    inst: WindowInstance, extra: ExtraIndex, now: float,
) -> float | None:
    """Extra usage billed while *inst* ran, or None where nothing bounds it.

    From the window opening to its reset, or to now while it is still open —
    not from the moment it was seen full, and not gated on Hit. Credits are
    billed by whichever limit ran out, which is rarely this one: a session
    window filling bills against a week window that stands at 44%, and a window
    sampled at 99% had run out too and the render that would have said so never
    happened. Both are in this machine's history.

    So the column answers "what was billed while this window ran", and the Hit
    beside it says whether this window is why. Windows of one type partition
    time, so a table's figures sum to what the period cost; two types overlap,
    and each reports the same dollars.

    The span is the window's own, not the sampled part of it: unwatched hours
    bill like any other. Where its length is unknown the first sample has to
    stand in, and the hours before it are missing from the figure.
    """
    return extra.spent_between(
        inst.started_at if inst.started_at is not None else inst.first_ts,
        min(inst.resets_at, now),
    )


def _instance_spend(
    inst: WindowInstance, index: SpendIndex, extra: ExtraIndex, now: float,
) -> WindowSpend:
    """windows.instance_spend, with this machine's Extra series joined on.

    The Extra reading is the one part of the row the server has no copy of, so
    it is read here and passed in rather than reached for from the shared
    module.
    """
    return instance_spend(inst, index, now, _instance_extra(inst, extra, now))


def _load_instance_spend(
    instances: list[WindowInstance], now: float,
) -> dict[tuple[str, str | None, float], WindowSpend]:
    """Price every instance, keyed the way window_instances grouped them.

    One corpus load, bounded to the span the instances cover: a report of the
    last two days of windows has no use for two years of records. The bound is
    the same one `--since` gives every other report, so the load is the cheap
    filtered path rather than the whole table.

    The full record path on purpose — dedup is what makes the number an answer.
    Summing the rows raw double-counts every message the log wrote twice, which
    on this machine reported $510 against a stretch that actually cost $231.

    The Extra series is loaded whole: 31 days of slow renders, against a record
    corpus that has to be filtered to stay affordable.
    """
    if not instances:
        return {}
    since = _as_local(min(i.first_ts for i in instances))
    until = _as_local(max(i.peak_ts for i in instances))
    index = SpendIndex(load_all_records(since=since, until=until))
    extra = ExtraIndex(cache_db.load_extra_snapshots())
    return {i.key: _instance_spend(i, index, extra, now) for i in instances}


def _fmt_span(seconds: float) -> str:
    """A fill time as hours and minutes: 3h 07m, 42m, 0m."""
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _as_local(ts: float) -> datetime:
    """An epoch as an aware datetime, for the AccountTimeline lookups.

    Both lookups compare epochs, so the zone is only there to make the value
    aware — but they take a datetime because every other caller has one.
    """
    return datetime.fromtimestamp(ts, _local_tz())


def _fmt_epoch(ts: float) -> str:
    """An epoch as local wall-clock time, in the tables' usual format."""
    return _as_local(ts).strftime("%Y-%m-%d %H:%M")


def _peak_style(pct: float) -> str:
    """Colour a peak by how close it came to the limit."""
    if round(pct) >= 100:
        return "bold red"
    if pct >= 90:
        return "yellow"
    if pct >= 50:
        return "green"
    return "dim green"


def _fmt_burn(rate: float | None) -> str:
    """A burn rate as points per hour, or the absent marker.

    Two decimals below 10: a 7-day window moves at tenths of a point an hour,
    and one decimal renders half a week's history as 0.1.
    """
    if rate is None:
        return _ABSENT
    return f"{rate:.1f}" if rate >= 10 else f"{rate:.2f}"


def _fmt_money(usd: float | None) -> str:
    """A spend or an exchange rate, or the absent marker for a missing corpus."""
    return _ABSENT if usd is None else fmt_cost(usd)


def _fmt_share(share: float | None) -> str:
    """A cache-hit share as a whole percentage, or the absent marker.

    Whole percents because the column answers "roughly how much of this window
    was already paid for", and a tenth of a point between two windows is not a
    difference anyone acts on.
    """
    return _ABSENT if share is None else f"{share * 100:.0f}%"


def _limits_entry(
    inst: WindowInstance,
    accounts: AccountTimeline,
    spend: WindowSpend,
    now: float,
) -> dict:
    """One instance as JSON: raw floats and epochs, nothing formatted.

    The point of --json here is arithmetic somewhere else — plotting a fill
    curve, correlating a tier change with the week it landed — so every number
    goes out as stored and the local-time rendering stays in the table.
    """
    when = _as_local(inst.first_ts)
    return {
        "window": inst.window,
        "model": inst.model,
        "resets_at": inst.resets_at,
        "first_ts": inst.first_ts,
        "peak_ts": inst.peak_ts,
        "last_ts": inst.last_ts,
        "opening_used_pct": inst.opening_pct,
        "peak_used_pct": inst.peak,
        "latest_used_pct": inst.latest_pct,
        "samples": len(inst.samples),
        "fill_seconds": inst.fill_s,
        "burn_pp_per_hour": inst.burn_pph,
        "open": inst.is_open(now),
        "projected_used_pct": inst.projected_pct(now),
        "spend_usd": spend.usd,
        "usd_per_pp": spend.per_pp,
        "headroom_usd": spend.headroom_usd,
        "extra_usd": spend.extra_usd,
        "cache_read_tokens": spend.cache_read,
        "observed_input_tokens": spend.observed_input,
        "cache_hit_share": spend.cache_hit,
        "hit_limit": inst.hit_limit,
        "partial": inst.partial,
        "window_start": inst.started_at,
        "unseen_seconds": inst.unseen_s,
        "account": accounts.label_at(when),
        "limit_tier": accounts.tier_at(when),
    }


def _tiered(inst: WindowInstance, accounts: AccountTimeline) -> bool:
    """Whether the account log records which tier *inst* filled against.

    False for a window whose first sample predates the tier columns: its peak
    and its spend are real readings, and a fill rate with no entitlement behind
    it cannot be set beside the window below it in the table.
    """
    return accounts.tier_at(_as_local(inst.first_ts)) is not None


def _partial_note(inst: WindowInstance) -> str | None:
    """The caption line for a window that was already filling when first seen.

    Marked in words under the table rather than by dropping the instance: the
    readings taken are real, and the peak is the only record that the window
    reached that height at all.
    """
    if not inst.partial:
        return None
    named = f"{short_model(inst.model)} " if inst.model else ""
    unseen = inst.unseen_s
    lag = f", {_fmt_span(unseen)} after it opened" if unseen else ""
    return (
        f"* {named}{_fmt_epoch(inst.resets_at)}: first sampled at "
        f"{inst.opening_pct:.1f}%{lag} — Peak counts that rise, Spend and $/pp do not"
    )


def _group_per_pp(group: list[WindowInstance], spends: dict) -> float | None:
    """The group's own exchange rate: its total spend over its total rise.

    Not the mean of the per-window rates — a window that rose one point would
    weigh as much as a week that rose forty.
    """
    priced = [(i, spends[i.key]) for i in group if spends[i.key].usd is not None]
    rise = sum(i.rise for i, _s in priced if i.rise > 0)
    if not rise:
        return None
    return sum(s.usd for i, s in priced if i.rise > 0) / rise


def report_limits(
    instances: list[WindowInstance],
    accounts: AccountTimeline,
    spends: dict[tuple[str, str | None, float], WindowSpend],
) -> None:
    """Print one table per window type, each summarized by its own footer.

    *instances* arrive in instance_order, so each group is already chronological.
    Nothing here needs the clock: an open window is a row like the closed ones,
    and where its rate leads is in --json.

    Account and tier are attributed at the instance's first sample, the way
    ccreport attributes a record: the table answers "who was drawing on this
    window, under which tier", and a /login part-way through a window makes that
    the account the window opened under.

    *spends* is keyed by WindowInstance.key. Every instance must be in it, an
    unpriceable one as an all-None WindowSpend: a missing key here would be a
    KeyError in the middle of a rendered table.
    """
    for window in window_types(instances):
        group = [i for i in instances if i.window == window]
        scoped = window == "scoped"
        notes = [n for n in (_partial_note(i) for i in group) if n]
        table = Table(
            title=f"{_LIMIT_WINDOW_LABELS.get(window, window)} — {len(group)} window(s)",
            title_style="bold", box=box.ROUNDED, expand=False, show_lines=False,
            caption="\n".join(notes) or None, caption_style="dim",
        )
        table.add_column("Reset", style="white", no_wrap=True)
        if scoped:
            table.add_column("Model", style="magenta", no_wrap=True)
        table.add_column("Peak", justify="right", no_wrap=True)
        table.add_column("Samples", justify="right", style="dim", no_wrap=True)
        table.add_column("Fill", justify="right", no_wrap=True)
        table.add_column("pp/h", justify="right", no_wrap=True)
        table.add_column("Spend", justify="right", no_wrap=True)
        table.add_column("$/pp", justify="right", no_wrap=True)
        table.add_column("Cache", justify="right", style="cyan", no_wrap=True)
        table.add_column("Extra", justify="right", no_wrap=True)
        table.add_column("Hit", justify="center", no_wrap=True)
        # The two wrappable columns, so Rich shaves width off these first.
        table.add_column("Account", style="green")
        table.add_column("Tier", style="dim")

        for inst in group:
            when = _as_local(inst.first_ts)
            tier = accounts.tier_at(when)
            spend = spends[inst.key]
            row: list = [_fmt_epoch(inst.resets_at)]
            if scoped:
                row.append(short_model(inst.model) if inst.model else _ABSENT)
            row += [
                Text(
                    f"{inst.peak:.1f}%{'*' if inst.partial else ''}",
                    style=_peak_style(inst.peak),
                ),
                str(len(inst.samples)),
                _fmt_span(inst.fill_s),
                _fmt_burn(inst.burn_pph),
                Text(_fmt_money(spend.usd), style=cost_style(spend.usd or 0.0)),
                _fmt_money(spend.per_pp),
                _fmt_share(spend.cache_hit),
                Text(_fmt_money(spend.extra_usd), style=cost_style(spend.extra_usd or 0.0)),
                Text("yes", style="bold red") if inst.hit_limit else "",
                _flex_cell(accounts.label_at(when)),
                _flex_cell(tier or _ABSENT),
            ]
            table.add_row(*row)

        hits = sum(1 for i in group if i.hit_limit)
        peak = max(i.peak for i in group)
        priced: list[float] = [
            usd for i in group if (usd := spends[i.key].usd) is not None
        ]
        # Windows of one type partition the period, so this totals what it cost
        # in credits — short whatever the windows with no baseline billed.
        extras: list[float] = [
            usd for i in group if (usd := spends[i.key].extra_usd) is not None
        ]
        summary: list = [Text(f"{len(group)} window(s)", style="dim bold")]
        if scoped:
            summary.append("")
        summary += [
            Text(f"{peak:.1f}%", style=_peak_style(peak)),
            str(sum(len(i.samples) for i in group)),
            "",
            "",
            _fmt_money(sum(priced) if priced else None),
            _fmt_money(_group_per_pp(group, spends)),
            _fmt_share(group_cache_hit(group, spends)),
            _fmt_money(sum(extras) if extras else None),
            f"{hits} hit",
            "", "",
        ]
        table.add_section()
        table.add_row(*summary, style="dim")
        # Which columns go when the terminal is too narrow for all of them.
        # Tier and account change rarely and are named in the row above the one
        # that changed them; the sample count is how the numbers were arrived
        # at, not one of them. Cache goes before Extra because Extra is real
        # money and a cache share is a diagnostic. Extra goes last and only to save the scoped
        # table, which carries a Model column the other three do not: at 80
        # columns dropping the first three still leaves it a character short,
        # and Rich's answer to that is to ellipsize every column at once.
        _fit_columns(table, ("Tier", "Account", "Samples", "Cache", "Extra"))
        _print_report(table)


# --- Archiving the days that can no longer be re-read ---

ARCHIVE_MIN_AGE_DAYS = 30
"""How far back from local midnight a day has to be before it can be folded.

Well past the ~30-day JSONL retention Claude Code applies, so a file this old
whose log is already gone is gone for good. The cutoff below can only move it
further back, never nearer.
"""


def _archive_cutoff(min_age_days: int) -> float:
    """The newest instant `ccreport archive` may fold, as an epoch second.

    Two bounds, and the older wins. The age bound is *min_age_days* back from
    local midnight. The other is `ccreport limits`: it prices each rate-limit
    window against the records covering its fill span, rate_limit_snapshots is
    never pruned, and a window's span reaches back from its oldest reading by
    the length of the window. Fold behind that and the report starts pricing
    windows against a corpus with a hole in it, which reads as a cheap week.
    """
    from ccreport import windows

    midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    by_age = (midnight - timedelta(days=min_age_days)).timestamp()
    oldest = cache_db.oldest_rate_limit_sample_ts()
    if oldest is None:
        return by_age
    return min(by_age, oldest - max(windows.LIMIT_WINDOW_SPAN_S.values()))


class ArchivePlan(NamedTuple):
    """What one `ccreport archive` run would fold, and what it would leave."""

    rows: list[tuple]
    paths: set[str]
    records: int
    """Cached record rows the fold would delete.

    Every row of every folded file, not the calls the archive ends up carrying:
    more than half of them lost a dedup and were never in a report, and a
    preview that counted only the survivors would under-report the delete by
    that much."""
    held_back: dict[str, str]
    """Purged files it will not touch this run, path -> why."""


def _archive_dedup_pass(files: list[Path], records_by_file: dict) -> dict:
    """Walk the live half of the corpus for its dedup keys alone.

    A full load claims a duplicated message for the first file it reaches, and
    it reaches the live files first. Replaying that here means an orphan's copy
    of a live message is dropped from the archive exactly as the report drops
    it, rather than folded in and then added back beside the live one.

    Returns the _keep bundle, so the orphan pass below carries on with the same
    seen_keys. The override and the change log are deliberately left out: a
    dedup key is a function of neither, and the archive stores the raw identity
    that both of them resolve at read time.
    """
    filters = {
        "since": None, "until": None, "project_filter": None,
        "account_filter": None, "seen_keys": set(),
        "override": None, "accounts": None,
    }
    for path in files:
        for rec in _deserialize_records(records_by_file.pop(str(path), [])):
            _keep(rec, **filters)
    return filters


def _plan_archive(min_age_days: int) -> tuple[ArchivePlan, float]:
    """What a fold at the current cutoff would do. Returns (plan, cutoff_ts)."""
    cutoff_ts = _archive_cutoff(min_age_days)
    files = discover_jsonl_files()
    live_paths = {str(p) for p in files}
    _ensure_cache_valid(live_paths)
    _refresh_changed_files(files, load_ccreport_file_meta())

    records_by_file = load_ccreport_records_in_range(None, None)
    filters = _archive_dedup_pass(files, records_by_file)

    already = archived_file_paths()
    event_ts = sorted(ev["ts"] for ev in load_account_events())
    projects_dirs = _get_projects_dirs()

    kept_by_path: dict[str, list[UsageRecord]] = {}
    held_back: dict[str, str] = {}
    for path, raw in records_by_file.items():
        if path in live_paths or path in already:
            continue
        kept = [rec for rec in _deserialize_records(raw) if _keep(rec, **filters)]
        stamps = [rec.timestamp.timestamp() for rec in kept]
        if stamps and max(stamps) >= cutoff_ts:
            held_back[path] = "newer than the cutoff"
            continue
        if stamps and any(min(stamps) <= ts <= max(stamps) for ts in event_ts):
            held_back[path] = "spans an account change"
            continue
        kept_by_path[path] = kept

    # A record archived here stops taking part in the dedup, so anything it beat
    # and left in the table would start counting. Nothing on a real machine is
    # in that state — a message id belongs to one session log — but a fold is
    # not undoable, so the collision is checked rather than assumed away.
    won = {
        key
        for path, kept in kept_by_path.items()
        for key in (_dedup_keys(kept))
    }
    if won:
        for path, raw in records_by_file.items():
            if path in kept_by_path or path in already:
                continue
            for rec in _deserialize_records(raw):
                clash = _dedup_keys([rec]) & won
                if clash:
                    for other, kept in list(kept_by_path.items()):
                        if _dedup_keys(kept) & clash:
                            del kept_by_path[other]
                            held_back[other] = "a live copy of one of its messages"

    rows: dict[tuple, list] = {}
    n_records = 0
    for path, kept in kept_by_path.items():
        prefix = _project_dir_prefix(path, projects_dirs) or ""
        n_records += len(records_by_file[path])
        for rec in kept:
            _fold_archive_row(rows, rec, prefix)
    plan = ArchivePlan(
        rows=[(*key, *row) for key, row in rows.items()],
        paths=set(kept_by_path),
        records=n_records,
        held_back=held_back,
    )
    return plan, cutoff_ts


def _dedup_keys(records: list[UsageRecord]) -> set[str]:
    """The dedup identities *records* carry, skipping the ones that have none."""
    keys = set()
    for rec in records:
        key = dedup_identity(
            rec.dedup_key, rec.message_id, rec.session_id,
            rec.timestamp.timestamp(), rec.model,
            (rec.tokens.input, rec.tokens.output,
             rec.tokens.cache_create, rec.tokens.cache_read),
        )
        if key is not None:
            keys.add(key)
    return keys


def _fold_archive_row(rows: dict[tuple, list], rec: UsageRecord, prefix: str) -> None:
    """Add one record to its archive bucket, creating the bucket if new."""
    key = (
        rec.day_key(), rec.fx_date().isoformat(), rec.session_id,
        rec.project, rec.model, rec.cwd or "", rec.repo or "", prefix,
    )
    ts = rec.timestamp.timestamp()
    t = rec.tokens
    row = rows.get(key)
    if row is None:
        rows[key] = [ts, ts, t.input, t.output, t.cache_create, t.cache_read,
                     rec.cost(), rec.count]
        return
    row[0] = min(row[0], ts)
    row[1] = max(row[1], ts)
    row[2] += t.input
    row[3] += t.output
    row[4] += t.cache_create
    row[5] += t.cache_read
    row[6] += rec.cost()
    row[7] += rec.count


def cmd_archive(args) -> None:
    """Fold the purged half of the corpus into day rows, or say what that would be."""
    plan, cutoff_ts = _plan_archive(args.min_age_days)
    cutoff = datetime.fromtimestamp(cutoff_ts).astimezone()
    console.print(f"Cutoff: [cyan]{cutoff:%Y-%m-%d %H:%M}[/cyan] "
                  f"(nothing at or after it is folded)")
    if not plan.paths:
        console.print("Nothing to archive.")
    else:
        console.print(
            f"Fold: [cyan]{len(plan.paths)}[/cyan] purged files, "
            f"[cyan]{plan.records}[/cyan] record rows -> "
            f"[cyan]{len(plan.rows)}[/cyan] archive rows"
        )
        console.print(f"Drop: [cyan]{plan.records}[/cyan] rows from ccreport_records")
        console.print("[dim]Every folded file stops being pushed: the server keys "
                      "a file on (machine, path) and replaces what it holds.[/dim]")
    if plan.held_back:
        reasons: dict[str, int] = {}
        for reason in plan.held_back.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, n in sorted(reasons.items()):
            console.print(f"Held back: [yellow]{n}[/yellow] purged files — {reason}")
    if args.dry_run:
        console.print("[dim]--dry-run: nothing was written.[/dim]")
        return
    if not plan.paths:
        return
    deleted = save_ccreport_archive(plan.rows, plan.paths)
    console.print(f"Archived. [green]{deleted}[/green] record rows deleted; "
                  f"run [cyan]sqlite3 {cache_db.DB_PATH} 'VACUUM'[/cyan] "
                  "to give the space back.")


def cmd_limits(args) -> None:
    """Report how full each rate-limit window got, and how fast.

    rate_limit_snapshots and account_events answer how full each window got and
    who was drawing on it. What the filling cost is not in either — a sample
    carries a percentage and no tokens — so the records covering the sampled
    span are loaded too, and only that span: the window instances bound the
    load, and history nobody sampled buys this report nothing.

    --since/--until select samples, not instances, so a window straddling the
    boundary reports the peak and fill time of the part inside the range, and
    the spend of that part.

    The tables carry the windows whose tier is recorded; --json carries every
    instance. A table is read down a column, and the history from before the
    tier columns cannot be compared with the rows under it, while --json is
    arithmetic somewhere else and drops nothing.

    Exits 1 with a reason on stderr when no samples have been recorded at all,
    when the filters leave none, and when no sampled window has a tier. Nothing
    else is printed there: the report is what the run is for.
    """
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None

    samples = load_rate_limit_snapshots()
    if not samples:
        print(
            "No rate-limit samples recorded yet; the status line writes them as "
            "it renders.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.window:
        samples = [s for s in samples if s["window"] == args.window]
    if since:
        samples = [s for s in samples if s["ts"] >= since.timestamp()]
    if until:
        samples = [s for s in samples if s["ts"] <= until.timestamp()]

    # A placeholder reset (RL_MAX_LOOKAHEAD_S past its reading) is not a window
    # and is left out silently. The tables print the reset time of every row, so
    # a reader counting them against a stored total has the dates to do it with,
    # and a note above every run is a line nobody reads twice.
    samples = [s for s in samples if not implausible_reset(s)]
    if not samples:
        print("No rate-limit samples match those filters.", file=sys.stderr)
        sys.exit(1)

    instances = sorted(window_instances(samples), key=instance_order)
    accounts = AccountTimeline(load_account_events())
    now = datetime.now(UTC).timestamp()
    if args.json:
        spends = _load_instance_spend(instances, now)
        print(json.dumps(
            [_limits_entry(i, accounts, spends[i.key], now) for i in instances],
            indent=2,
        ))
        return

    shown = [i for i in instances if _tiered(i, accounts)]
    if not shown:
        print(
            "Every sampled window predates the tier columns in the account log, so "
            "the tables would carry no comparison. `ccreport limits --json` prints "
            "them as recorded.",
            file=sys.stderr,
        )
        sys.exit(1)
    report_limits(shown, accounts, _load_instance_spend(shown, now))


def main() -> None:
    # Before anything opens the DB: every path below it touches cache_db, and
    # get_connection reads this once, when it opens the singleton connection.
    # An interactive report is a bad place to spend the once-a-day 72 MB copy;
    # the statusline's detached refresh takes it instead. An explicit setting
    # from the environment wins.
    os.environ.setdefault("CLAUDE_CACHE_SNAPSHOT_DEFER", "1")
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code token usage and costs from local JSONL logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  ccreport daily --since 20260201\n"
               "  ccreport monthly\n"
               "  ccreport session --limit 10\n"
               "  ccreport daily --breakdown --project myapp\n"
               "  ccreport account\n"
               "  ccreport monthly --account personal@example.com\n"
               "  ccreport adopt            # claim pre-capture history\n"
               "  ccreport tiers plans.toml # declare plan changes off the receipts\n"
               "  ccreport limits -w session\n"
               "  ccreport archive --dry-run  # what the purged half folds to\n"
               "  ccreport update           # is master ahead of this checkout?\n"
               "  ccreport migrate --dry-run\n"
               "  ccreport --server https://ccreport.example.net monthly\n"
               "  ccreport push            # send this machine's records\n"
               "  ccreport server connect https://ccreport.example.net --token ...\n"
               "  ccreport server status\n"
               "  ccreport budget          # spend forecast per account\n",
    )
    sub = parser.add_subparsers(dest="command", help="Report type")

    # Common args
    for name in ["daily", "monthly", "project", "session", "account"]:
        p = sub.add_parser(name)
        p.add_argument("--all", "-A", action="store_true",
                       help="Merge this account's spend from every pushing machine")
        p.add_argument("--since", help="Start date (YYYYMMDD or YYYY-MM-DD)")
        p.add_argument("--until", help="End date (YYYYMMDD or YYYY-MM-DD)")
        p.add_argument("--project", "-p", help="Filter by project name (substring match)")
        p.add_argument("--account", "-a", help="Filter by account email (substring match)")
        p.add_argument("--json", "-j", action="store_true", help="Output as JSON")
        p.add_argument("--no-mva", action="store_true", help="Show NOK without 25%% MVA")
        # SUPPRESS, not None: a subparser argument with a default overwrites
        # whatever the top-level parser already put on the namespace, so
        # `ccreport --server URL daily` would lose the URL to this line's
        # default. Suppressed, the attribute is only set when the flag is given.
        p.add_argument("--server", default=argparse.SUPPRESS,
                       help="Render the merged report from this ccreport server")
        p.add_argument("--machine", default=argparse.SUPPRESS,
                       help="With --server: only this machine's records")
        if name == "daily":
            p.add_argument("--breakdown", "-b", "-m", action="store_true",
                           help="Show per-model breakdown")
        if name == "project":
            p.add_argument("--limit", "-l", type=int, default=20, help="Max projects to show (0=all)")
        if name == "session":
            p.add_argument("--limit", "-l", type=int, default=20, help="Max sessions to show (0=all)")

    # Project-grouping overrides (manual merges/renames, stored locally)
    sub.add_parser("overrides", help="List manual project-grouping rules")
    pm = sub.add_parser("merge", help="Group one project name into another")
    pm.add_argument("source", help="Name to remap (or remote/cwd-prefix with --kind)")
    pm.add_argument("target", help="Project name to group it under")
    pm.add_argument("--kind", choices=["name", "remote", "cwd_prefix"], default="name",
                    help="What 'source' matches against (default: name)")
    pu = sub.add_parser("unmerge", help="Remove a grouping rule")
    pu.add_argument("source", help="The rule's match value to remove")
    pu.add_argument("--kind", choices=["name", "remote", "cwd_prefix"],
                    help="Restrict removal to this match kind")

    # Claim the history that predates account capture (stored locally, like the
    # override rules above).
    pad = sub.add_parser(
        "adopt", help="Attribute pre-capture history to the signed-in account")
    pad.add_argument("--remove", action="store_true",
                     help=f"Undo it; that history reads as {UNKNOWN_ACCOUNT!r} again")
    pad.add_argument("--yes", "-y", action="store_true",
                     help="Skip the confirmation prompt")

    # The other half of the same problem: adopt claims who paid for pre-capture
    # history, this declares which plan they were paying for.
    pt = sub.add_parser(
        "tiers", help="Declare plan changes the capture log never saw")
    pt.add_argument("file", nargs="?",
                    help="TOML timeline of [[tier]] entries; omit to list what is declared")
    pt.add_argument("--remove", action="store_true",
                    help="Drop every declared plan change; captures are untouched")
    pt.add_argument("--yes", "-y", action="store_true",
                    help="Skip the confirmation prompt")

    # One-time relocation off the paths this tooling used inside macsetup.
    pmg = sub.add_parser(
        "migrate", help="Move the cache, snapshots and config to their ccreport paths")
    pmg.add_argument("--dry-run", "-n", action="store_true",
                     help="List what would move without moving it")

    # The same "master has moved" check the status line renders, asked live.
    pup = sub.add_parser("update", help="Check whether origin's master is ahead of this checkout")
    pup.add_argument("--pull", action="store_true",
                     help="Fast-forward the checkout when it is behind")

    # Per-account spend ceilings, and the projections measured against them.
    pb = sub.add_parser("budget", help="Per-account spend ceilings and the spend forecast")
    budget_sub = pb.add_subparsers(dest="budget_command")
    pbs = budget_sub.add_parser("set", help="Set an account's ceiling or renewal day")
    pbs.add_argument("account", help="The account label, as the reports spell it")
    pbs.add_argument("amount", type=float, nargs="?", help="Ceiling in USD")
    pbs.add_argument("--renewal-day", type=int,
                     help="Day of month the subscription renews; the usage API carries none")
    pbc = budget_sub.add_parser("clear", help="Forget an account's ceiling")
    pbc.add_argument("account", help="The account label")

    # Configuring which servers this machine pushes to, and under what policy.
    ps = sub.add_parser("server", help="Configure the ccreport servers this machine pushes to")
    server_sub = ps.add_subparsers(dest="server_command")
    for name, helptext in (
        ("connect", "Set up this machine against a server"),
        ("allow", "Identify more projects by name"),
        ("deny", "Stop identifying projects by name"),
        ("exclude", "Redact projects however open the server is"),
        ("unexclude", "Stop redacting projects the server would otherwise name"),
        ("status", "What each server knows this machine as"),
        ("push", "Push this machine's records to a ccreport server"),
        ("pull", "Fetch what the account's other machines spent"),
        ("sync", "Push, then pull, in one round trip"),
        ("disconnect", "Stop pushing to a server and forget it locally"),
    ):
        sp = server_sub.add_parser(name, help=helptext)
        verb = "Read" if name in ("status", "push", "pull", "sync") else "Write"
        sp.add_argument("--config",
                        help=f"{verb} somewhere other than ~/.config/ccreport/push.toml")
        if name == "connect":
            sp.add_argument("url", help="The server's base URL")
            sp.add_argument("--token", required=True, help="The token the web UI minted")
            sp.add_argument("--opt-in-repos", metavar="NAMES", nargs="?", const="",
                            help="Comma-separated projects to identify by name; sets restricted. "
                                 "The flag with no names sets restricted and identifies nothing")
            sp.add_argument("--exclude-repos", metavar="NAMES",
                            help="Comma-separated projects to redact on this server whatever "
                                 "--opt-in-repos says. The other direction: name everything "
                                 "but these")
            sp.add_argument("--only-on-network", metavar="CIDRS",
                            help="Comma-separated CIDRs this machine must be inside to push")
            sp.add_argument("--interval-minutes", metavar="N", type=int,
                            help="Minutes between detached pushes from this machine "
                                 "(default 30)")
        if name in ("allow", "deny", "exclude", "unexclude"):
            sp.add_argument("targets", nargs="+", metavar="TARGET",
                            help="Project names, before or after a merge rule, after an optional "
                                 "leading server URL as push.toml spells it. The URL may be left "
                                 "out when push.toml names one server")
        if name in ("push", "pull", "sync"):
            sp.add_argument("--server", help="Only this server URL, as push.toml spells it")
        if name in ("push", "sync"):
            sp.add_argument("--full", action="store_true",
                            help="Forget the watermark and offer every file again")
        if name == "disconnect":
            sp.add_argument("url", nargs="?",
                            help="The server's URL as push.toml spells it. May be left "
                                 "out when push.toml names one server")
            sp.add_argument("--yes", "-y", action="store_true",
                            help="Skip the confirmation prompt")

    # The same push, spelled the way it was before `server push` existed. The
    # status line spawns the module rather than either, so both are for people.
    pp = sub.add_parser("push", help="Push this machine's records to a ccreport server")
    pp.add_argument("--config", help="Read somewhere other than ~/.config/ccreport/push.toml")
    pp.add_argument("--server", help="Only this server URL, as push.toml spells it")
    pp.add_argument("--full", action="store_true",
                    help="Forget the watermark and offer every file again")

    # Rate-limit utilization history, from the statusline's samples.
    par = sub.add_parser(
        "archive",
        help="Fold the purged half of the record cache into day rows")
    par.add_argument("--dry-run", "-n", action="store_true",
                     help="Print what would be folded without writing anything")
    par.add_argument("--min-age-days", type=int, default=ARCHIVE_MIN_AGE_DAYS,
                     help=f"Days a record must be behind local midnight "
                          f"(default: {ARCHIVE_MIN_AGE_DAYS}). The rate-limit "
                          f"history can push the cutoff further back, never nearer")

    pl = sub.add_parser("limits", help="Rate-limit window utilization history")
    pl.add_argument("--since", help="Start date (YYYYMMDD or YYYY-MM-DD)")
    pl.add_argument("--until", help="End date (YYYYMMDD or YYYY-MM-DD)")
    pl.add_argument("--window", "-w", choices=LIMIT_WINDOWS,
                    help="Only this window type")
    pl.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    # Default (no subcommand): every report, the account table conditionally.
    parser.add_argument("--since", help="Start date (YYYYMMDD or YYYY-MM-DD)")
    parser.add_argument("--until", help="End date (YYYYMMDD or YYYY-MM-DD)")
    parser.add_argument("--project", "-p", help="Filter by project name")
    parser.add_argument("--account", "-a", help="Filter by account email")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    parser.add_argument("--no-mva", action="store_true", help="Show NOK without 25%% MVA")
    parser.add_argument("--models", "-m", action="store_true",
                        help="Show per-model breakdown rows in the daily table")
    parser.add_argument("--all", "-A", action="store_true",
                        help="Merge what this account spent on every machine that "
                             "pushes to a configured server. Reads the local cache "
                             "alone; `ccreport server pull` is what fills it")
    parser.add_argument("--server", help="Render the merged reports from this ccreport server")
    parser.add_argument("--machine", help="With --server: only this machine's records")

    args = parser.parse_args()

    # First, and before any of the branches below can open the DB: get_connection
    # would otherwise do the same move itself, leaving this command nothing to
    # report on the run the user actually asked for it.
    if args.command == "migrate":
        cmd_migrate(args)
        return

    if args.command in ("overrides", "merge", "unmerge"):
        cmd_overrides(args)
        return
    # Reads no records and prints no report: it wants the checkout and the
    # compare API, neither of which the corpus load below has anything to add to.
    if args.command == "update":
        cmd_update(args)
        return
    # Loads records itself, bounded to the span its samples cover, so it runs
    # here rather than falling through to the report path's unbounded load and
    # the report it has no use for.
    if args.command == "limits":
        cmd_limits(args)
        return
    # Writes the cache and prints its own summary; there is no report behind it.
    if args.command == "archive":
        cmd_archive(args)
        return
    # Unlike the three above, this one loads records — its preview counts what
    # the adoption would cover — so it runs itself rather than falling through
    # to the report path, which would want a report to print.
    if args.command == "adopt":
        cmd_adopt(args)
        return

    # Reads the account log and writes it back; no report to print either.
    if args.command == "tiers":
        cmd_tiers(args)
        return
    # Reads the record cache and the usage row; it prints no report table.
    if args.command == "budget":
        cmd_budget(args)
        return
    # Reads the cache and writes only a watermark; it has no report to print.
    if args.command == "push":
        cmd_push(args)
        return
    # Reads push.toml and, apart from `server push`, writes it back.
    if args.command == "server":
        if args.server_command == "connect":
            cmd_server_connect(args)
        elif args.server_command in ("allow", "deny"):
            args.command = args.server_command
            cmd_server_allow(args)
        elif args.server_command in ("exclude", "unexclude"):
            args.command = args.server_command
            cmd_server_exclude(args)
        elif args.server_command in ("push", "sync"):
            cmd_push(args)
        elif args.server_command == "pull":
            cmd_pull(args)
        elif args.server_command == "disconnect":
            cmd_server_disconnect(args)
        else:
            cmd_server_status(args)
        return

    # Every branch below reads this machine's cache. --server reads somebody
    # else's merged one instead and never touches ours, so it turns off here
    # rather than inside the report path.
    if getattr(args, "server", None):
        cmd_server_report(args)
        return

    mva = not args.no_mva

    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    project_filter = args.project if hasattr(args, "project") else None
    account_filter = args.account if hasattr(args, "account") else None

    wants_json = bool(getattr(args, "json", False))
    # Off before the corpus load rather than after it: the request does not
    # depend on which records come back, only on which recent dates the rate
    # cache is still short of, so it runs while the records are being read.
    prefetch = exchange.start_prefetch()
    records = load_all_records(
        since=since, until=until,
        project_filter=project_filter, account_filter=account_filter,
        # A rollup row is one day of one session, so it can answer a report's
        # totals and nothing finer. Every filter selects on something it has
        # aggregated away, and --json prints one entry per API call.
        use_rollups=not (since or until or project_filter or account_filter
                         or wants_json),
    )

    # Local rows alone, always. -A merges the pulled tables below; without it
    # they answer the note under each table instead. Either way `ccreport` opens
    # no socket: these are local rows a detached pull wrote.
    remote = _remote_records(since, until, project_filter, account_filter)
    merge_all = bool(getattr(args, "all", False))
    # `local` stays this machine's own throughout. The session report reads it
    # even under -A: a pulled row is one day of one machine and carries no
    # session, so merging would add a bucket with no name to a table keyed on it.
    local = records
    note = "" if merge_all else _remote_note(remote)
    if merge_all:
        records = sorted(records + remote.records, key=lambda r: r.timestamp)

    if not records:
        print("No usage records found.", file=sys.stderr)
        sys.exit(1)

    # Bulk-load exchange rates for all records
    nok, has_full_coverage = load_rates_for_records(records, mva=mva, prefetch=prefetch)
    if nok.enabled and not has_full_coverage:
        print("⚠ Some dates lack exchange rate data; NOK values are partial.", file=sys.stderr)

    if wants_json:
        # This machine's records alone, whatever -A says: an entry here is one
        # API call, and a pulled row is a day of somebody else's.
        report_json(local, nok=nok)
        return

    command = args.command

    def _note() -> None:
        """The gap note, under a table -A could have merged."""
        if note:
            console.print(note)

    if command == "daily":
        # args.models covers `ccreport -m daily`, where -m lands on the top-level parser.
        report_daily(records, breakdown=args.breakdown or args.models, nok=nok)
        _note()
    elif command == "monthly":
        report_monthly(records, nok=nok)
        _note()
    elif command == "project":
        lim = args.limit if args.limit != 0 else None
        report_project(records, limit=lim, nok=nok)
        _note()
    elif command == "session":
        lim = args.limit if args.limit != 0 else None
        report_session(local, limit=lim, nok=nok)
        _session_note(merge_all, remote)
    elif command == "account":
        report_account(records, nok=nok)
        _note()
    else:
        report_daily(records, breakdown=args.models, nok=nok)
        _note()
        report_monthly(records, nok=nok)
        _note()
        report_project(records, nok=nok)
        _note()
        report_session(local, nok=nok)
        _session_note(merge_all, remote)
        # Trails the rest, and only once there is a split to show. Decided from
        # the records already in hand, so a single-account machine — which is
        # most of them — pays nothing for the check.
        if _accounts_worth_showing(records):
            report_account(records, nok=nok)


if __name__ == "__main__":
    main()
