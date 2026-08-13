"""Which Claude account a record billed to, from the account_events change log.

A session JSONL names no account and ~/.claude.json holds only the current
login, so this timeline is the only thing that can attribute a historic record.
It lives apart from ccreport.py because the push client answers the same
question in a detached process, and importing the CLI there would pull rich
into it.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime

from ccreport.aggregate import UNKNOWN_ACCOUNT
from ccreport.cache_db import effective_limit_tier


def _account_labels(events: list[dict]) -> list[str]:
    """The display label for each event, in the order given.

    An email is the label, because that is what the person recognizes. The same
    address can bill through more than one organization — a work login and a
    personal one — and those are separate accounts that must not share a
    bucket, so an email seen under more than one organization carries the
    organization name too. An event with no email falls back to its uuid, which
    is the only field guaranteed to be there.
    """
    orgs: dict[str, set[str]] = defaultdict(set)
    for e in events:
        if e["email"]:
            orgs[e["email"]].add(e["organization_name"] or "")
    labels = []
    for e in events:
        email = e["email"]
        if not email:
            labels.append(e["account_uuid"])
        elif len(orgs[email]) > 1 and e["organization_name"]:
            labels.append(f"{email} ({e['organization_name']})")
        else:
            labels.append(email)
    return labels



class AccountTimeline:
    """Which Claude account was signed in at a given moment, and on which tier.

    Built from the append-only account_events log the statusline writes. The
    log holds wall-clock capture times as epoch seconds, and a record's
    timestamp is timezone-aware, so both sides of the lookup compare as epochs
    and neither depends on the local zone.
    """

    def __init__(self, events: list[dict]) -> None:
        self._ts = [e["ts"] for e in events]
        self._labels = _account_labels(events)
        # Resolved once here rather than per lookup: both answers come off the
        # same event, so the two getters differ only in which list they index.
        self._tiers = [effective_limit_tier(e) for e in events]
        self._uuids = [e["account_uuid"] for e in events]

    def _index_at(self, when: datetime) -> int:
        """Position of the event in force at *when*, or -1 when none is.

        A moment older than the first captured event has no event: the log
        starts when capture was switched on, and what ran before it is
        genuinely not recorded anywhere.
        """
        return bisect.bisect_right(self._ts, when.timestamp()) - 1

    def label_at(self, when: datetime) -> str:
        """The account in force at *when*, "unknown" before the first event."""
        i = self._index_at(when)
        return self._labels[i] if i >= 0 else UNKNOWN_ACCOUNT

    def tier_at(self, when: datetime) -> str | None:
        """The effective rate-limit tier at *when*, None where it is unrecorded.

        None covers both "no event yet" and "an event that predates the tier
        columns" — neither is a tier reading, and a report has to show them as
        absent rather than as a change to something.
        """
        i = self._index_at(when)
        return self._tiers[i] if i >= 0 else None

    def uuid_at(self, when: datetime) -> str | None:
        """The account uuid in force at *when*, None before the first event.

        The stable key, where label_at gives what a person reads. The push
        client sends both: the label is what a report shows and can change
        with an email, the uuid is what the server groups on.
        """
        i = self._index_at(when)
        return self._uuids[i] if i >= 0 else None
