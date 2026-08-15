"""The corpus the golden report tests render, and how they render it.

Imported by tests/test_reports.py and by the throwaway script that captured the
golden files from the pre-split code, so both sides render the same records the
same way. Costs are stated on every record rather than computed, which keeps the
golden files answering "did the split change the output" instead of "did prices
move".
"""

from __future__ import annotations

import datetime as dt
import io

FROZEN_NOW = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)
"""What the monthly report's projection is computed against.

Mid-month, so both PROJECTED lines render: day 15 of 31, with the trailing
14-day window reaching back into the same month.
"""

WIDE = 200
NARROW = 80
"""Console widths. The reports have two layouts and NARROW_WIDTH is 100."""


def _ts(month: int, day: int, hour: int) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, 0, tzinfo=dt.UTC)


RECORDS_SPEC = [
    ("m1", "claude-opus-4-5-20260101", (12000, 3400, 90000, 410000),
     _ts(2, 3, 9), "sess-alpha", "ccr-projA", 4.5, "me@work.example", 1),
    ("m2", "claude-sonnet-4-5-20260101", (8000, 1200, 40000, 220000),
     _ts(2, 3, 14), "sess-alpha", "ccr-projA", 0.75, "me@work.example", 1),
    ("m3", "claude-haiku-4-5", (500, 90, 0, 1200),
     _ts(2, 17, 11), "sess-beta", "ccr-projB", 0.04, "me@work.example", 1),
    # count > 1: what a rollup row deserializes to, so the Calls column adds
    # rather than counts.
    ("m4", "claude-opus-4-5-20260101", (60000, 9000, 300000, 1800000),
     _ts(2, 24, 16), "sess-beta", "ccr-projB", 23.5, "me@work.example", 37),
    ("m5", "claude-opus-4-5-20260101", (21000, 5100, 120000, 640000),
     _ts(3, 2, 10), "sess-gamma", "ccr-projA", 9.96, "me@home.example", 1),
    # Inside the trailing window, which is what makes the second PROJECTED row
    # render at all.
    ("m6", "claude-sonnet-4-5-20260101", (3000, 800, 15000, 70000),
     _ts(3, 9, 8), "sess-gamma", "ccr-projA", 0.42, "me@home.example", 1),
    ("m7", "claude-haiku-4-5", (900, 150, 2000, 9000),
     _ts(3, 9, 20), "sess-delta", "ccr-projC", 0.09, "me@home.example", 1),
    # <synthetic> is excluded from the Models column and from nothing else.
    ("m8", "<synthetic>", (0, 0, 0, 0),
     _ts(3, 12, 13), "sess-delta", "ccr-projC", 0.0, "me@home.example", 1),
    ("m9", "claude-opus-4-5-20260101", (15000, 2200, 80000, 300000),
     _ts(3, 14, 17), "sess-epsilon", "ccr-projD", 6.2, "unknown", 1),
    ("m10", "claude-sonnet-4-5-20260101", (700, 110, 3000, 12000),
     _ts(3, 15, 9), "sess-epsilon", "ccr-projD", 0.11, "unknown", 1),
]

"""Each entry is message id, model, token counts, timestamp, session, project,
USD cost, account and call count, in the order build_records unpacks them."""

RATES = {
    "2026-02-03": 10.10, "2026-02-17": 10.42, "2026-02-24": 10.55,
    "2026-03-02": 10.61, "2026-03-09": 10.73, "2026-03-12": 10.80,
    "2026-03-14": 10.88, "2026-03-15": 10.91,
}
"""One rate per Oslo date the corpus touches, each distinct so a record
converted under the wrong date lands on a visibly wrong number."""


def build_records(module):
    """The corpus, built through *module*'s own record classes.

    *module* is ccreport.aggregate now and was ccreport.ccreport before the
    split, which is the only reason this takes one at all.
    """
    records = []
    for mid, model, tokens, ts, sid, project, cost, account, count in RECORDS_SPEC:
        tin, tout, tcc, tcr = tokens
        records.append(module.UsageRecord(
            message_id=mid,
            model=model,
            tokens=module.TokenCounts(input=tin, output=tout, cache_create=tcc, cache_read=tcr),
            timestamp=ts,
            session_id=sid,
            project=project,
            cost_usd=cost,
            dedup_key=f"{mid}:req",
            account=account,
            count=count,
        ))
    return records


class FrozenDatetime(dt.datetime):
    """A datetime whose now() is FROZEN_NOW, so the projection never drifts."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


def render_all(ccreport_module, records, nok, width: int) -> str:
    """Every report a bare `ccreport` prints, plus the limited variants.

    One string per width rather than a file per report: the reports are read
    together and a diff that spans two of them is easier to read as one.
    """
    from rich.console import Console

    buf = io.StringIO()
    previous = ccreport_module.console
    ccreport_module.console = Console(file=buf, width=width, no_color=True)
    try:
        ccreport_module.report_daily(records, breakdown=True, nok=nok)
        ccreport_module.report_daily(records, breakdown=False, nok=nok)
        ccreport_module.report_monthly(records, nok=nok)
        ccreport_module.report_project(records, limit=None, nok=nok)
        ccreport_module.report_project(records, limit=2, nok=nok)
        ccreport_module.report_session(records, limit=None, nok=nok)
        ccreport_module.report_session(records, limit=2, nok=nok)
        ccreport_module.report_account(records, nok=nok)
    finally:
        ccreport_module.console = previous
    return buf.getvalue()
