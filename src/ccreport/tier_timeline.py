"""A declared history of which plan an account was on, and when it changed.

`account_events` records a tier only when a status-line render happened to
notice one, so a machine whose capture started after its first record has no
tier for most of its corpus, and a server has none at all for anything pushed
before a client learned to send one. The billing receipts do have it: each
names the line item in force for the period it covers, and a mid-cycle upgrade
bills a prorated line the day it takes effect, so the receipt is both the date
and the plan.

This module is the format those dates are typed in and the lookup that answers
a moment with them. It is here rather than in the CLI because both ends read
the same text — the client writes `account_events` rows from it, the server
stores it per account and resolves a record's tier at read time — and it
imports nothing beyond the stdlib for the same reason `protocol.py` does: the
push client must stay clear of rich and the server is on the other side of that
line.

Nothing validates a tier string against a list. The names come from Anthropic
and change without notice; a backfill that refused an unrecognized one would
fail on the plan it was written to record.
"""

from __future__ import annotations

import bisect
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import NoReturn

TIER_FIELDS = ("seat_tier", "user_rate_limit_tier", "organization_rate_limit_tier")

_TABLE = "tier"


def _reject(message: str) -> NoReturn:
    """Refuse the file.

    One exception type for every way the text can be wrong, including a field
    of the wrong type: the callers catch what a bad file raises alongside what
    a missing one does, and a second type there would only ever be re-raised
    with the same message.
    """
    raise ValueError(message)


@dataclass(frozen=True)
class Entry:
    """One plan change: the moment it took effect, and what it took effect as.

    `account` is whatever the file named — a uuid or a login email. Resolving
    it against a real account is the caller's job, because only the caller
    knows which log to resolve it against.
    """

    ts: float
    account: str
    seat_tier: str | None = None
    user_rate_limit_tier: str | None = None
    organization_rate_limit_tier: str | None = None

    def tiers(self) -> dict[str, str | None]:
        """The three tier fields as a dict, in TIER_FIELDS order."""
        return {f: getattr(self, f) for f in TIER_FIELDS}


def _as_epoch(value: object, where: str) -> float:
    """A TOML `at` value as an epoch, or a ValueError naming what was wrong.

    A bare date is midnight UTC, which is what a PDF invoice can honestly say:
    it carries the day the plan changed and no clock time. A naive datetime is
    read as UTC too, so a file that omits the offset means one thing rather
    than following the zone of whichever machine applies it.
    """
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
    _reject(f"{where}: 'at' must be a TOML date or datetime, not {type(value).__name__}")


def _as_tier(value: object, where: str, field: str) -> str | None:
    """One tier field, with an empty string read as absent rather than stored."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        _reject(f"{where}: '{field}' must be a string, not {type(value).__name__}")
    return value


def parse(text: str) -> list[Entry]:
    """The entries *text* declares, oldest first.

    Raises ValueError on anything it cannot read. A person typed this file to
    correct their own history, so a silently dropped line would leave a gap
    that looks exactly like a plan they never had — the opposite of what they
    sat down to fix.

    Two entries at the same instant for the same account is an error rather
    than a last-one-wins: they contradict each other, and picking one would be
    a guess about which receipt was misread.
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        msg = f"not readable as TOML: {e}"
        raise ValueError(msg) from e

    raw = doc.get(_TABLE, [])
    if not isinstance(raw, list):
        _reject(f"'{_TABLE}' must be a list of tables, written [[{_TABLE}]]")

    entries: list[Entry] = []
    for i, item in enumerate(raw, start=1):
        where = f"[[{_TABLE}]] #{i}"
        if not isinstance(item, dict):
            _reject(f"{where}: expected a table")
        unknown = set(item) - {"at", "account", *TIER_FIELDS}
        if unknown:
            _reject(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        if "at" not in item:
            _reject(f"{where}: missing 'at'")
        account = item.get("account")
        if not isinstance(account, str) or not account:
            _reject(f"{where}: 'account' must be a non-empty uuid or login email")
        entries.append(Entry(
            ts=_as_epoch(item["at"], where),
            account=account,
            **{f: _as_tier(item.get(f), where, f) for f in TIER_FIELDS},
        ))

    seen: set[tuple[str, float]] = set()
    for e in entries:
        key = (e.account, e.ts)
        if key in seen:
            when = datetime.fromtimestamp(e.ts, tz=UTC).isoformat()
            _reject(f"two entries for {e.account} at {when}")
        seen.add(key)
    return sorted(entries, key=lambda e: (e.account, e.ts))


def render(entries: list[Entry]) -> str:
    """*entries* as the TOML `parse` reads, oldest first.

    So a stored timeline comes back into the box it was typed in and a person
    correcting one receipt edits a line rather than retyping the file. Times go
    out in UTC with the offset spelled, which is what they were read as; a
    field nothing set is left out rather than written empty, so the document
    stays as short as what it says.
    """
    blocks = []
    for e in sorted(entries, key=lambda x: (x.ts, x.account)):
        when = datetime.fromtimestamp(e.ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [f"[[{_TABLE}]]", f"at = {when}", f'account = "{e.account}"']
        lines += [f'{f} = "{v}"' for f in TIER_FIELDS if (v := getattr(e, f))]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def effective_tier(tiers: dict[str, str | None]) -> str | None:
    """The bucket *tiers* draws against: the per-user one, then the org pool.

    `cache_db.effective_limit_tier` spelled for a plain dict, so the server can
    answer the same question without importing a module that opens a cache the
    server does not have.
    """
    return tiers.get("user_rate_limit_tier") or tiers.get("organization_rate_limit_tier")


class TierTimeline:
    """Which tier an account was on at a given moment, by declared entry.

    One instance covers every account, keyed by whatever string the entries
    were resolved to, because the server folds records for all of them in one
    pass. A moment older than an account's first entry has no tier: the
    declaration starts where the receipts do, and what ran before them is not
    recorded anywhere either.
    """

    def __init__(self, entries: list[Entry]) -> None:
        self._ts: dict[str, list[float]] = {}
        self._tiers: dict[str, list[str | None]] = {}
        # The whole tier set beside the resolved one. A report groups on the
        # bucket an account drew against; a price is what the seat or the plan
        # behind that bucket cost, and the two are not the same field.
        self._sets: dict[str, list[dict[str, str | None]]] = {}
        for e in sorted(entries, key=lambda x: (x.account, x.ts)):
            tiers = e.tiers()
            self._ts.setdefault(e.account, []).append(e.ts)
            self._tiers.setdefault(e.account, []).append(effective_tier(tiers))
            self._sets.setdefault(e.account, []).append(tiers)

    def __bool__(self) -> bool:
        return bool(self._ts)

    def at(self, account: str, ts: float) -> str | None:
        """The effective tier for *account* at *ts*, None where none is declared."""
        stamps = self._ts.get(account)
        if not stamps:
            return None
        i = bisect.bisect_right(stamps, ts) - 1
        return self._tiers[account][i] if i >= 0 else None

    def stretches(
        self, account: str, start: float, end: float,
    ) -> list[tuple[float, float, dict[str, str | None] | None]]:
        """*account*'s plan spans clipped to *start*..*end*, as (from, to, tiers).

        What a caller needs to price a month the way it was billed: a plan
        changed mid-cycle bills prorated, so the answer is not one tier but how
        long each was in force. Contiguous and gapless, so the lengths sum to
        the span — a stretch before the first declared entry carries None
        rather than being dropped, which is what keeps an unpriced opening
        visible instead of quietly shortening the month.

        All three tier fields travel, not the resolved one: a team seat and a
        personal plan can land on the same rate-limit bucket and cost different
        money, so the field that prices a stretch is not the field that groups
        it.

        Pricing is not done here: this module stays free of every import but
        the stdlib, and the prices are `pricing.py`'s to hold.
        """
        if end <= start:
            return []
        stamps = self._ts.get(account)
        if not stamps:
            return [(start, end, None)]
        tiers = self._sets[account]
        # Every boundary inside the span, plus the span's own ends. bisect_left
        # so an entry landing exactly on `start` opens the first stretch rather
        # than adding a zero-length one before it.
        cuts = [start, *(t for t in stamps if start < t < end), end]
        out = []
        for from_ts, to_ts in pairwise(cuts):
            i = bisect.bisect_right(stamps, from_ts) - 1
            out.append((from_ts, to_ts, tiers[i] if i >= 0 else None))
        return out
