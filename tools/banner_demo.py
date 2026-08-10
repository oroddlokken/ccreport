#!/usr/bin/env python3
"""Render real examples of every banner / colored token src/ccreport/statusline.py uses.

Run this whenever you tweak banner colors to eyeball every variant in one shot.
Self-contained: ANSI codes are hardcoded to mirror statusline.py — keep the two
in sync when colors change.
"""
import re

RST = "\033[0m"
SUBDUED = "\033[38;5;242m"
_ANSI = re.compile(r"\033\[[0-9;]*m")

DESC_COL = 14  # column where the description text starts


def row(sample: str, desc: str) -> str:
    visible = len(_ANSI.sub("", sample))
    pad = max(1, DESC_COL - visible)
    return f"  {sample}{' ' * pad}{desc}"


print("Model banners (label is the full display_name, upper-cased):")
print(row(f"\033[1;97;48;5;93m OPUS 5 {RST}",             "Opus — deep purple bg"))
print(row(f"\033[1;97;44m SONNET 5 {RST}",                "Sonnet — blue bg"))
print(row(f"\033[1;97;48;5;28m FABLE 5 {RST}",            "Fable — green bg"))
print(row(f"\033[1;97;45m HAIKU 4.5 {RST}",               "Haiku, HAIKU_RED=0 — magenta bg"))
print(row(f"\033[1;97;48;5;196m HAIKU 4.5 {RST}",         "Haiku, HAIKU_RED=1 — bright pure-red bg"))
print(row(f"\033[1;97;100m MYSTERY 9 {RST}",              "unknown family — grey fallback"))
print()

print("Rate-limit segments:")
print(row(f"\033[0;90mS:\033[0;90m24%{RST}",              "under 65% — dim"))
print(row(f"\033[0;90mW:\033[0;33m70%{RST}",              "65-84% — yellow"))
print(row(f"\033[0;90mFa:\033[0;31m90%{RST}",             "85%+ — red (scoped per-model limit)"))
print(row(f"{SUBDUED}TTL:9m59s{RST}",                     "next fetch due (or heartbeat when skipped)"))
print(row("\033[0;31mstale:25m\033[0m",                   "fetch wanted but overdue"))
print()

print("Combined examples (line 1 of wide layout):")
DOT = f"{SUBDUED} · {RST}"
TOP = (
    f"{SUBDUED}01:14{RST} \033[0;34mccreport/src{RST} "
    f"\033[0;33mmain{RST}"
)

# The badge sits where the plain model name used to, ahead of the effort level.
opus = f"\033[1;97;48;5;93m OPUS 5 {RST}"
fable = f"\033[1;97;48;5;28m FABLE 5 {RST}"
print(
    f"  {TOP}{DOT}{opus} {SUBDUED}Extra 435k/967k{RST}"
    f"\033[0;90m:45%{RST}"
)
print(
    f"  {TOP}{DOT}{fable} {SUBDUED}High 561k/967k{RST}"
    f"\033[0;90m:\033[0;33m58%{RST}"
)

# When HAIKU_RED=1, _force_red strips inner ANSI across the whole line and
# rewraps it bright red, so every inner color collapses to plain text. The badge
# is stashed out of that pass, so its background still reads.
haiku = f"\033[1;97;48;5;196m HAIKU 4.5 {RST}"
red = "\033[1;91m"
print(
    f"  {red}01:14 ccreport/src main[+2-1] · {RST}{haiku}"
    f"{red} CH:98% 12k/167k:7%{RST}   (HAIKU_RED=1 collapses inner colors)"
)
