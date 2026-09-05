#!/usr/bin/env bash
# Wrapper the UserPromptSubmit and PreToolUse hooks both point at. The verdict
# is the same for either event; only which turn it halts differs.

# The whole arming gate: with CCQUOTA_STOP unset this exits before any
# interpreter starts, so an unarmed session pays one bash spawn per prompt.
[[ -n ${CCQUOTA_STOP:-} ]] || exit 0

set -euo pipefail

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
  # The walk starts only from a directory that holds the package, because five
  # levels above a shallow path is /, whose python3 cannot import ccreport and
  # exits 1 into a hook that reads 1 as a block.
  if [[ -d "$SRC/ccreport" ]]; then
    probe="$SRC"
    for _ in 1 2 3 4; do
      probe="$probe/.."
      if [[ -x "$probe/bin/python3" ]]; then
        PY="$probe/bin/python3"
        break
      fi
    done
  fi
fi

# Imported rather than run as a script, for the reason statusline-command_x.sh
# does it: CPython caches bytecode only for modules it imports, and this runs
# before every tool call.
BOOT='import sys; sys.path.insert(0, sys.argv.pop(1)); from ccreport import quota_guard; quota_guard.main()'

# Last resort: a python3 off PATH can be older than this package needs, and the
# environment it was installed into is not always on PATH.
[[ -x "$PY" ]] || PY="$(command -v python3)"
# No interpreter is not a reason to block a session: the guard is advisory
# machinery, and a machine without python3 has no readings to guard with.
[[ -x "$PY" ]] || exit 0

exec "$PY" -c "$BOOT" "$SRC"
