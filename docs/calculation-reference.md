# Calculation Reference — Claude Code Usage & Cost Pipeline

## Table of Contents

1. [Pricing Tables](#1-pricing-tables)
2. [Per-Message Cost Calculation](#2-per-message-cost-calculation)
3. [Usage Percentage Parsing (usage_api.py)](#3-usage-percentage-parsing)
4. [Cost Aggregation Windows (pricing.py)](#4-cost-aggregation-windows)
5. [Caching Strategy & Cache Formats](#5-caching-strategy--cache-formats)
6. [Deduplication](#6-deduplication)
7. [Shared Pricing Module & Maintenance](#7-shared-pricing-module--maintenance)
8. [Display & Formatting](#8-display--formatting)
9. [Account Attribution](#9-account-attribution)
10. [Skills](#10-skills)

---

## 1. Pricing Tables

All pricing data lives in `src/ccreport/pricing.py`. Both `usage_api.py` and
`ccreport.py` import from this shared module (see §7).

### Structure

```python
PRICING_HISTORY = [
    {
        "effective": "YYYY-MM-DD",   # date this pricing became active
        "models": {
            "model-id": {
                "input":              $/token,  # base input rate
                "output":             $/token,  # base output rate
                "cache_create":       $/token,  # prompt cache write rate
                "cache_read":         $/token,  # prompt cache read rate
                "input_200k":         $/token,  # tiered input rate (optional)
                "output_200k":        $/token,  # tiered output rate (optional)
                "cache_create_200k":  $/token,  # tiered cache write rate (optional)
                "cache_read_200k":    $/token,  # tiered cache read rate (optional)
            }
        }
    },
    ...
]
```

Periods are ordered chronologically. Lookup walks **reverse** to find the
most recent entry whose `effective` date is <= the message timestamp.

### Model Resolution

1. Check `MODEL_ALIASES` dict (e.g. `"claude-opus-4-5"` -> `"claude-opus-4-5-20251101"`)
2. Exact match against period's models dict
3. Substring match: if `key in resolved` or `resolved in key`

### Current Prices (as of LAST_CHECKED = 2026-02-22)

| Model | Input | Output | Cache Create | Cache Read |
|-------|-------|--------|-------------|------------|
| claude-opus-4-6 | $5/M | $25/M | $6.25/M | $0.50/M |
| claude-opus-4-6 (>200K) | $10/M | $37.50/M | $12.50/M | $1/M |
| claude-sonnet-4-6 | $3/M | $15/M | $3.75/M | $0.30/M |
| claude-sonnet-4-6 (>200K) | $6/M | $22.50/M | $7.50/M | $0.60/M |
| claude-sonnet-4-20250514 | $3/M | $15/M | $3.75/M | $0.30/M |
| claude-sonnet-4-5-20250929 | $3/M | $15/M | $3.75/M | $0.30/M |
| claude-opus-4-5-20251101 | $5/M | $25/M | $6.25/M | $0.50/M |
| claude-haiku-4-5-20251001 | $1/M | $5/M | $1.25/M | $0.10/M |

($/M = dollars per million tokens)

---

## 2. Per-Message Cost Calculation

The core cost formula lives in `pricing.py` and is used by both
`usage_api.py` and `ccreport.py`.

### 200K Tier Threshold

```
TIER_THRESHOLD = 200,000 tokens
```

Each token type is tiered **independently** — the threshold applies per-type,
not to total tokens.

### Tiered Cost Formula

For each token type (input, output, cache_create, cache_read):

```
_tiered_cost(count, base_rate, tiered_rate):
    if count > 200,000 AND tiered_rate exists:
        below = min(count, 200,000)
        above = count - below
        cost = below * base_rate + above * tiered_rate
    else:
        cost = count * base_rate
```

### Total Message Cost

```
message_cost = _tiered_cost(input_tokens,                 prices.input,        prices.input_200k)
             + _tiered_cost(output_tokens,                prices.output,       prices.output_200k)
             + _tiered_cost(cache_creation_input_tokens,  prices.cache_create, prices.cache_create_200k)
             + _tiered_cost(cache_read_input_tokens,      prices.cache_read,   prices.cache_read_200k)
```

### Input Data (from JSONL records)

Each assistant message in JSONL contains:

```json
{
    "type": "assistant",
    "message": {
        "id": "<message_id>",
        "model": "<model-id>",
        "usage": {
            "input_tokens": N,
            "output_tokens": N,
            "cache_creation_input_tokens": N,
            "cache_read_input_tokens": N
        }
    },
    "requestId": "<request_id>",
    "timestamp": "<ISO-8601>"
}
```

Additionally, `ccreport.py` supports a `costUSD` field on JSONL records — if
present, that pre-calculated value is used instead of computing from tokens.

---

## 3. Usage Percentage Parsing

`usage_api.py` scrapes the Claude CLI `/usage` command by spawning it in a
PTY and parsing the rendered text output.

### PTY Execution

1. Find `claude` binary (PATH -> common locations -> nvm -> npx fallback)
2. Ensure `~/.cache/ccreport/` is trusted in `~/.claude.json`
3. Open a PTY pair, spawn `claude --session-id <random-uuid> /usage`
4. Poll aggressively: drain PTY every 100ms, attempt parse every 500ms
5. Exit early once both `session_percent` and `week_percent` are found
6. Hard deadline: 15 seconds
7. Send `/exit\n`, terminate process, close PTY

### ANSI Stripping

Before parsing, raw PTY output is cleaned:
- Cursor positioning (`CSI row;col H`) -> newline
- Cursor forward (`CSI n C`) -> N spaces
- Cursor up/down/back -> newline
- All other CSI sequences (colors, clearing) -> empty
- OSC sequences -> empty
- Collapse triple+ newlines to double

### Section Boundaries

The parser identifies four sections by anchor regex:

| Section | Anchor Pattern |
|---------|---------------|
| session | `session` (case-insensitive) |
| week | `week\s*\(all` (case-insensitive) |
| sonnet | `week\s*\(Sonnet` (case-insensitive) |
| extra | `Extra\s+usage` (case-insensitive) |

Sections are bounded: each section ends where the next one starts.
Regexes are constrained to their section to prevent cross-section leakage.

### Percentage Extraction

For each section, the pattern `(\d+)%\s*used` extracts the usage percent.

### Reset Time Parsing

Two strategies, tried in order:

**Time-only** (e.g. "Resets 10pm", PTY-mangled "Rese s 10 m"):

```
Pattern: Rese[\w\s]*?(\d+(?::\d+)?)\s*([ap]?\s*m)
```

- Collapses whitespace in am/pm (PTY mangles "am" -> "a m")
- Converts to 24h, constructs datetime for today; if past, adds 1 day

**Date+time** (e.g. "Resets Mar 1", "Resets Feb 25 at 8pm"):

```
Pattern: Rese[\w\s]*?\s+([A-Za-z]+\s+\d+)(?:,?\s*(\d{4}))?\s*(?:at\s+)?(?:(\d+(?::\d+)?)\s*([ap]?\s*m))?
```

- Group 1: month+day, Group 2: optional year, Group 3: optional time, Group 4: optional am/pm
- If no year given and the resulting date is in the past, rolls to next year

### Fields Produced

```json
{
    "session_percent": int,
    "session_reset": "ISO-8601",
    "week_percent": int,
    "week_reset": "ISO-8601",
    "sonnet_percent": int,
    "sonnet_reset": "ISO-8601",
    "scoped_percent": int,
    "scoped_model": str,
    "scoped_reset": "ISO-8601",
    "extra_percent": int,
    "extra_spent": float,
    "extra_limit": float,
    "extra_reset": "ISO-8601"
}
```

All fields are optional — only present if successfully parsed.

---

## 4. Cost Aggregation Windows

`pricing.py` `compute_costs()` produces ten cost buckets by scanning
all JSONL files under `~/.claude/projects/` and `~/.config/claude/projects/`.

### Window Derivation

Given `session_reset` and `week_reset` ISO strings from §3, one helper —
`pricing.window_start_epoch(reset_iso, window_seconds, now)` — derives every
session/week window start, including the statusline's Extra deltas and weekly
pace:

```
if reset <= now:
    window_start = reset          # the window just rolled over
else:
    window_start = reset - window_seconds
```

Window lengths live in `pricing.py`: `SESSION_WINDOW_S` (5 h, from
`SESSION_WINDOW_HOURS`) and `WEEK_WINDOW_S` (7 d). A missing or unparseable
reset gives None; the week then falls back to Monday 00:00 local time. Naive
ISO strings count as local time — Claude Code's stdin rate limits arrive as
epoch seconds and reach the pipeline naive, while the usage API sends
offset-aware strings.

### Cost Buckets

The rolling buckets below are generated, not listed: `pricing.ROLLING_WINDOWS`
holds each window's key prefix, span and display label, and
`pricing.rolling_cost_keys()` turns that into the `<window>_cost` /
`<window>_project_cost` pair that `cache_db._USAGE_FIELDS` and the statusline's
merge list consume. Adding a window means one edit there.
`tests/test_window_keys.py` fails if the usage table columns, `UsageData`, or
the statusline's per-window env toggles fall behind.

| Bucket | Scope | Time Filter |
|--------|-------|-------------|
| `session_cost` | Only JSONL files matching `session_id` + `cwd` | All time (entire chat history) |
| `session_window_cost` | ALL JSONL files | `timestamp >= session_window_start` (key omitted when the session reset time is unknown) |
| `week_cost` | ALL JSONL files | `timestamp >= week_window_start` |
| `month_cost` | ALL JSONL files | `timestamp >= month_window_start` |
| `six_hour_cost` | ALL JSONL files | `timestamp >= now - 6 hours` |
| `twelve_hour_cost` | ALL JSONL files | `timestamp >= now - 12 hours` |
| `twenty_four_hour_cost` | ALL JSONL files | `timestamp >= now - 24 hours` |
| `seven_day_cost` | ALL JSONL files | `timestamp >= now - 7 days` |
| `thirty_day_cost` | ALL JSONL files | `timestamp >= now - 30 days` |
| `all_time_cost` | ALL JSONL files | No filter (all records) |
| `six_hour_project_cost` | JSONL files matching `cwd` project key | `timestamp >= now - 6 hours` |
| `twelve_hour_project_cost` | JSONL files matching `cwd` project key | `timestamp >= now - 12 hours` |
| `twenty_four_hour_project_cost` | JSONL files matching `cwd` project key | `timestamp >= now - 24 hours` |
| `seven_day_project_cost` | JSONL files matching `cwd` project key | `timestamp >= now - 7 days` |
| `thirty_day_project_cost` | JSONL files matching `cwd` project key | `timestamp >= now - 30 days` |
| `all_time_project_cost` | JSONL files matching `cwd` project key | No filter (all records) |

**Project identification**: `pricing.project_scope()` answers "is this record
the cwd's?" for every cost computation, and both tests it returns come from
`project_identity.py` so the reports and the statusline group identically
(macsetup-2qrp):

- **By path** — `cwd.replace("/", "-")` → JSONL file paths starting with
  `projects_dir / project_key /`. The trailing separator is what keeps a
  sibling out: `/tmp/proj-other` encodes to `-tmp-proj-other`, which has
  `-tmp-proj` as a bare string prefix.
- **By name** — the project the record was parsed under. This is the only
  handle left on an orphaned record whose directory is gone.

A `ccreport merge` applies to both. The scope's name becomes the merge target,
and its prefixes grow to cover every other project directory that resolves to
that same target, so merged projects share one set of cost windows. Without
merge rules the scope is one directory and one name, and costs a single small
table read per computation.

**Month window start**: First day of current month, 00:00 local time.
**Rolling windows**: 6h, 12h, 24h, 7-day, and 30-day are computed from
`now - N` (local time). These and `session_window_cost` are always computed
fresh (not cached per-file) since the window shifts continuously.
**Cached per-file**: `week_cost`, `month_cost`, `all_time_cost`, and
`session_cost` are stored in the cost cache (see §5.2).
**Per-model week split**: `week_model_costs` is the week bucket alone, split by
model family — the bucket a `weekly_scoped` quota is spent against, so the
scoped statusline segment can price its own window. `pricing.model_family()`
derives the key on both sides: from the record's model ID when accumulating,
from the quota's display name when reading, with everything outside
`MODEL_FAMILIES` (haiku, sonnet, opus, fable) sharing one `other` bucket. Every
path into `week_cost` carries the split — the fresh scan, both cached-file
branches, and the orphaned ccreport records — and it is cached per file in the
same row as the totals. No other window has one.
**Missing session window**: without a session reset time there is no window to
total, so `compute_costs` leaves `session_window_cost` out of its result and out
of the cost summary. Callers merge the dict over existing data — a placeholder
`0.0` would overwrite a real total written by a caller that had the reset.

### Session File Identification

For a given `session_id` and `cwd`:
1. Convert cwd to project key: `cwd.replace("/", "-")`
2. Look in each projects dir for:
   - `{project_key}/{session_id}.jsonl` (main session)
   - `{project_key}/{session_id}/*.jsonl` (subagents, tool results)

### Scanning Logic

For each JSONL file:
1. Classify it against the windows: session file, in the session window
   (`mtime >= session_window_start`), in the rolling window
   (`mtime >= thirty_day_start`)
2. Check per-file cache (mtime_ns + size match)
3. If cached AND not in session window AND not in rolling window →
   use cached `week_cost`/`month_cost`/`all_time_cost`/`session_cost`
4. If cached AND in session window or rolling window →
   use cached totals but re-parse for `session_window_cost` and rolling costs
5. If not cached → full parse, compute all bucket contributions

### Per-Record Processing

For each `"type": "assistant"` record in a JSONL file:
1. Extract `message.usage` token counts
2. Call `calc_cost(usage, model, timestamp)` (same formula as §2)
3. Check dedup key (see §6)
4. Add to appropriate bucket(s) based on timestamp

---

## 5. Caching Strategy — Unified SQLite Database

All caching lives in a single SQLite database managed by `claude/cache_db.py`:

- **File**: `~/.cache/ccreport/cache.db`
- **Mode**: WAL (concurrent readers, single writer)
- **PRAGMAs**: `synchronous=NORMAL`, `foreign_keys=ON`, `cache_size=-2000`

### 5.1 Tables

| Table | Purpose | Consumers |
|-------|---------|-----------|
| `meta` | Global metadata (week/month keys, ccreport version/hash/salt, migration flags) | all |
| `usage` | Singleton row with PTY-fetched usage data + computed costs | usage_api.py, statusline.py |
| `file_costs` | Per-JSONL-file cost totals for compute_costs() | pricing.py |
| `dedup_keys` | Dedup keys of in-window files, linked to file_costs | pricing.py |
| `cache_stats` | Per-session token accumulation | statusline.py |
| `session_costs` | Per-session JSONL cost cache | pricing.py |
| `ccreport_files` | Per-file mtime/size tracking for ccreport | ccreport.py |
| `ccreport_records` | Parsed assistant message records | ccreport.py |
| `project_overrides` | Manual project-grouping rules (`name` / `remote` / `cwd_prefix` → target), applied by every reader | ccreport.py (write), project_identity.py (read) |
| `extra_usage_snapshots` | `(ts, spent)` history of Extra usage spend, pruned at 31 days | usage_api.py (write), statusline.py (read) |
| `exchange_rates` | Norges Bank USD→NOK daily spot rates, keyed by Oslo date | exchange.py |
| `account_events` | Append-on-change log of the signed-in Claude account and its tiers (§9) | statusline.py (write), ccreport.py (read) |
| `rate_limit_snapshots` | Utilization samples per rate-limit window, keyed by window instance (`resets_at`); a row only when the whole-percent reading moves. Never pruned (§9.6) | statusline.py (write), ccreport.py (read) |

`project_overrides` is local data by design: merges and renames live in the DB,
not in code, so they are never committed.

`ccreport_records` is the only table that grows without bound — orphan
preservation (§5.6) means rows outlive the JSONL they came from. Order of
magnitude on a machine with ~6 months of history: ~89K rows across ~1,560 files
and 90 distinct days (measured 2026-08-07). The `NokCtx._rate_memo` docstring in
`ccreport.py` says "half a million records", which overstates this machine by
~5x — treat it as a ceiling for the memo's rationale, not a row count.

### 5.2 Usage Data

- **Table**: `usage` (singleton row, `id = 1`)
- **Max age**: 600 seconds, checked via `last_updated` column
- **Early invalidation**: if `session_reset` or `week_reset` time has passed
- **Written after**: each fresh PTY fetch via `write_usage_cache()`, which
  updates only the keys the write dict carries — a column the caller omits keeps
  its stored value, so a failed cost computation cannot null the cost columns
- **Read by**: `read_usage_stale()` — the single SELECT of the row. Freshness is
  the predicate `usage_is_fresh(row, max_age)` over that dict, so the statusline
  reads the row once per render and `read_usage_cache()` wraps the pair
- **Extra blobs**: `_meta`, `_cleaned_session` stored in `meta_json` TEXT column

### 5.3 Cost Cache

- **Tables**: `file_costs` + `dedup_keys`
- **Scoped by**: `cost_week`, `cost_month` and `cost_schema` keys in `meta` table
- **Invalidation**: when week/month keys shift, all `file_costs` rows are deleted (cascades to `dedup_keys`)
- **Entry shape**: `cost_schema` is the payload version (`cache_db._COST_ENTRY_SCHEMA`),
  checked the same way and truncating the same rows. A row predating a field —
  `week_model_json`, added for the per-model week split — still matches on mtime
  and size, so nothing else would invalidate it and the field would total as zero.
  Bumping the constant buys one full re-scan and makes the whole corpus correct
- **Per-file change detection**: `mtime_ns` + `size` columns, same logic as before
- **Dedup keys**: stored in separate `dedup_keys` table with `(file_path, dk)` composite PK
- **Pre-loaded**: all dedup keys loaded into Python `set` at start of `compute_costs()`
- **Retention**: keys are only stored for files whose `mtime_ns` is inside the widest
  bounded window (the older of the calendar-month start and the rolling 30-day
  threshold, passed in as `dedup_cutoff_ns`); older files' keys are never written and are
  deleted on the next save. Accepted risk: `all_time` is unbounded, so a message id
  shared between a fresh file and one that has aged out counts twice there
  (macsetup-1jvz)
- **Bulk save**: `bulk_save_file_costs()` writes the whole dataset in one transaction, but
  only rewrites rows named in its `changed` set — the paths `compute_costs()` actually
  re-scanned. Everything else keeps its row, and so keeps its `dedup_keys` children, which
  a DELETE would have cascaded away (macsetup-5vsf). Paths absent from the dataset are
  deleted; deletes are chunked to stay under the bound-parameter limit

**Cache hit logic**:

```
file_unchanged = (cached.mtime_ns == stat.mtime_ns AND cached.size == stat.size)

if file_unchanged AND not in_session_window AND not in_rolling_window:
    # Full hit: use cached week_cost/month_cost/all_time_cost/session_cost
elif file_unchanged AND (in_session_window OR in_rolling_window):
    # Partial hit: use cached totals, re-parse for session_window_cost and rolling costs
else:
    # Miss: full parse, compute all costs, write new entry
```

### 5.4 Cache Stats

- **Table**: `cache_stats`
- **Key**: `session_id`
- **Columns**: `total_in_tokens` (change detector), `cum_fresh`, `cum_cache_create`, `cum_cache_read`
- **Update logic**: same as before — accumulate deltas when `total_in_tokens` changes

### 5.5 Session Costs

- **Table**: `session_costs`
- **Key**: `session_id`
- **Columns**: `fingerprint` (change detector), `cost`
- **Fingerprint**: md5 over the sorted `(path, mtime_ns, size)` tuples of every
  JSONL file in the session (`pricing.compute_session_cost`). Declared `INTEGER`
  in `_SCHEMA_SQL` but holds the hex digest — SQLite's dynamic typing lets the
  TEXT value through unconverted.
- **Invalidation**: re-parsed when any file's mtime or size changes, or when a
  file joins or leaves the session. An edit that leaves the total byte count
  unchanged does invalidate — the mtime moves.

### 5.6 Report Cache

- **Tables**: `ccreport_files` + `ccreport_records`
- **Invalidation**: `check_ccreport_valid()` compares three `meta` keys —
  `ccreport_version`, `ccreport_script_hash`, and `ccreport_schema_salt` (the
  `cache_db.CACHE_SCHEMA_SALT` constant, bumped by hand when a schema or
  serialization change alters the format of stored records, macsetup-2tt1).
  `init_ccreport_meta()` writes all three. Any mismatch resets mtime/size to
  force re-parse and NULLs costs for recompute, but preserves orphaned records
  (macsetup-qn0k)
- **Per-file change detection**: `mtime_ns` + `size` in `ccreport_files`
- **Records**: stored as individual rows in `ccreport_records` (indexed on `file_path` and `ts`)
- **Orphan preservation**: when JSONL files are purged from disk (e.g. Claude Code auto-cleanup),
  their cached records are kept in SQLite. `load_all_records()` picks them out of the
  `bulk_load_ccreport_cache()` result — every cached file whose path was not seen on disk
  this run — preserving historic usage data beyond the ~1 month JSONL retention window
- **Writes are batched**: freshly parsed files accumulate and flush through
  `save_ccreport_files()` every `_SAVE_BATCH` files, one write transaction per batch
  (macsetup-92y0)
- **The statusline reads it too**: `compute_project_rolling_costs` sums a live
  file from `load_ccreport_records_under` when
  `load_ccreport_file_meta_under` says the fingerprint still matches the file
  on disk, and re-parses the JSONL when it does not — 90 MB re-parsed per
  render was ~93% of it (macsetup-rn21). Only per-record facts come from the
  cache; the windows are still derived from `now` on every call. A live file's
  cost is recomputed from its cached tokens rather than read from the stored
  `cost`, so the two paths agree on a record whose `costUSD` disagrees with
  `calc_cost`; the orphan pass keeps the stored cost, which for a purged file
  is all that is left. The render never writes back, so files newer than the
  last `ccreport` run cost one parse each
- **The resolved scope is cached per cwd**: `project_scopes(cwd, name,
  prefixes)` holds what `pricing.project_scope` worked out, so a render with
  merge rules in play skips `load_ccreport_file_identities` — a GROUP BY over
  every cached record, 0.020s of an 0.085s call (macsetup-6cov). The row
  carries no fingerprint of its own: it is a pure function of
  `project_overrides` and those identities, and every writer of either
  (`add_project_override`, `delete_project_override`, `invalidate_ccreport`)
  empties the table in the same transaction — except `save_ccreport_files`,
  which empties it only when the batch changes some file's
  (repo, cwd, project) identity (macsetup-ov32); a record write by any other
  route must clear scopes itself. Reads are
  salt-gated like every other ccreport reader, so a stale row format degrades
  a cached scope exactly as it degrades a freshly derived one — to the
  unmerged scope. A row is also ignored when it no longer covers the cwd's own
  project directories, which is what lets a projects dir that appeared since
  the write show up without waiting for an invalidation. This is the one thing
  a render does write, best-effort: a failing write costs the next render a
  re-derivation and nothing else

---

## 6. Deduplication

Both `usage_api.py` and `ccreport.py` use the same deduplication strategy.

### Composite Key

```
dedup_key = "{message.id}:{requestId}"
```

- Both fields must be non-empty for the key to be valid
- First occurrence wins; subsequent duplicates are skipped

### Fallback Identity

A log missing either field leaves the key NULL, and those records used to be
waved through — a row stored twice then counted twice in every reader
(macsetup-2wgm). `pricing.dedup_identity()` supplies a stand-in key:

```
(message.id, sessionId, timestamp, model, all four token counts)
```

Every reader goes through that one function: `ccreport._keep`, the orphaned and
cached-record passes in `compute_costs`, `compute_project_rolling_costs`, and
the JSONL scan itself. Only the log's own key is ever persisted as a file's
`dedup_keys`; the fallback is a read-time device.

Deliberately narrow. Progressive chunks of one streaming message share a
message id but differ in their token counts, so the fallback keeps them apart —
collapsing chunks is the real key's job and they carry one. A record with
neither a message id nor a single token is never a duplicate: session and
timestamp alone are not enough to delete a row on.

---

## 7. Shared Pricing Module & Maintenance

All pricing data and cost computation lives in a single shared module:

```
claude/pricing.py
```

`usage_api.py`, `statusline.py`, and `ccreport.py` all import
from this module:

| Item | Location |
|------|----------|
| `PRICING_HISTORY` | `pricing.py` |
| `MODEL_ALIASES` | `pricing.py` |
| `TIER_THRESHOLD` | `pricing.py` (200,000) |
| `tiered_cost()` | `pricing.py` |
| `find_pricing()` | `pricing.py` |
| `calc_cost()` | `pricing.py` |
| `compute_session_cost()` | `pricing.py` |
| `compute_costs()` | `pricing.py` |
| `_iter_jsonl_costs()` | `pricing.py` |
| `_parse_window_starts()` | `pricing.py` |
| `window_start_epoch()` | `pricing.py` |
| `LAST_CHECKED` | `pricing.py` |

When updating pricing, only `pricing.py` needs to change.

### Pricing Source

All per-token pricing comes from the LiteLLM open-source pricing database:

```
https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
```

The `LAST_CHECKED` constant in `pricing.py` records when pricing was last verified.

### Checking for Pricing Updates

Fetch the latest JSON and compare against our tracked models:

```bash
curl -sL "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
models = ['claude-opus-5', 'claude-sonnet-5', 'claude-fable-5', 'claude-mythos-5',
          'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6', 'claude-sonnet-4-6',
          'claude-opus-4-5-20251101', 'claude-sonnet-4-20250514',
          'claude-haiku-4-5-20251001', 'claude-sonnet-4-5-20250929']
for m in models:
    d = data.get(m, {})
    print(f'{m}:')
    print(f'  input:        {d.get(\"input_cost_per_token\")}')
    print(f'  output:       {d.get(\"output_cost_per_token\")}')
    print(f'  cache_create: {d.get(\"cache_creation_input_token_cost\")}')
    print(f'  cache_read:   {d.get(\"cache_read_input_token_cost\")}')
    print()
"
```

You can also check the commit history for Claude-related changes:

```bash
gh api "repos/BerriAI/litellm/commits?path=model_prices_and_context_window.json&since=$(date -v-30d +%Y-%m-%dT00:00:00Z)&per_page=50" \
  --jq '.[] | select(.commit.message | test("claude|sonnet|opus|haiku"; "i")) | "\(.sha[:8]) \(.commit.committer.date[:10]) \(.commit.message | split("\n")[0][:120])"'
```

After verifying, update `LAST_CHECKED` in `pricing.py`.

### Adding a New Model

If the model uses existing pricing (same rates as an already-tracked model), add it to the
latest period that covers its release date. If no period exists for that date, create one.

Example — adding a hypothetical `claude-foo-5-20260601`:

```python
{
    "effective": "2026-06-01",
    "models": {
        "claude-foo-5-20260601": {
            "input": 3e-06,
            "output": 15e-06,
            "cache_create": 3.75e-06,
            "cache_read": 0.3e-06,
        },
    },
},
```

If the model has a short alias (e.g. `claude-foo-5`), add it to `MODEL_ALIASES`.

### Handling a Price Change

If an existing model's pricing changes on a specific date, add a **new period** with that
date and the updated rates. Do **not** modify the old period — that preserves accurate
costing for historical usage before the change.

```python
{
    "effective": "2026-07-01",  # date the new price takes effect
    "models": {
        "claude-opus-4-6": {
            "input": 4e-06,  # new lower price
            "output": 20e-06,
            "cache_create": 5e-06,
            "cache_read": 0.4e-06,
        },
    },
},
```

### Rate Keys

| Key | Meaning | LiteLLM JSON Key |
|-----|---------|-----------------|
| `input` | Input token cost | `input_cost_per_token` |
| `output` | Output token cost | `output_cost_per_token` |
| `cache_create` | Prompt cache write cost | `cache_creation_input_token_cost` |
| `cache_read` | Prompt cache read cost | `cache_read_input_token_cost` |
| `input_200k` | Input cost above 200K context | `input_cost_per_token_above_200k_tokens` |
| `output_200k` | Output cost above 200K context | `output_cost_per_token_above_200k_tokens` |
| `cache_create_200k` | Cache write cost above 200K context | `cache_creation_input_token_cost_above_200k_tokens` |
| `cache_read_200k` | Cache read cost above 200K context | `cache_read_input_token_cost_above_200k_tokens` |

The `_200k` keys are optional. If absent, the base rate applies at all context lengths.

---

## 8. Display & Formatting

This section covers rendering logic across all display surfaces. Separated
from computation to keep §1-7 focused on data/logic.

### 8.1 Statusline (statusline.py)

Receives JSON via stdin from Claude Code's statusline hook.

**Input fields used:**

```
cwd                 = .workspace.current_dir // .cwd
model               = .model.display_name
used                = .context_window.used_percentage   # fallback only, see below
cost                = .cost.total_cost_usd
ctx_size            = .context_window.context_window_size
lines_added         = .cost.total_lines_added
lines_removed       = .cost.total_lines_removed
cache_create        = .context_window.current_usage.cache_creation_input_tokens
cache_read          = .context_window.current_usage.cache_read_input_tokens
input_fresh         = .context_window.current_usage.input_tokens
total_in_tokens     = .context_window.total_input_tokens
cur_session_id      = .session_id
```

**Context window:**

```
used_tokens = total_in_tokens > 0 ? total_in_tokens : round(ctx_size * used / 100)
usable      = USABLE_CTX ? ctx_size - 33_000 : ctx_size
used_k      = ceil(used_tokens / 1000)
total_k     = ceil(usable / 1000)          # rendered as "1M" once >= 1000
ctx_pct     = min(100, ceil(used_tokens * 100 / usable))
Display: "{used_k}k/{total_k}k:{ctx_pct}%"   # bare "ctx:{ctx_pct}%" with SESSION off
Color:   red >= 70, yellow >= 50, else grey (green when CTX_GREEN is on)
```

`total_in_tokens` is exact and is the same input-only basis `used_percentage`
reports (`input + cache_creation + cache_read`), so `used` is only a fallback for
the two states where the field reads 0: before the first API response, and after
`/compact` until the next call. With neither available, both figures are hidden.

The 33k is `AUTOCOMPACT_BUFFER`, an estimate — Claude Code publishes no
auto-compact threshold in the statusline payload or its docs. It is applied flat,
so a 1M window reads as 967k. Verify against `/context` before trusting it.

**Cumulative cache hit rate** (uses cache-stats file from §5.3):

```
total_in = cum_fresh + cum_cache_create + cum_cache_read
ch_pct   = cum_cache_read * 100 / total_in
Display: "CH:{ch_pct}%"
```

**Usage section** (calls `usage_api.py --session <id> --cwd <dir>`):

Reset countdown format:
```
>= 86400s:  "{d}d{h}h"
>= 3600s:   "{h}h{m}m"
else:        "{m}m"
```

The S window appends the local wall-clock reset time: "2h15m(17:26)". W and the
scoped windows do not — their resets are days out, so a bare clock misleads.

Rate-limit line (SC = session-window cost, WC = week cost):
```
cost_fmt = ceil(cost_val)
Hidden if ceil rounds to 0
Display: "{label}:${cost_fmt}"
```

Session cost sourced from `usage_json.session_cost`.

Sonnet section hidden when `so_pct < CLAUDE_STATUSLINE_SONNET_THRESHOLD` (default 25).

Scoped section (per-model weekly limit, from `limits[]` where `kind` is
`weekly_scoped`) hidden when `sc_pct < CLAUDE_STATUSLINE_SCOPED_THRESHOLD`
(default 25), when `scoped_model` is absent, or when the scope names Sonnet and
the Sonnet section already rendered:
```
Label: first two chars of scoped_model, title-cased ("Fable" → "Fa")
Display: "{label}:{pct}% ${cost_fmt} {countdown}"
```
The cost is `week_model_costs[model_family(scoped_model)]` (§4) — the scoped
quota's window is the weekly one, so its cost is W's narrowed to the capped
model. Omitted, leaving the segment as it was, when the split names no such
family or has not reached the render.
The countdown is dropped when it renders identically to W's — the scoped quota
usually resets with the weekly one, and the clock only earns its space once.
The pace half stays either way, so the segment still reads standalone:
"W:62% $1236 2d21h/7d(4d2h) +21% · Fa:9% $312 2d21h/7d -32%".
Fetch-only — native `rate_limits` on stdin carries `five_hour`/`seven_day` only.

Extra usage hidden when `s_pct < CLAUDE_STATUSLINE_EXTRA_SESSION_THRESHOLD` (default 60):
```
Display: "E:${spent_fmt}/${limit_fmt}"   (trailing zeros stripped)
```

TTL and staleness. S/W arrive on stdin every render, so the fetch clock is only
shown where it still drives something:

```
native rate_limits absent (the fetch IS the S/W source):
  ttl_s = 600 - age;  "TTL:{m}m{s}s" while positive — opt-in, needs
                      CLAUDE_STATUSLINE_TTL=1
  past 0:             silent until age >= 1800 (STALE_GRACE_S), then red
                      "stale:{m}m" — earlier only if check_fetch_backoff()
                      reports a recorded failure
  age >= 3600:        rate-limit line dropped entirely
native rate_limits present (normal Pro/Max case):
  no TTL at any age
  red "stale:{m}m" only when age >= 3600 AND a fetched field is on screen —
  Sonnet or scoped above threshold, or Extra with spend > 0
  otherwise silent; S/W stay live however old the fetched fields are
```

Both the grace and TTL being opt-in come from the same fact: the
absent-`rate_limits` case is also the session's first render. Claude Code sends
no `rate_limits` before the session's first API response, so a cache older than
the 600s interval would flag every cold start red, and the countdown would
appear only in that pre-first-message window. That render spawns a full refresh
which lands on the next one. Set `CLAUDE_STATUSLINE_TTL=1` on a plan that never
receives native rate limits, where the countdown tracks the real S/W source.

A displayed `E:$0` does not qualify as on-screen fetched data: some profiles pin
`EXTRA_SESSION_THRESHOLD` to 0, and an unspent balance cannot move until the
window is exhausted.

The API fetch is conditional (`_api_fetch_needed`). S/W arrive natively on
stdin, so the API is called only when a fetch-only field could change the
render — Sonnet or scoped within `NEAR_THRESHOLD_MARGIN` (10) of its threshold,
Extra with spend > 0 or session >= `EXTRA_ACCRUAL_PCT` (90) — with a
`USAGE_HEARTBEAT_S` (3600) ceiling so cached percentages cannot drift
unnoticed. Otherwise the spawn becomes `--costs-only`, which recomputes costs
and leaves every API column, `last_updated` included, untouched. Because the
skip is deliberate, TTL counts to the heartbeat instead of turning red.

**Historic cost line** (separate from rate-limit line):

Each bucket is controlled by an env var and hidden when value rounds to $0:

| Label | Env Var | Data Field | Default |
|-------|---------|------------|---------|
| 6H | `CLAUDE_STATUSLINE_6H_COST` | `six_hour_cost` | off |
| 12H | `CLAUDE_STATUSLINE_12H_COST` | `twelve_hour_cost` | off |
| 24H | `CLAUDE_STATUSLINE_24H_COST` | `twenty_four_hour_cost` | on |
| 7D | `CLAUDE_STATUSLINE_7D_COST` | `seven_day_cost` | on |
| 30D | `CLAUDE_STATUSLINE_30D_COST` | `thirty_day_cost` | on |
| AT | `CLAUDE_STATUSLINE_AT_COST` | `all_time_cost` | on (shown only when `all_time - thirty_day >= 0.005`) |

The entire historic cost line is gated by `CLAUDE_STATUSLINE_HISTORIC_COST` (default on).

**Per-project cost display**: When a project-specific cost is available and
`ceil(project_cost) < ceil(total_cost)`, the format becomes `LABEL:$project/$total`
(e.g. `7D:$64/$375`). When they are equal or project cost is 0, only `$total` is shown.

**Active sessions count:**
```
cutoff_ms = now_epoch * 1000 - 900000    (15 minutes ago)
Count distinct .project values in last 100 lines of ~/.claude/history.jsonl
where timestamp >= cutoff_ms AND project != current cwd
```

**Line separator:** When `CLAUDE_STATUSLINE_WRAP=1`, the three output
sections (top info, session, usage) are separated by newlines. When `0`
(default), they are joined with spaces on a single line.

### 8.2 Terminal Dashboard (ccu.zsh)

Calls `usage_api.py [--force]`, renders progress bars + countdowns.

`ccu --json` skips all of this and runs `usage_api.py --raw`, which
prints the untouched `/api/oauth/usage` body. That path bypasses the usage
cache (only mapped fields are cached, never the raw body), so it always hits
the API, and it never records a fetch failure.

Sections, in order: session, week (all models), week (Sonnet only), week
(model-scoped, e.g. Fable), extra usage. The Sonnet and model-scoped
sections are each shown only when their fields are present. The scoped
section's label is taken from `scoped_model` — "Current week ({model} only)"
— so it tracks whatever model the API scopes the weekly limit to. Fable is
sourced from the API's `limits[]` array (`kind: weekly_scoped`), not a
dedicated top-level key.

**Progress bar:**
```
width = 50, filled = pct * 50 / 100
Display: {filled * '#' in green}{(50-filled) * '#' in dark gray}
```

**Countdown:**
```
days > 0:    "{d} day(s) [and {h} hour(s)]"
hours > 0:   "{h} hour(s) [and {m} minute(s)]"
else:         "{m} minute(s)"
```

**Reset line:**
```
midnight:           "Resets in {countdown} on {month day} ({tz})"
today/tomorrow:     "Resets in {countdown} at {time} ({tz})"
else:               "Resets in {countdown} at {time} on {month day} ({tz})"
```

Time format: 12-hour with am/pm, minutes omitted if :00.

**Weekly pace** (shown below weekly bar):
```
week_start  = reset_epoch - 7 * 86400
elapsed_s   = now - week_start
expected    = elapsed_s * 100 / (7 * 86400)
delta       = actual - expected
Display: "{el_d}d {el_h}h into 7-day window — {expected}% expected, {sign}{delta}% {label}"
Labels: >+15 "ahead" (red), >+5 "ahead" (yellow), ±5 "on pace" (green),
        <-5 "behind" (cyan), <-15 "behind" (dim)
```

**Last fetched:**
```
ago_m = (now - last_updated) / 60
<= 0: "just now", == 1: "1 minute ago", else: "{ago_m} minutes ago"
```

### 8.3 Report Tables (ccreport.py)

**Aggregation buckets:**

```python
class AggBucket:
    tokens: TokenCounts    # sum of all token counts
    cost: float            # sum of record_cost() for each record
    models: set[str]       # distinct models seen
    count: int             # number of assistant messages
```

**Report types:**

| Report | Bucket Key | Sorting |
|--------|-----------|---------|
| daily | `YYYY-MM-DD` (local time) | chronological |
| monthly | `YYYY-MM` (local time) | chronological |
| project | project name (last path segment) | by cost descending |
| session | session_id | by cost descending |
| account | account label (§9) | by cost descending |

**Token formatting:**
```
>= 1,000,000:  "{n/1M:.1f}M"
>= 1,000:      "{n/1K:.1f}K"
else:           "{n}"
```

**Cost formatting:**
```
>= $1.00:  "${c:.2f}"
<  $1.00:  "${c:.4f}"
```

**Project name derivation:** Directory names like `-Users-ove-git-foo` ->
strip leading dash, split on `-`, take last segment (-> `foo`). Subagent
files use grandparent's parent directory name.

**Averages:** Reports with multiple buckets show average per displayed set
and average per all buckets.

### 8.4 Color Thresholds

All ANSI color codes consolidated here:

| Metric | Green (32) | Yellow (33) | Red (31) |
|--------|-----------|-------------|----------|
| Context window % | < 50 | >= 50 | >= 70 |
| Cache hit % | >= 90 | >= 50 | < 50 |
| Usage % (S/W/So) | < 65 | >= 65 | >= 85 |
| Active sessions | — (dim 90: < 2) | >= 2 | >= 4 |

| Cost Range | ccreport.py Style |
|------------|------------------|
| >= $50 | bold red |
| >= $10 | yellow |
| >= $1 | green |
| < $1 | dim green |

---

## 9. Account Attribution

Which Claude account paid for a record. Nothing in the data says so directly:
a session JSONL carries no account field, and `~/.claude.json` holds only the
account signed in *right now*. Attribution therefore runs off a captured
timeline of account changes.

### 9.1 Capture

- **Source**: the `oauthAccount` object in `~/.claude.json`
- **Capture point**: `statusline.py::_capture_account()`, on every
  slow-path render. The render is the only thing that runs often enough to
  catch a mid-session `/login`; there is no hook for it. The file is a quarter
  of a megabyte, so it is reparsed only when its `(mtime_ns, size)` differs
  from the stamp the session memo holds — an account cannot have changed in a
  file nothing rewrote
- **Stored — identity**: `accountUuid` (stable key), `emailAddress` (label),
  `organizationUuid` + `organizationName` (which split the same address billing
  through work from the same address billing personally). These four are
  `cache_db._ACCOUNT_IDENTITY_COLS`, and they are what "the same account" means
- **Stored — tiers**: `seatTier` → `seat_tier` (Team seat product, e.g.
  `team_tier_1`; NULL on personal plans), `userRateLimitTier` →
  `user_rate_limit_tier` (per-user bucket, e.g. `default_claude_max_5x`),
  `organizationRateLimitTier` → `organization_rate_limit_tier` (org pool, e.g.
  `default_raven`). `cache_db.effective_limit_tier(row)` resolves the pair the
  limits report shows: the user tier when set, else the org one, because a
  per-user bucket overrides the pool the account would otherwise share
- **Deliberately not stored**: `billingType`, the role fields, `displayName`
- **Written when**: `cache_db.record_account_event()` compares against the
  newest row and inserts only on a difference. This is an append-only change
  log, not a per-render log — the unchanged case costs one SELECT and no write.
  The comparison covers all seven fields, so a seat upgrade or a plan change on
  an unchanged login appends a row: dating a tier change is the point of keeping
  the tiers at all
- **Staleness**: the tier fields are undocumented and cached. Claude Code
  refreshes `oauthAccount` on `/login` (`profileFetchedAt`), so a tier changed
  server-side reads stale here until the next sign-in
- **The first row after the columns arrived is not a tier change.** Every event
  captured before them holds NULL tiers, so the next capture differs and
  appends — dated when the tiers were first observed, not when they were set
- **Never stored**: an `oauthAccount` with no `accountUuid`. A row here is
  permanent history no later render can correct, so a NULL key is refused
- **Failure mode**: best-effort. A missing, half-written or unparseable config,
  and a database held by another writer, each cost the log one sample and never
  the status line

### 9.2 Attribution

`ccreport.AccountTimeline` reads the whole log once per run
(`cache_db.load_account_events()`), and `_keep` stamps every record —
freshly parsed, cached, and orphaned alike — with the newest event at or before
its timestamp. `account_events.ts` is wall-clock epoch seconds from
`time.time()`; `UsageRecord.timestamp` is timezone-aware, so the lookup
compares `datetime.timestamp()` against it and is zone-independent.

Two consequences of doing this at read time rather than parse time:

- Records older than the first captured event report as `unknown`. The log
  begins when capture began; what ran before it is genuinely unrecorded, unless
  it is claimed by hand (§9.5)
- Nothing is frozen. `CACHE_VERSION` is untouched and no account column exists
  in `ccreport_records`, so a log that later gains an event re-attributes every
  past report on the next run with no re-parse of the corpus

### 9.3 Labels

The email is the bucket label. An email seen under more than one organization
name carries that name too (`me@example.com (Work AS)`) — one address can front
two separate accounts, and those must not share a bucket. An event with no
email falls back to its `accountUuid`; a record with no event at all is
`unknown`.

### 9.4 Reporting

- `ccreport account` — per-account table, report_project's columns and TOTAL
  row, sorted by cost descending. No `--limit`: an account is a login, so there
  are two or three of them
- `--account` / `-a` — case-insensitive substring filter on the label, applied
  in `_keep` alongside `--project`. Available on every report and on the
  default combined run. `-a unknown` selects the unattributed history
- `--json` gains an `"account"` key per record

The default combined report (no subcommand) prints daily, monthly, project and
session, then appends the account table — but only when
`_accounts_worth_showing()` holds: **more than one distinct account label that
is not `unknown`**. The predicate reads the records `main()` already loaded and
filtered, so the check costs no extra query.

The reasoning, and it is the same reasoning in both directions:

- Two or more real accounts — the split carries information no other table has
- One real account — every row restates the TOTAL the other four already printed
- One real account beside `unknown` — one account's costs drawn twice, which is
  the pair §9.5 exists to merge, so `unknown` never counts towards the two
- No records, or nothing but `unknown` — less than that again

Worth expecting: adopting (§9.5) can *hide* the table. Before adopting, a
machine with one login and pre-capture history has one real label and `unknown`,
which is already below the bar; after adopting it has one real label and
nothing else. Either way one account, either way hidden — the table returns when
a second account actually appears. That is intended, not a regression.

`ccreport account` is unconditional. It is where someone goes to see the split
the default run declined to volunteer, `unknown` included.

### 9.5 Adopting Pre-Capture History

`ccreport adopt` claims the `unknown` bucket for an account. It writes exactly
one row — `account_events` at `ts = cache_db.ADOPTED_TS` (`0.0`) — carrying the
identity copied from the newest real capture. §9.2's rule does the rest: an
event older than every record on the machine is the one every otherwise
unattributed record resolves to. No record is rewritten, no cache is
invalidated, and `CACHE_VERSION` stays put.

| Command | Effect |
|---------|--------|
| `ccreport adopt` | Preview, confirm, then write the row |
| `ccreport adopt --yes` | Same, without the y/N prompt |
| `ccreport adopt --remove` | Delete the row; that history reads as `unknown` again |

Rules the implementation holds to:

- **The row is a claim, not a capture.** `read_latest_account()` filters
  `ts > ADOPTED_TS`, so an adoption can never be copied into a fresh adoption,
  and a log holding nothing but an adoption still reports as never captured
- **An empty capture log is refused** (exit 1). There is no identity to adopt
  under until the status line has seen one
- **It never overrides a real event.** The row is oldest, so every capture keeps
  its own span; the adoption gets only what precedes the first one
- **`record_account_event` is unaffected.** Its newest-row comparison sorts
  `ts = 0` last, so a capture still writes on a real change and still stays
  quiet otherwise
- **The preview counts records older than the first capture**, not records
  currently reading `unknown` — the two agree until the first adoption, after
  which the latter is zero and would tell a user re-adopting that there is
  nothing to adopt
- **`--remove` is idempotent** and exits 0 either way. It is the only `DELETE`
  on this table and can only reach `ts = 0`; captures are permanent, and
  dropping one would silently mis-attribute every record after it
- **Re-adopting under the same identity is a no-op**; under a different one it
  names the existing claim and replaces it after the same confirmation. "Same"
  is `ccreport._same_account`, over the identity columns only — a tier that
  moved since the row was written does not make it a different account
- **The row's tier columns are NULL.** It claims who paid for pre-capture
  history; which tier that history ran under is not something today's login can
  be asked, and a copied tier would read as a reading and date a tier change to
  the wrong side of itself

### 9.6 Rate-Limit Utilization History

`ccreport limits` reads `rate_limit_snapshots` and `account_events` for how full
each window got and who was drawing on it, plus the records covering the sampled
span for what the filling cost — a sample carries a percentage and no tokens, so
the two tables cannot price a window on their own. `main()` still routes it
before the report path, like `overrides` and `adopt`: the window instances bound
the load to the span they cover, and the default report's unbounded one would be
thrown away.

**Window instance** = `(window, model, resets_at)`, with `resets_at` bucketed to
the whole minute by `cache_db.rl_window_key()`. Samples sharing a reset time are
readings of one 5-hour or 7-day span filling up, which is what makes a peak and a
fill time mean anything; the model is in the key because the scoped limit follows
whichever model it is scoped to, and which model that is can change inside one
week.

Two writer-side defects the reader has to absorb, both found by running this
report on real data:

- **Reset-time drift.** The usage API returns `resets_at` as a float that moves
  by up to a second between fetches of one window (observed: 80 scoped rows
  spanning `1786305599.03`–`1786305600.95`, all of the reset at
  `1786305600`). It is an identity at both ends, so the drift defeated the write
  gate — every render looked like a new window, and one scoped week reported as
  80 single-sample instances. `_rl_sample` now normalizes before storing;
  `_window_instances` normalizes again on read, because the rows already written
  keep their drift forever. The instance reports the bucket, the samples keep the
  float they were stored with
- **Placeholder reset times.** Claude Code sends `resets_at = 9999999999` on
  stdin where it has no real one; four rows carry it. `_rl_sample` now refuses
  anything more than `cache_db.RL_MAX_LOOKAHEAD_S` (8 days — one day of slack
  over the longest real window) past the reading, and `cmd_limits` drops the
  stored ones after the date/window filters, printing a one-line count to stderr.
  Silently dropping them would leave a report nobody could reconcile against the
  row count in the table

Per instance:

| Field | Meaning |
|-------|---------|
| Peak | the fullest reading, raw float as stored |
| Samples | how many readings the write gate let through |
| Fill | first sample → **first** sample at the peak. Hours spent sitting at the peak are plateau, not fill, and the value is a floor: the window may have been filling before the first render saw it |
| pp/h | the rise (peak − **first reading taken**) over the fill span. Wall-clock, so an overnight gap between two renders counts as time the window took to fill — the rate to project a reset with, and the wrong one for "how fast does a working hour spend the quota". `None` (rendered `—`) for one sample or a window that never rose while it was watched, never 0, which would read as "not filling" |
| Spend | deduplicated record cost over the **fill span**, so it describes the same stretch of time the rise and the rate do. Filtered to one model family for a scoped window (the model the sample names) and for the Sonnet window (scoped by definition); session and week count every model |
| $/pp | Spend ÷ rise — an exchange rate, not an identity. The rate limit meters something Anthropic does not publish; this prices the points in the only unit this tool has. The footer's is the group's total spend over its total rise, not the mean of the rows: a window that rose one point would otherwise weigh as much as a week that rose forty |
| Hit | `round(peak) >= 100`. Rounded to match the write gate, which only passes a whole-percent move — 99.6 is the last sample a full window can leave behind |
| Account / Tier | attributed at the instance's **first** sample, via `AccountTimeline.label_at()` / `.tier_at()` — one bisect over the same events, so both answers come off the event in force when the window opened |

The spend join takes the full record path (`load_all_records`), not a `SUM` over
`ccreport_records`: dedup is what makes the number an answer, and summing the
rows raw reported $510 against a stretch that had actually cost $231. A window
that never rose prices as absent rather than as $0.00 — its fill span is a
single instant, and the spend of an instant would read as "this window was
free". So does every window when no corpus loaded at all.

Below each table, one caption line per **open** window (`resets_at` in the
future): where it stands, the reading it was first seen at, the rate, the
projected fill at reset, and what the points left are worth at that window's own
$/pp. A projection is per-window, so a column of it would be one number over a
stack of dashes; it also needs the words, because the reading it starts from is
where capture began and not where the window did. It extrapolates from the
**last sample**, not from now — both ends are then readings, and a machine that
has not rendered in six hours does not get those hours counted twice, once as
idle time inside the rate and once as time still to burn. Uncapped: over 100%
says the limit arrives before the reset does.

A blank tier is an event that predates the tier columns (or no event at all):
absent, not a change. `--since` / `--until` select samples, not instances, so a
window straddling the bound reports the peak, fill time and spend of the part
inside the range. Each window type gets its own table, summarized by instances
seen, how many hit 100%, the max peak and the group exchange rate; `--json`
prints the same structures with raw floats and epochs and no local-time
formatting.

More columns than a terminal has room for, so `_fit_columns` drops Tier, then
Account, then Samples until the table fits. Rich's own answer is to shave every
column by a character, which ellipsizes all of them and loses the table instead
of one column of it.

**Retention: nothing prunes this table** (macsetup-3u9n decided it). The write
gate holds one instance to ~100 rows, the report's questions are about all of
history — how often a window ever filled — and a window that filled a year ago
is unreconstructible once dropped: the live percentages were the only source.

---

## 10. Skills

Skills live in `claude/skills/`. See `claude/skills/README.md` for the full
inventory split by origin (ours vs installed packs).

When adding or removing skills, update `claude/skills/README.md` to keep the
listing accurate.
