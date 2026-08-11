#!/usr/bin/env bash
set -euo pipefail
# Deletes the statusline's per-session memo, whose --dangerously-skip-permissions
# verdict comes from the argv of the claude process that launched the session id.
# `--resume` reuses that id under new argv, and startup is the only moment it can
# change.

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null || true)
[[ -n $sid ]] || exit 0
# Must match _session_state_path in ../src/ccreport/statusline.py; drift deletes
# nothing, silently. tests/test_statusline.py asserts the two agree.
sid=${sid//[^A-Za-z0-9-]/_}
rm -f "${TMPDIR:-/tmp}/claude-statusline-$(id -u)-${sid:0:64}.memo.json"
