# Agent Instructions

## What is here

Two tools over one SQLite cache at `~/.cache/ccreport/cache.db`:

- `src/ccreport/ccreport.py` — the CLI. Reads Claude Code's JSONL session logs,
  prices them, and reports by day, month, project, session and account.
- `src/ccreport/statusline.py` — the status line Claude Code runs on every
  render. Reads the same cache and spawns `usage_api.py` detached to refresh it.

`pricing.py`, `cache_db.py`, `exchange.py`, `project_identity.py` and
`usage_api.py` are shared by both. `bin/` holds the wrappers that Claude Code's
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
- Both refresh spawns carry the window bounds as `--session-reset`/`--week-reset`,
  native stdin readings first and the cached row only as fallback; `usage_api.py`
  in turn treats them as the fallback for a response that omitted `resets_at`.
  Without a bound `compute_costs` omits `session_window_cost` instead of zeroing
  it, so the row would keep the previous window's total across a rollover
- Renders within 15 s (`FAST_TTL_S`) reuse the previous render's fetch results —
  git, battery, dsp, dcat, usage row, cost summary, session cost, and the
  rendered sandbox and sessions badges — from a per-session temp file
  (`_Fetched`, guarded by `_FAST_CACHE_SCHEMA`). Native S/W, clock, ctx% and
  countdowns stay live from stdin. The only bookkeeping the fast path keeps is
  cache-stats accumulation, keyed on `total_in` changing; account capture and
  rate-limit snapshots run on slow renders only
- A second per-session temp file (`.memo`, `_load_memo`/`_save_memo`) holds what
  no later render need re-derive and so has no TTL: the DSP verdict, fixed once
  the session's `claude` was launched, and the `(mtime_ns, size)` of
  `~/.claude.json` that the last account capture parsed
- All pricing data lives in `pricing.py` — update only this file when prices
  change. Source: the LiteLLM pricing database; update `LAST_CHECKED` after
  verifying
- Cost windows come from `pricing.ROLLING_WINDOWS` (name, span, label). Adding
  one means editing that list plus `_COST_WINDOW_TOGGLES` in `statusline.py`;
  every other key list is derived. `tests/test_window_keys.py` fails if
  something drifts
- `pricing.window_start_epoch()` derives every session/week window start, and
  `pricing.project_key()` every cwd→projects-dir name. Do not re-derive either
  inline
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
  rendering — so neither side may match a family inline. It is cached per file in
  `file_costs.week_model_json`, and an entry whose stored shape gains or loses a
  field needs `cache_db._COST_ENTRY_SCHEMA` bumped: mtime and size still match,
  so nothing else re-scans it and the missing field totals as zero
- Read-time dedup goes through `pricing.dedup_identity()`. It falls back to
  record content when the log carried no `message.id` or `requestId`; only the
  log's own key is persisted
- Which account a record billed to comes from the `account_events` change log,
  not the JSONL — a session log names no account. The status line checks
  `~/.claude.json` on every slow-path render and reparses it for `oauthAccount`
  whenever its `(mtime_ns, size)` moved (so a mid-session `/login` can be
  recorded up to `FAST_TTL_S` late, and spend in that gap attributes to the old
  account) and appends a row only when it differs from the newest one. A row
  carries the four identity fields plus three tiers (`seat_tier`,
  `user_rate_limit_tier`, `organization_rate_limit_tier`), and the change gate
  compares all seven — so a seat or plan change on an unchanged login appends a
  row too, which is how the log dates a tier change.
  `cache_db._ACCOUNT_IDENTITY_COLS` is the subset that answers "same account?";
  `cache_db.effective_limit_tier()` picks the user tier over the org one.
  `ccreport` stamps each record at read time from the newest event at or before
  its timestamp, so records predating the log report as `unknown` and a later
  event re-attributes past reports with no re-parse. `ccreport adopt` claims that
  pre-capture history by writing one backdated event at `ts=0` (undo with
  `--remove`); it is the only thing that ever deletes from `account_events`, and
  only that row
- How full each rate-limit window got over time lives in `rate_limit_snapshots`,
  appended by slow-path renders — the live percentages are the only source, so an
  unobserved window leaves no history. A sample lands only when the whole-percent
  reading moves and 300 s have passed, or when `resets_at` names a new window
  instance; `used_pct` keeps the raw float. `resets_at` is stored normalized to
  the whole minute (`cache_db.rl_window_key`) and a reading more than
  `RL_MAX_LOOKAHEAD_S` out is refused — the API's float drifts between fetches of
  one window, which bypassed the gate entirely, and Claude Code sends
  `9999999999` where it has no reset. Both defects are in stored history, so
  `ccreport limits` re-normalizes on read and drops the placeholders with a note.
  Rows carry no account: attribute by `ts` against `account_events`, as
  `ccreport` does for records. `ccreport limits` is the reader — it groups by
  `(window, model, resets_at)`, and prices each instance's rise against the
  records covering its fill span, through `load_all_records` (dedup is what makes
  the number an answer) bounded to the span the samples cover. A scoped or Sonnet
  window counts one model family, session and week every model. Nothing prunes
  this table; the write gate is what bounds it
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
the rest discard uncommitted work that no reflog can return. `git push` itself
can reach production — `.deploy.remote.settings.toml` sets `git_push_after`,
so a deploy pushes as part of its run.

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
