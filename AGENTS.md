# Agent Instructions

## What is here

Two tools over one SQLite cache at `~/.cache/ccreport/cache.db`:

- `src/ccreport/ccreport.py` — the CLI. Reads Claude Code's JSONL session logs,
  prices them, and reports by day, month, project, session and account.
- `src/ccreport/statusline.py` — the status line Claude Code runs on every
  render. Reads the same cache and spawns `usage_api.py` detached to refresh it.
- `src/ccreport/ccu.py` — the quota dashboard. Runs `usage_api` as a subprocess
  and draws bars, reset countdowns and a weekly pace line from what it printed.

`src/ccreport/server/` is the merged database the machines push to: a FastAPI
app over its own SQLite file, run by Granian (`just serve`), configured by
`CCREPORT_SERVER_*` environment variables alone. `ingest.py` is the write side,
`pages.py` the server-rendered UI that mints the tokens it checks, and
`report_api.py` the merged reports `ccreport --server URL` renders.
`dashboard.py` is the merged spend page it serves at `/`.
`src/ccreport/push.py` is the other end: it sends this machine's records, run
by `ccreport server push` (or its older spelling `ccreport push`) or by a
detached spawn from the status line.

`burn.py` projects a rate-limit window to exhaustion and `forecast.py` projects
spend to a ceiling. Both are pure and stdlib-light, because `ccu` and the
status line read them.

`pricing.py`, `cache_db.py`, `exchange.py`, `aggregate.py`,
`project_identity.py` and `usage_api.py` are shared. `update_check.py` is
spawned by the status line alone. `bin/` holds the wrappers that Claude Code's
settings.json and a PATH entry point at.

The status line renders on every frame, which is why it imports nothing outside
the stdlib and defers `cache_db` (and with it sqlite3) into the functions that
touch the database. A new top-level import there costs every render.

Detailed calculations: `docs/calculation-reference.md`. Read on demand.

## Pipeline rules

- S/W rate limits come from Claude Code on stdin (`rate_limits.five_hour`,
  `.seven_day`), never from a fetch. The usage API is called only for what it
  alone supplies — Sonnet %, the scoped per-model limit, Extra spend — per
  `_api_fetch_needed`; otherwise the spawn is `--costs-only`
- The usage row is one global singleton; the cost summary is keyed by project. A
  fresh row therefore does not mean a fresh summary, and `_fetch_usage` spawns
  `--costs-only` on the fresh-row path too once this project's summary has aged
  out — otherwise one project's session starves every other project's. That gate
  is `cache_db.is_costs_refresh_blocked()`, never `is_fetch_blocked()`: the
  costs lock skips the API error backoff on purpose
- Both refresh spawns carry the window bounds as `--session-reset`/`--week-reset`,
  native stdin readings first and the cached row only as fallback; `usage_api.py`
  in turn treats them as the fallback for a response that omitted `resets_at`.
  Without a bound `compute_costs` omits `session_window_cost` instead of zeroing
  it, so the row would keep the previous window's total across a rollover
- Renders within 15 s (`FAST_TTL_S`) reuse the previous render's fetch results —
  git, battery, dsp, dcat, usage row, cost summary, session cost and the model
  families that session logged, and the rendered sandbox, sessions, account and
  update segments — from a per-session file
  (`_Fetched`, guarded by `_FAST_CACHE_SCHEMA`). Native S/W, clock, ctx% and
  countdowns stay live from stdin. The only bookkeeping the fast path keeps is
  cache-stats accumulation, keyed on `total_in` changing; account capture and
  rate-limit snapshots run on slow renders only
- A second per-session temp file (`.memo`, `_load_memo`/`_save_memo`) holds what
  no later render need re-derive and so has no TTL: the DSP verdict, fixed once
  the session's `claude` was launched, and the `(mtime_ns, size)` of
  `~/.claude.json` that the last account capture parsed
- The update line comes from `update_check.py`, spawned detached on slow renders
  when the stored stamp is older than `UPDATE_CHECK_INTERVAL_S` (12 h). The child
  writes that stamp on every outcome, failures included, so an unreachable API
  cannot become a spawn per render. A stored count is rendered only while
  `update_local_sha` still equals HEAD, so a pull silences the line instead of
  repeating a number the user has acted on. There are no tags and no releases —
  master is the release and the unit is a commit. `ccreport update` asks the
  same question inline, with no spawn and no interval, and writes its answer
  through the same keys
- Which rows a report has is `aggregate.py`; what they look like is
  `ccreport.py`. The row builders there are the one place the rollup path and
  the full record path meet, and the server folds records through the same
  functions, so nothing in `aggregate.py` may import rich. `tests/golden/`
  holds the pre-split rendering of every report — a diff there means the split
  changed output
- The server prices every record at ingest with its own `pricing.py` and stores
  the client's cost, if the log carried one, in a separate column. A model it
  has no price for fails that whole file with a reason in the response — never
  a stored zero, which is a week of money that looks like an idle week. Only
  the `<...>` pseudo-models cost a known zero
- Ingest sits outside the web UI's network allowlist and the UI sits inside it,
  wired in `factory.py`: a machine pushes from wherever it is and its token is
  what admits it, while the pages are reachable from home and nowhere else.
  `/static` is behind the gate with the pages it styles
- The client resolves before it sends: the account from `account_events`
  (`accounts.py`, shared with the CLI so a detached push needs no rich) and the
  project name through this machine's own override rules. The server holds no
  merge rules and treats the pushed name as final. Each record also carries the
  machine's UTC offset at that instant, which is what makes `server_records.day`
  the machine's calendar day rather than the server's
- `~/.config/ccreport/push.toml` is the machine's whole push policy — server,
  token, `restricted`, `allow`, `salt`, `networks` — written by
  `ccreport server connect` at mode 0600, one `[server."URL"]` table each.
  There are no environment variables for any of it. A `.restricted` marker
  sits beside it and wins: a push.toml that lost its `restricted = true` to an
  edit or an old backup redacts everything rather than reading as open
- A restricted machine sends every record's counts and strips the identity of
  any project outside `allow`: project and session become salted pseudonyms so
  the server can still group them, cwd and repo become null. The salt never
  leaves the machine. Changing `restricted`, `allow` or the local merge rules
  moves `policy_hash`, which clears the watermark *and* sets `replace` on every
  file — the server's skip is keyed on (mtime_ns, size), and the logs that
  carried the old names are closed and will never change again
- The network gate is `on_allowed_network`: a connected UDP socket per CIDR,
  which picks a route without sending a packet, so a VPN handing out an address
  in range counts as being on the network. Every CIDR is parsed before any is
  probed, since a machine that matched the first one would otherwise never
  reach the typo in the second. A blocked push writes no watermark and still
  stamps the attempt
- The push watermark is `push_state` in cache.db, written from the server's
  response and never from having sent it, so a rejected file is retried. The
  status line spawns `ccreport.push` but never imports it: its gate is one meta
  key, `push_next_at`, that the child writes on every outcome. How far the
  interval widens after a failure and which servers are due live in `push.py`,
  and a 401 is terminal — a revoked token stops the machine rather than
  knocking every interval
- The attempt stamp moves on every outcome, so it cannot date a push. What
  `ccreport server status` prints as `last push` is the separate `success`
  stamp `write_push_attempt(succeeded=True)` writes on the success path alone,
  and a failed attempt renders as itself with the `reason` stored beside it —
  a count of failures cannot tell connection-refused from a 500
- The dashboard's chart library is vendored under
  `server/static/vendor/`, not fetched from a CDN, so the page draws with no
  internet. Nothing updates it: a new version is a deliberate copy plus an edit
  to that directory's README, and `tests/test_dashboard.py` fails if any
  template or asset grows a remote origin
- `exchange.py` keeps the Norges Bank walk-back and the negative cache; where
  the rows land is swappable through `use_rate_store`, which is how the server
  converts against its own `exchange_rates` table instead of a client cache
- All pricing data lives in `pricing.py` — update only this file when prices
  change. Source: the LiteLLM pricing database; update `LAST_CHECKED` after
  verifying
- Cost windows come from `pricing.ROLLING_WINDOWS` (name, span, label). Adding
  one means editing that list plus `_COST_WINDOW_TOGGLES` in `statusline.py`;
  every other key list is derived. `tests/test_window_keys.py` fails if
  something drifts
- `pricing.window_start_epoch()` derives every session/week window start,
  `pricing.pace_days()` reads `CLAUDE_CODE_PACE_DAYS` for both the status line's
  pace segment and `ccu`'s pace line, and `pricing.project_key()` derives every
  cwd→projects-dir name. Do not re-derive any of the three inline
- Which project a record belongs to is `project_identity.py` plus
  `pricing.project_scope()` — `ccreport merge` rules apply to reports and status
  line alike. `ccreport._script_hash()` covers `project_identity.py`, so changing
  how a name is derived re-parses the corpus
- A bare `ccreport` serves days older than `ccreport.ROLLUP_WINDOW_DAYS` from the
  `ccreport_rollups` table — per-day aggregates built from the post-dedup,
  post-override, post-attribution record stream, never from a `GROUP BY` over
  `ccreport_records`. Any filter, `--json` and `adopt` take the full record path:
  a rollup row is one day of one session and has aggregated away what they
  select on. The rows are valid only against a fingerprint written in the same
  transaction, and it hashes `pricing.py` even though `_script_hash()`
  deliberately does not — a rollup freezes each record's cost and nothing
  recomputes a frozen sum
- The week bucket alone is also split by model family (`week_model_costs`), for
  the `weekly_scoped` quota's segment. `pricing.model_family()` keys both ends —
  the record's model ID when accumulating, the quota's display name when
  rendering — so neither side may match a family inline. It also decides whether
  that segment shows at all: under `SCOPED_MODE=current`, `_scoped_model_in_use`
  matches the quota's family against the families the session's own log carries
  (`compute_session_usage`), because a Task subagent spends on the model its
  definition names and stdin only ever reports the selected one. It is cached per file in
  `file_costs.week_model_json`, and an entry whose stored shape gains or loses a
  field needs `cache_db._COST_ENTRY_SCHEMA` bumped: mtime and size still match,
  so nothing else re-scans it and the missing field totals as zero
- Read-time dedup goes through `pricing.dedup_identity()`. It falls back to
  record content when the log carried no `message.id` or `requestId`; only the
  log's own key is persisted
- Which account a record billed to comes from the `account_events` change log,
  not the JSONL — a session log names no account. Slow-path renders append an
  event when `~/.claude.json` names a different identity or tier, and `ccreport`
  stamps each record at read time from the newest event at or before it.
  `cache_db._ACCOUNT_IDENTITY_COLS` answers "same account?",
  `cache_db.effective_limit_tier()` picks the user tier over the org one, and
  `ccreport adopt` claims pre-capture history — the rest is
  `docs/calculation-reference.md` section 9
- How full each rate-limit window got over time lives in `rate_limit_snapshots`,
  appended by slow-path renders from the live percentages, so an unobserved
  window leaves no history. `ccreport limits` is the reader: it groups by
  `(window, model, resets_at)` and prices each instance's rise against the
  records covering its fill span. `docs/calculation-reference.md` section 9.6
  has the write gate, the account attribution and the two defects stored history
  carries
- NOK conversion uses Norges Bank daily spot rates via `exchange.py`, cached in
  the `exchange_rates` table. Each record's USD cost is converted at its own
  Oslo-date rate, walking back up to 10 days for weekends and holidays. An
  unreachable API degrades to cached rates. Dates the API returned no observation
  for are negative-cached as rate `0.0` (`exchange._NO_OBSERVATION`) and filtered
  out of every lookup by `exchange._read_cached`. Weekends are skipped
  structurally — the series is business-day only — and dates newer than 5 days
  are never negative-cached, since that day's rate may simply not be published
  yet

## Formatting

`just fmt` runs `ruff check --fix` and no formatter. Line length is 110 and the
wrapping is hand-chosen: aligned SQL, rich column definitions and the status
line's env-var table all read as tables and `ruff format` rewraps them into
prose. Do not add `ruff format` to a recipe.

## Development

`uv` owns the environment: `uv add <pkg>`, `uv add --group dev <pkg>`,
`uv sync`, `uv run <cmd>`. `uv.lock` is the source of truth, so a package
installed any other way is absent from it and the next `uv sync` removes it.

`just --list` prints every recipe. `just fmt` formats, `just lint-all` runs
every linter, `just test` runs the suite.

Before committing: `just lint-all` and `just test` both pass, and every new
public function, route or CLI command has a pytest test under `tests/`.

Ask before `git push --force`, `git reset --hard`, `git checkout -- .` or
deleting a branch: the first rewrites history others have already pulled, and
the rest discard uncommitted work that no reflog can return.

Project creation runs `git init` without an initial commit, so `git log` and
`git diff HEAD` fail until you make one.

## Issue tracking

**dcat** is the issue tracker. Run `dcat prime --opinionated` at session start
and again after a compaction or a `/clear` — it prints the workflow rules and
the command reference, and is safe to run repeatedly. Then `dcat list` for the
backlog. Reserve `dcat list --agent-only` for autonomous runs with no human
present: it hides `--manual` issues, and `--manual` means human-in-the-loop,
not agent-skips.

Work in this order: (1) high-priority bugs, (2) high-priority features,
(3) standard bugs, (4) standard features. Ask the user which comes first when
two issues sit in the same tier.

Make separate parallel Bash tool calls for multiple `dcat` commands instead of
chaining them with `&&` and `echo` separators.

Mark an issue `in_progress` when you begin it and `in_review` when its work is
done, one issue at a time, so the status reflects what you are working on right
now. Working on several related issues at once is fine as long as each is
marked as you reach it.

When the user reports a bug or asks for a change, ask whether to create an
issue before you write code. Set labels with `--labels` (`cli`, `api`, `docs`,
`testing`, `refactor`, `ux`, `performance`). `--labels` takes one comma- or
space-separated value and a second `--labels` flag overwrites the first,
dropping labels silently — pass them all in one flag and confirm with
`dcat show`.

When research produces findings for an existing issue, ask as two separate
questions in order: "Should I update issue [id] with these findings?" and then
"Should I start working on the implementation?" — the user may want the issue
updated without starting work.

Wait for explicit user approval before closing an issue. When the work is done:
run `dcat update <id> --status in_review`, ask the user to test, ask "Can I
close issue [id] '[title]'?", and run `dcat close <id>` after they confirm.
