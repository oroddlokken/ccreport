#!/usr/bin/env bash
# Wrapper Claude Code's settings.json points at. Segment defaults live in
# src/ccreport/statusline.py; per-machine CLAUDE_STATUSLINE_* overrides go here.

export CLAUDE_STATUSLINE_SCOPED_THRESHOLD="${CLAUDE_STATUSLINE_SCOPED_THRESHOLD:-0}"
export CLAUDE_STATUSLINE_SCOPED_MODE="${CLAUDE_STATUSLINE_SCOPED_MODE:-current}"

export CLAUDE_STATUSLINE_CHANGES="${CLAUDE_STATUSLINE_CHANGES:-0}"
export CLAUDE_STATUSLINE_GIT_DIFFSTAT="${CLAUDE_STATUSLINE_GIT_DIFFSTAT:-0}"
export CLAUDE_STATUSLINE_DOGCAT="${CLAUDE_STATUSLINE_DOGCAT:-0}"

export CLAUDE_STATUSLINE_USER="${CLAUDE_STATUSLINE_USER:-0}"
export CLAUDE_STATUSLINE_ORG="${CLAUDE_STATUSLINE_ORG:-0}"

export CLAUDE_STATUSLINE_BATTERY="${CLAUDE_STATUSLINE_BATTERY:-0}"
export CLAUDE_STATUSLINE_RENDER_TIME="${CLAUDE_STATUSLINE_RENDER_TIME:-0}"

# ${var%/*} leaves a slashless path untouched, where dirname would answer "."
BIN="${BASH_SOURCE[0]%/*}"
[[ "$BIN" == "${BASH_SOURCE[0]}" ]] && BIN="."
REPO="$BIN/.."
SRC="$REPO/src"
PY="$REPO/.venv/bin/python"

# Two layouts. A checkout has this file in bin/ beside src/, with the project
# venv beside both; a wheel install has it in the package's own scripts/ dir,
# where the import root is the directory holding the package.
if [[ ! -d "$SRC/ccreport" ]]; then
  SRC="$REPO/.."
  # The interpreter that owns the package, at <prefix>/bin — three levels above
  # site-packages in a venv, and the walk covers lib64 and the flat layouts.
  PY=""
  probe="$SRC"
  for _ in 1 2 3 4; do
    probe="$probe/.."
    if [[ -x "$probe/bin/python3" ]]; then
      PY="$probe/bin/python3"
      break
    fi
  done
fi

# CPython writes and reads __pycache__ only for modules it imports, never for
# the file named on its command line: run as a script, the whole module is
# tokenized and compiled again on every render, before main() even reaches the
# fast-path check. Importing it instead caches the bytecode and leaves only
# this one-line -c string to compile. It pops the directory back off argv so
# the module still sees `-t` at sys.argv[1] the way a script invocation did.
BOOT='import sys; sys.path.insert(0, sys.argv.pop(1)); from ccreport import statusline; statusline.main()'

# The render and the refresh it spawns import nothing outside the stdlib, which
# is what lets this wrapper skip `uv run`: the resolver costs ~40 ms of every
# render and would answer with the interpreter picked above anyway.
# Last resort: a python3 off PATH can be older than this package needs, and the
# environment it was installed into is not always on PATH.
[[ -x "$PY" ]] || PY="$(command -v python3)"
if [[ ! -x "$PY" ]]; then
  # No output at all rather than an error line: this is a status bar, and a
  # message here would sit under every prompt until the machine is fixed.
  exit 0
fi

# The render-time segment's second figure: time the whole Python invocation
# from out here — no in-process clock can see its own startup and exit — and
# substitute the result over the token the render embeds. Exporting the token
# is also the render's go-ahead to embed it. $EPOCHREALTIME is bash 5 and has
# exactly six fractional digits, so stripping the separator (locale may make
# it a comma) gives integer microseconds; under an older bash it expands
# empty and the segment shows the in-process time alone.
#
# Timing costs a subshell and a command substitution, so it is off by default.
# `!= 0` matches the render's own _on().
if [[ "$CLAUDE_STATUSLINE_RENDER_TIME" == 0 || -z "$EPOCHREALTIME" ]]; then
  exec "$PY" -c "$BOOT" "$SRC" "$@"
fi
export CLAUDE_STATUSLINE_TOTAL_TOKEN="__SL_TOTAL__"
t0=${EPOCHREALTIME/[.,]/}
out="$("$PY" -c "$BOOT" "$SRC" "$@")"
us=$(( ${EPOCHREALTIME/[.,]/} - t0 ))
printf -v dt '%d.%03ds' $(( us / 1000000 )) $(( us % 1000000 / 1000 ))
# An empty render (e.g. unparsable stdin) must stay empty — an empty *line*
# would render as a blank status row where no output means none at all.
[[ -n "$out" ]] && printf '%s\n' "${out//__SL_TOTAL__/$dt}"
