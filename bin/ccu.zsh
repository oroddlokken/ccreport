#!/usr/bin/env zsh
# Claude Code Usage — terminal dashboard similar to the /usage screen.
# Runs ccreport.usage_api and renders progress bars with reset countdowns.
#
# AUDIT: All calculations are documented in docs/calculation-reference.md.
#        When changing any calculation, caching, or data format here,
#        update that document to match.
#
# Usage: ccu.zsh [--force|-f] [--json]
#        --json prints the raw usage API response instead of the dashboard.

set -euo pipefail

local REPO="${0:A:h}/.."

# --- Fetch usage JSON ---

local force_flag="" json_only=0
local arg
for arg in "$@"; do
  case "$arg" in
    --force|-f) force_flag="--force" ;;
    --json)     json_only=1 ;;
    *) echo "Usage: ccu [--force|-f] [--json]" >&2; exit 2 ;;
  esac
done

# usage_api imports nothing outside the stdlib, so any interpreter runs it and
# the project venv is only a preference.
local py="$REPO/.venv/bin/python"
if [[ ! -x "$py" ]]; then
  py=python3
  local p
  for p in python3.14 python3.13 python3.12; do
    command -v "$p" >/dev/null 2>&1 && py="$p" && break
  done
fi

# -m, not a path: run as a script the module would be __main__ with the package
# unimported, and its own `from ccreport.cache_db import ...` would fail.
local -a usage_cmd=("$py" -m ccreport.usage_api)
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

# --json is a passthrough of the API body — no cache, no rendering.
if (( json_only )); then
  exec "${usage_cmd[@]}" --raw
fi

local json
json=$("${usage_cmd[@]}" $force_flag 2>/dev/null) || true
if [[ -z "$json" ]]; then
  echo "Failed to fetch usage data" >&2
  exit 1
fi

# --- Parse fields ---

local jq_out
jq_out=$(echo "$json" | jq -r '[
  (.session_percent // ""),
  (.week_percent // ""),
  (.sonnet_percent // ""),
  (.scoped_percent // ""),
  (.scoped_model // ""),
  (.extra_percent // ""),
  (.extra_spent // ""),
  (.extra_limit // ""),
  (.session_reset // ""),
  (.week_reset // ""),
  (.sonnet_reset // ""),
  (.scoped_reset // ""),
  (.extra_reset // ""),
  (.last_updated // "")
] | join("\u001f")')
local s_pct w_pct so_pct sc_pct sc_model e_pct e_spent e_limit s_reset w_reset so_reset sc_reset e_reset last_updated
IFS=$'\x1f' read -r s_pct w_pct so_pct sc_pct sc_model e_pct e_spent e_limit s_reset w_reset so_reset sc_reset e_reset last_updated <<< "$jq_out"

if [[ -z "$s_pct" && -z "$w_pct" ]]; then
  echo "No usage data available" >&2
  exit 1
fi

# --- Timezone ---

local tz_name
tz_name=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)
[[ -z "$tz_name" ]] && tz_name=$(date +%Z)

# --- Helpers ---

# Parse ISO 8601 timestamp to epoch, respecting timezone offset.
ccu_iso_to_epoch() {
  local iso=$1
  local stripped
  stripped=$(echo "$iso" | sed 's/\.[0-9]*//')

  if [[ "$stripped" == *Z ]]; then
    local clean="${stripped%Z}"
    TZ=UTC0 date -j -f "%Y-%m-%dT%H:%M:%S" "$clean" +%s 2>/dev/null
  elif [[ "$stripped" =~ (.*T[0-9:]+)([+-])([0-9]{2}:[0-9]{2})$ ]]; then
    local clean="${match[1]}" sign="${match[2]}" offset="${match[3]}"
    # POSIX TZ inverts the sign: ISO +02:00 → TZ=UTC-02:00
    local inv="-"; [[ "$sign" == "-" ]] && inv="+"
    TZ="UTC${inv}${offset}" date -j -f "%Y-%m-%dT%H:%M:%S" "$clean" +%s 2>/dev/null
  else
    date -j -f "%Y-%m-%dT%H:%M:%S" "$stripped" +%s 2>/dev/null
  fi
}

# Progress bar: green filled, dark gray empty.
ccu_bar() {
  local pct=${1:-0} width=50
  local filled=$(( pct * width / 100 ))
  local empty=$(( width - filled ))
  printf '\033[0;32m'
  (( filled > 0 )) && printf '%.0s█' {1..$filled}
  printf '\033[0;90m'
  (( empty > 0 )) && printf '%.0s░' {1..$empty}
  printf '\033[0m'
}

# Compact countdown from epoch delta: "1d2h", "3h14m", "42m".
ccu_countdown() {
  local epoch=$1
  [[ -z "$epoch" ]] && return 0
  local now_epoch diff_s
  now_epoch=$(date +%s)
  diff_s=$(( epoch - now_epoch ))
  (( diff_s <= 0 )) && return 0

  local d=$(( diff_s / 86400 ))
  local h=$(( (diff_s % 86400) / 3600 ))
  local m=$(( (diff_s % 3600) / 60 ))

  local out=""
  if (( d > 0 )); then
    out="${d} day"
    (( d != 1 )) && out="${out}s"
    if (( h > 0 )); then
      out="${out} and ${h} hour"
      (( h != 1 )) && out="${out}s"
    fi
  elif (( h > 0 )); then
    out="${h} hour"
    (( h != 1 )) && out="${out}s"
    if (( m > 0 )); then
      out="${out} and ${m} minute"
      (( m != 1 )) && out="${out}s"
    fi
  else
    out="${m} minute"
    (( m != 1 )) && out="${out}s"
  fi
  printf '%s' "$out"
}

# Format ISO reset time: "Resets in 1d2h4m at 2am (Europe/Oslo)".
ccu_reset_fmt() {
  local iso=$1
  [[ -z "$iso" ]] && return 0
  local epoch
  epoch=$(ccu_iso_to_epoch "$iso")
  [[ -z "$epoch" ]] && return 0

  local countdown
  countdown=$(ccu_countdown "$epoch")

  local raw_h raw_m month_day
  raw_h=$(date -j -f "%s" "$epoch" "+%H" 2>/dev/null)
  raw_m=$(date -j -f "%s" "$epoch" "+%M" 2>/dev/null)
  month_day=$(date -j -f "%s" "$epoch" "+%b %-d" 2>/dev/null)

  # Midnight means no specific time was parsed — show date only.
  if [[ "$raw_h" == "00" && "$raw_m" == "00" ]]; then
    if [[ -n "$countdown" ]]; then
      printf 'Resets in %s on %s (%s)' "$countdown" "$month_day" "$tz_name"
    else
      printf 'Resets %s (%s)' "$month_day" "$tz_name"
    fi
    return
  fi

  local h ampm today tomorrow time_str
  h=$(date -j -f "%s" "$epoch" "+%-I" 2>/dev/null)
  ampm=$(date -j -f "%s" "$epoch" "+%p" 2>/dev/null | tr '[:upper:]' '[:lower:]')
  today=$(date "+%b %-d")
  tomorrow=$(date -v+1d "+%b %-d" 2>/dev/null || true)

  if [[ "$raw_m" == "00" ]]; then
    time_str="${h}${ampm}"
  else
    time_str="${h}:${raw_m}${ampm}"
  fi

  local at_part
  if [[ "$month_day" == "$today" || "$month_day" == "$tomorrow" ]]; then
    at_part="at $time_str"
  else
    at_part="at $time_str on $month_day"
  fi

  if [[ -n "$countdown" ]]; then
    printf 'Resets in %s %s (%s)' "$countdown" "$at_part" "$tz_name"
  else
    printf 'Resets %s (%s)' "$at_part" "$tz_name"
  fi
}

# Weekly pace: compare actual usage % to expected % based on time elapsed in window.
ccu_pace() {
  local actual=$1 reset_iso=$2
  [[ -z "$actual" || -z "$reset_iso" ]] && return 0
  local reset_epoch
  reset_epoch=$(ccu_iso_to_epoch "$reset_iso")
  [[ -z "$reset_epoch" ]] && return 0

  local pace_days=${CLAUDE_CODE_PACE_DAYS:-7}
  (( pace_days < 1 || pace_days > 7 )) && pace_days=7

  local now_epoch week_start elapsed_s total_s
  now_epoch=$(date +%s)
  week_start=$(( reset_epoch - 7 * 86400 ))
  elapsed_s=$(( now_epoch - week_start ))
  total_s=$(( 7 * 86400 ))

  (( elapsed_s <= 0 || elapsed_s > total_s )) && return 0

  # Expected usage: consume 100% in pace_days, not 7 (cap at 100)
  local expected=$(( elapsed_s * 100 / (pace_days * 86400) ))
  (( expected > 100 )) && expected=100
  local delta=$(( actual - expected ))

  # Elapsed time display using ccu_countdown style
  local el_d=$(( elapsed_s / 86400 ))
  local el_h=$(( (elapsed_s % 86400) / 3600 ))
  local elapsed_str=""
  if (( el_d > 0 && el_h > 0 )); then
    elapsed_str="${el_d}d ${el_h}h"
  elif (( el_d > 0 )); then
    elapsed_str="${el_d}d"
  else
    elapsed_str="${el_h}h"
  fi

  local color
  if (( delta > 15 )); then
    color="0;31"   # red — overcooking
  elif (( delta > 5 )); then
    color="0;33"   # yellow — warm
  elif (( delta >= -5 )); then
    color="0;32"   # green — on pace
  elif (( delta >= -15 )); then
    color="0;36"   # cyan — cool
  else
    color="0;90"   # dim — underusing
  fi

  local sign=""
  (( delta >= 0 )) && sign="+"
  printf '\033[0;90m%s into 7-day window (pace: %dd) — %d%% expected, \033[%sm%s%d%%\033[0m\n' \
    "$elapsed_str" "$pace_days" "$expected" "$color" "$sign" "$delta"
}

# Render one usage section: title, bar, percentage, reset info.
ccu_section() {
  local title=$1 pct=$2 reset_iso=${3:-} extra_info=${4:-}
  [[ -z "$pct" ]] && return 0
  printf '\033[1;32m%s\033[0m\n' "$title"
  ccu_bar "$pct"
  printf '  \033[1m%d%% used\033[0m\n' "$pct"
  [[ -n "$extra_info" ]] && echo "$extra_info"
  local reset_line
  reset_line=$(ccu_reset_fmt "$reset_iso")
  [[ -n "$reset_line" ]] && echo "$reset_line"
  return 0
}

# --- Render ---

if [[ -n "$last_updated" ]]; then
  local lu_epoch
  lu_epoch=$(ccu_iso_to_epoch "$last_updated") || true
  if [[ -n "$lu_epoch" ]]; then
    local ago_s=$(( $(date +%s) - lu_epoch ))
    local ago_m=$(( ago_s / 60 ))
    if (( ago_m <= 0 )); then
      printf '\033[0;90mLast fetched just now\033[0m\n'
    elif (( ago_m == 1 )); then
      printf '\033[0;90mLast fetched 1 minute ago\033[0m\n'
    else
      printf '\033[0;90mLast fetched %d minutes ago\033[0m\n' "$ago_m"
    fi
  fi
fi
echo
ccu_section "Current session" "$s_pct" "$s_reset"
if [[ -n "$w_pct" ]]; then
  echo
  ccu_section "Current week (all models)" "$w_pct" "$w_reset"
  ccu_pace "$w_pct" "$w_reset"
fi
if [[ -n "$so_pct" ]]; then
  echo
  ccu_section "Current week (Sonnet only)" "$so_pct" "$so_reset"
fi
if [[ -n "$sc_pct" ]]; then
  echo
  local sc_label="${sc_model:-model}"
  ccu_section "Current week (${sc_label} only)" "$sc_pct" "$sc_reset"
fi
if [[ -n "$e_pct" ]]; then
  echo
  local extra_info=""
  if [[ -n "$e_spent" && -n "$e_limit" ]]; then
    extra_info=$(printf '$%.2f / $%.2f spent' "$e_spent" "$e_limit")
  fi
  ccu_section "Extra usage" "$e_pct" "$e_reset" "$extra_info"
fi
echo
