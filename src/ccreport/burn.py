"""When a rate-limit window runs out at the rate it is currently filling.

`ccu` compares usage against the clock and stops there. What it does not say is
when the quota actually runs out, which is the question people open a usage
monitor to answer.

The input is `rate_limit_snapshots`, appended by slow-path status line renders
from the live percentages. So the answer is only ever as good as the sampling:
a window nobody was working through has no history, and one with a single
reading supports no slope at all. Both print nothing here rather than a number
that will be visibly wrong five minutes later.

Stdlib only, and small: the status line reads this on its slow path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_SAMPLES = 2
"""Below this there is no slope. One reading is a point, not a rate."""

MIN_SLOPE_PP_PER_S = 1e-9
"""A slope at or under this is flat or falling, and projects to no exhaustion.

Falling happens: a window's percentage is the server's, and a correction or a
rounding step down is not a reason to announce anything."""

HALF_LIFE_S = 1800.0
"""How fast an older sample stops counting. Half an hour, so the last stretch
of work decides the answer and the lull before lunch does not."""

PARTIAL_OPENING_PP = 5.0
"""How full a window may already be at its first sample before the answer says
the sampling started late. Capture begins at a render, so a point or two of lag
is ordinary; past this, the rise before it was never observed."""


@dataclass(frozen=True)
class Burn:
    """A window's fill rate and what it projects to."""

    rate_pp_per_s: float
    """Percentage points per second, fitted over the samples of this instance."""
    exhausts_at: float | None
    """When 100% is reached at that rate, or None if the window resets first."""
    resets_at: float
    partial: bool
    """Whether the window was already filling before the first sample.

    The slope is fitted over observed samples alone and never across the gap,
    so the projection stands; what it cannot say is how the unobserved part
    compared."""

    @property
    def before_reset(self) -> bool:
        return self.exhausts_at is not None


def _weighted_slope(points: list[tuple[float, float]]) -> float:
    """Percentage points per second, weighting recent samples more heavily.

    Weighted least squares with an exponential decay on age. A plain fit gives
    the morning the same say as the last ten minutes, and the question is about
    the rate right now.
    """
    newest = points[-1][0]
    weights = [math.exp(-(newest - ts) * math.log(2) / HALF_LIFE_S) for ts, _ in points]
    total = sum(weights)
    if total <= 0:
        return 0.0
    mean_t = sum(w * ts for w, (ts, _) in zip(weights, points, strict=True)) / total
    mean_p = sum(w * pct for w, (_, pct) in zip(weights, points, strict=True)) / total
    numerator = sum(
        w * (ts - mean_t) * (pct - mean_p)
        for w, (ts, pct) in zip(weights, points, strict=True)
    )
    denominator = sum(
        w * (ts - mean_t) ** 2 for w, (ts, _) in zip(weights, points, strict=True)
    )
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def project(samples: list[dict], now: float) -> Burn | None:
    """Where one window instance's samples are heading, or None to say nothing.

    *samples* are the readings of a single instance — rows sharing a resets_at —
    oldest first, as load_rate_limit_snapshots returns them.

    None on every case where an answer would be invented: fewer than two
    samples, a flat or falling slope, a window already at 100%, or one whose
    reset has passed.
    """
    if len(samples) < MIN_SAMPLES:
        return None
    resets_at = float(samples[-1]["resets_at"])
    if resets_at <= now:
        return None
    latest_pct = float(samples[-1]["used_pct"])
    if latest_pct >= 100:
        return None

    points = [(float(s["ts"]), float(s["used_pct"])) for s in samples]
    slope = _weighted_slope(points)
    if slope <= MIN_SLOPE_PP_PER_S:
        return None

    # Anchored at the last reading rather than at now: the gap since it is time
    # nobody sampled, and assuming the window kept filling through it would
    # move the answer earlier on exactly the machine that stopped working.
    eta = points[-1][0] + (100.0 - latest_pct) / slope
    partial = points[0][1] > PARTIAL_OPENING_PP
    return Burn(
        rate_pp_per_s=slope,
        exhausts_at=eta if eta < resets_at else None,
        resets_at=resets_at,
        partial=partial,
    )


def current_instance(samples: list[dict], window: str, now: float,
                     model: str | None = None) -> list[dict]:
    """The samples of the window instance that is running right now.

    Grouped by resets_at, the way `ccreport limits` groups them: rows sharing
    one are readings of the same quota filling up. The instance whose reset is
    still ahead is the current one.
    """
    live = [
        s for s in samples
        if s["window"] == window
        and (model is None or s.get("model") == model)
        and s.get("resets_at")
        and float(s["resets_at"]) > now
    ]
    if not live:
        return []
    latest_reset = max(float(s["resets_at"]) for s in live)
    return [s for s in live if float(s["resets_at"]) == latest_reset]


def span(seconds: float) -> str:
    """A duration as the coarsest two units that describe it: "2h 10m"."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def describe(burn: Burn, now: float, clock) -> str:
    """One sentence: when it runs out, or that the window resets first.

    *clock* formats an epoch as a local time, so this module needs no zone of
    its own and each surface keeps its own format.
    """
    late = " (sampled late)" if burn.partial else ""
    if burn.exhausts_at is None:
        return f"At this rate the window resets before the quota runs out{late}"
    ahead = span(burn.exhausts_at - now)
    spare = span(burn.resets_at - burn.exhausts_at)
    return (
        f"At this rate 100% lands in {ahead}, at {clock(burn.exhausts_at)} — "
        f"{spare} before the reset{late}"
    )
