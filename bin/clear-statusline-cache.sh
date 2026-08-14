#!/usr/bin/env bash
set -euo pipefail
# Deletes the statusline's per-session fetch cache, which holds the sbx/!sbx badge
# and the segments the CLAUDE_CODE_STATUSLINE_* toggles switch on for FAST_TTL_S.
# Both are read out of settings files, so a ConfigChange leaves them stale for up
# to that TTL. The .memo.json file beside it is left alone: its DSP verdict comes
# from argv and its ~/.claude.json stamp is invalidated by that file itself.

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null || true)
[[ -n $sid ]] || exit 0
# Must match _fast_cache_path in ../src/ccreport/statusline.py; drift deletes
# nothing, silently. tests/test_statusline.py asserts the two agree.
sid=${sid//[^A-Za-z0-9-]/_}
# An unremovable file costs the next render nothing; exit 2 would block the
# config change itself, which is not this hook's business.
rm -f "${TMPDIR:-/tmp}/claude-statusline-$(id -u)-${sid:0:64}.json" || true
