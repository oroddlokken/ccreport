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

# Imported rather than run as a script, for the reason statusline-command_x.sh
# does it: CPython caches bytecode only for modules it imports, and this runs
# before every tool call.
BOOT='import sys; sys.path.insert(0, sys.argv.pop(1)); from ccreport import quota_guard; quota_guard.main()'

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
# No interpreter is not a reason to block a session: the guard is advisory
# machinery, and a machine without python3 has no readings to guard with.
[[ -x "$PY" ]] || exit 0

exec "$PY" -c "$BOOT" "$SRC"
