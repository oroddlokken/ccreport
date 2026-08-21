# Agent Instructions

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


## What is here

Two tools over one SQLite cache at `~/.cache/ccreport/cache.db`:

- `src/ccreport/ccreport.py` — the CLI. Reads Claude Code's JSONL session logs,
  prices them, and reports by day, month, project, session and account.
- `src/ccreport/statusline.py` — the status line Claude Code runs on every
  render. Reads the same cache and spawns `usage_api.py` detached to refresh it.
- `src/ccreport/ccu.py` — the quota dashboard. Runs `usage_api` as a subprocess
  and draws bars, reset countdowns and a weekly pace line from what it printed.
- `src/ccreport/quota_guard.py` — the verdict the `UserPromptSubmit` and
  `PreToolUse` hooks in `bin/quota-guard.sh` share. Stops a session over
  `CCQUOTA_STOP` and warns over `CCQUOTA_WARN`.

`src/ccreport/server/` is the merged database the machines push to: a FastAPI
app over its own SQLite file, run by Granian (`just serve`), configured by
`CCREPORT_SERVER_*` environment variables alone. `ingest.py` is the write side,
`pages.py` the server-rendered UI that mints the tokens it checks, and
`report_api.py` the merged reports `ccreport --server URL` renders.
`dashboard.py` is the merged spend page it serves at `/`.
`src/ccreport/push.py` is the other end: it sends this machine's records, run
by `ccreport server push` (or its older spelling `ccreport push`) or by a
detached spawn from the status line.

`windows.py` is the rate-limit window: one instance's peak, fill span and burn
rate, the `SpendIndex` that prices a span and counts its cache reads, and the
`WindowSpend` those produce. It imports no rich and no cache_db, because
`ccreport limits` renders these as tables and `server/limits.py` renders the
merged ones as pages — and because cache_db reads `rl_window_key` from it, so
the identity a sample is stored under and the identity a report groups on stay
one rule.

`forecast.py` projects spend to a ceiling. It is pure and stdlib-light, because
`ccu` and the status line read it.

`scan.py` reads the JSONL logs into `ccreport_files` and `ccreport_records` and
is the only writer of either. It imports no rich, because the push refreshes
that cache before it sends and the CLI is not always what runs first.

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
- No render derives the `*_project_cost` split itself: that walks every JSONL
  file in the project on the frame Claude Code is waiting on. A summary too old
  for the merge still supplies the split, up to
  `COST_SUMMARY_FALLBACK_MAX_AGE`, and past that the cost windows show the
  machine-wide total alone until the detached refresh lands. Only the project
  keys are taken from it — `_fetch_usage` never sees that read, since a hit
  would suppress the refresh spawn it gates on the fresh one
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
  the session's `claude` was launched, and the `(path, mtime_ns, size)` of the
  config file that the last account capture parsed — the path is in the stamp
  because `statusline._config_json_path` picks the first of
  `~/.claude-config/.claude.json`, `~/.claude/.claude.json` and
  `~/.claude.json` that exists, and a file appearing in a config directory is a
  different file rather than a rewrite of the one the stamp came from
- The quota guard reads and never fetches. S and W come from the `.quota` file a
  slow render writes while `CCQUOTA_STOP` is set — `rate_limit_snapshots` stores
  a row only when a reading moves, so its newest ts dates the last change and
  cannot date the last observation — and Sonnet and scoped come from the usage
  row. Each source carries its own budget, `NATIVE_MAX_AGE_S` against a render's
  cadence and `API_MAX_AGE_S` against `statusline.USAGE_HEARTBEAT_S`; past it the
  window is unknown and blocks, while a null column on a row inside the budget is
  the plan not having that quota and is not watched. `read_windows` opens its own
  read-only connection rather than `cache_db.get_connection`, whose bootstrap and
  daily snapshot do not belong before every tool call
- The update line comes from `update_check.py`, spawned detached on slow renders
  when the stored stamp is older than `UPDATE_CHECK_INTERVAL_S` (36 h). The child
  writes that stamp on every outcome, failures included, so an unreachable API
  cannot become a spawn per render. A stored count is rendered only while
  `update_local_sha` still equals HEAD, so a pull silences the line instead of
  repeating a number the user has acted on. There are no tags and no releases —
  master is the release and the unit is a commit. `ccreport update` asks the
  same question inline, with no spawn and no interval, and writes its answer
  through the same keys
- A rate-limit sample is pushed like a record: `push.build_samples` resolves the
  account off `account_events` and sends everything newer than a per-server
  watermark (`cache_db.read_push_samples_at`), which `--full` and a policy
  change clear with the file one. Nothing is redacted — a sample carries a
  window name, a percentage, a reset time and a model, none of which is a
  project or a session — so a restricted machine sends the rows an open one
  does. The server keys them on (machine, window, ts) and REPLACEs, so a
  re-offered sample is a no-op
- `ccreport server disconnect <url>` is the other end of `connect`. It removes
  the entry from push.toml through `push.remove_server` and clears every local
  row keyed on that URL through `cache_db.forget_server`: `push_state`, the
  per-server meta keys enumerated in `_PUSH_META_NAMES`, and both remote cost
  tables. Those last are the ones that matter — left behind, `-A` and the status
  line's merged windows go on adding a server nobody pushes to. It previews
  what will go and confirms first (`--yes` skips), because the token's plaintext
  is nowhere else and reconnecting needs one minted afresh. Nothing on the
  server is touched, and the `.restricted` marker survives: it claims this
  machine has pushed under a restriction, not that one server did
- What the client and the server agree on over the wire is
  `protocol.PROTOCOL_VERSION`, one integer bumped by hand when the bytes change:
  a new payload section, a renamed or retyped field, a response key a client
  reads. Not the package version, which is `0.1.0` and has never moved, and not
  a commit sha, which differs on every commit and so can only ever be advisory.
  A refactor that moves no bytes needs no bump, and neither does a schema change
  the payload does not expose — the two `MIGRATION_CHAIN`s are the versions for
  those. `protocol.py` imports nothing, because `push.py` must stay clear of
  rich and `server/ingest.py` is on the other side of that line. Every ingest
  request carries it and every response returns it, `/health` included. A client
  *ahead* of the server is refused with 409 before the machine row is written
  and treats it as terminal the way it treats a 401 — a half-read payload
  answered 200 is the failure this exists to catch, so `post_batch` also refuses
  a reply whose `protocol` is below its own, which is the only thing that
  catches a server predating the field. A client *behind* pushes normally;
  that direction is a line in `ccreport server status`, never a refusal.
  `ccreport server connect` checks it at setup and writes no push.toml on a
  mismatch
- `ccreport server pull` and `server sync` bring back what the account's *other*
  machines spent, so the status line's cost windows and `ccreport -A` are the
  account's total rather than this machine's. The transport is the ingest
  response, not `/v1/report`: that endpoint sits inside the network allowlist
  and would answer nothing when the laptop is away from home, which is when the
  other machine's spend is most missing. The exclusion is by dedup identity, not
  by machine id — `server/pull.py` drops the asking machine's rows and then any
  remaining record whose dedup key that machine also pushed, because the server
  dedups across the set it folds and a client adding its local total to that
  set's leftovers cannot see the overlap. Two grains, since neither derives the
  other: per-minute cost buckets bounded to the longest `pricing.ROLLING_WINDOWS`
  span, which the client sums into windows so that list stays out of the
  protocol, and one row per (machine, day, project) which keeps every day.
  `remote_window_costs` and `remote_day_costs` are their own tables and are
  never mixed into the corpus — `scan.py` stays the only writer of
  `ccreport_records`, and a per-machine row is what makes the staleness marker
  per contributor possible. Both are scoped to `read_latest_account()` on the
  read side as well as the write side, so a login switch cannot add a previous
  account's spend to this one's windows; those rows stay rather than being
  deleted, and the read filter is what makes that safe. `ccreport` never opens a
  socket, `-A` included: only `server push`, `server pull`, `server sync` and
  the status line's detached spawn talk to a server. The spawn is the existing
  `python -m ccreport.push`, whose `run_once` pulls by default — one round trip
  rather than the separate `pull_next_at` gate the epic first sketched. The
  session table is the one `-A` cannot merge and says so; `--json` is this
  machine's records whatever `-A` says, since an entry there is one API call
- The Extra column is the only real money in a window report; everything else
  there is an API-price valuation. Its series is `extra_usage_snapshots`,
  cumulative dollars within a billing month, written by slow status-line renders
  and pruned to 31 days by `cache_db.write_usage_cache`. `push.build_extra`
  sends it behind its own watermark (`read_push_extra_at`, cleared by `--full`
  and a policy change with the other two) into the server's
  `extra_usage_samples`, which is never pruned — past 31 days the server's copy
  is the only one, the same answer `ccreport_archive` gives on the client. The
  reading is the *account's* cumulative spend, so two machines on one account
  report the same dollars rather than halves of them: `limits._instance_extra`
  answers a window from one machine's series alone, whichever has the most
  readings inside the span. Merging them would let a lagging reading land below
  a fresher one, and a drop in this series is what `windows.ExtraIndex` reads as
  the billing month rolling over
- A quota belongs to an account, not to a machine, so `server/limits.py` groups
  samples on (account, window, model, reset) and two laptops signed into one
  account draw one fill curve. It prices each window against `reports.load` —
  the full record path, bounded to the window spans — because a 5-hour window is
  priced over hours and a grouped row has folded the hour away. `/limits` is
  registered before the `/{dimension}/{key}` catch-all, and one window's page
  carries the model and the account in the query string: both can hold a slash
- Which rows a report has is `aggregate.py`; what they look like is
  `ccreport.py`. The row builders there are the one place the rollup path and
  the full record path meet, and the server folds records through the same
  functions, so nothing in `aggregate.py` may import rich. `tests/golden/`
  holds the pre-split rendering of every report — a diff there means the split
  changed output
- A schema change reaches a database that already exists only through
  `migrations.py`: a numbered `Step` appended to `MIGRATION_CHAIN` in
  `cache_db.py` or `server/db.py`, one version above the last. `SCHEMA_VERSION`
  is the chain head and is never hand-edited. The `CREATE ... IF NOT EXISTS`
  scripts still carry a new table or index, and `Step(N, "name")` with no
  callable is what moves the version that re-runs them — but a column added to a
  table that is already there is skipped by the very `IF NOT EXISTS` that makes
  the script safe to re-run, so it needs a step that does the `ALTER`. Steps run
  inside a transaction with the stamp, so one may not `BEGIN`, `COMMIT` or turn
  `foreign_keys` off, and needs no meta flag: its version is the flag. An entry
  that has shipped is never edited — `migrations.run` records each step's source
  hash and refuses to start where the recorded one no longer matches. The five
  meta-flagged repairs in `cache_db._run_migrations` and `_ADDED_COLUMNS` are the
  frozen pre-baseline bootstrap at 11, and nothing renumbers them
- The server prices every record at ingest with its own `pricing.py` and stores
  the client's cost, if the log carried one, in a separate column. A model it
  has no price for fails that whole file with a reason in the response — never
  a stored zero, which is a week of money that looks like an idle week. Only
  the `<...>` pseudo-models cost a known zero
- Ingest sits outside the web UI's network allowlist and the UI sits inside it,
  wired in `factory.py`: a machine pushes from wherever it is and its token is
  what admits it, while the pages are reachable from home and nowhere else.
  `/static` is behind the gate with the pages it styles, through
  `middleware.NetworkGated` rather than the dependency: `app.mount` takes no
  `dependencies`, so the check has to sit one layer down in the ASGI app
- The dashboard is `/`; everything that administers the server is under
  `/settings` — machines, minting and account names, forms and redirects
  included. `/tokens/{hash}/revoke` and `/delete` are the exception: they
  redirect through Referer and are posted to from whichever page listed the
  token
- `/{dimension}/{key}` is one entity's page, for each of `dashboard.SCOPES`. It
  is registered last in `pages.py` and matches whatever the named routes did
  not, which is why `factory.py` mounts `/static` before the router — the
  catch-all would otherwise answer 404 for every asset. The scope matches the
  string the breakdown table shows, not a stored column, so an alias, a machine
  label and a redacted project bucket each reach their own rows, and it is
  never cached: `cached_build` holds the whole-server view per range, which is
  the page a browser opens over and over
- The three `dashboard.PERIODS` — day, week, month — are their own range and
  print no toggle: the span is the page. A day charts by hour, so it is the one
  build that calls `reports.load`; a week and a month chart by day and take
  `load_grouped`, which folds the hour away. All three widen the ts window by a
  day at each end and match `day_key()` afterwards, because `day` is the
  machine's calendar day and a machine on another clock keeps records whose
  instant falls outside this server's day. `period_span` counts the axis over
  `date` arithmetic rather than `(end - start).days`, which is one short in a
  month that changed clocks. A week is keyed on any date it holds and opens on
  that date's Monday, so its seven URLs draw one page, and a key the period
  cannot parse is a 404 rather than an empty page that reads as an idle month
- A detail page draws four charts rather than one with a toggle, and each has
  one scale: cost, cost by model, tokens by kind and calls do not share an
  axis. Series colours come from `static/palette.js` in fixed order, so a
  filter that drops one series never repaints the others, and the six hues are
  validated as a set against the dark surface — re-run the dataviz validator
  before changing a value. A seventh series folds into `Other` at
  `dashboard.TRACE_LIMIT`
- A stylesheet or script is linked through the `asset()` template global, never
  as a bare `/static/…` path. It stamps the URL with the file's mtime, because
  StaticFiles sends no Cache-Control and a browser is otherwise free to hold
  yesterday's app.css against a page you just changed
- The client resolves before it sends: the account from `account_events`
  (`accounts.py`, shared with the CLI so a detached push needs no rich) and the
  project name through this machine's own override rules. The server holds no
  merge rules and treats the pushed name as final. Each record also carries the
  machine's UTC offset at that instant, which is what makes `server_records.day`
  the machine's calendar day rather than the server's
- `~/.config/ccreport/push.toml` is the machine's whole push policy — server,
  token, `restricted`, `allow`, `salt`, `networks`, `interval_minutes` — written by
  `ccreport server connect` at mode 0600, one `[server."URL"]` table each.
  There are no environment variables for any of it. The mint page types the
  networks and the opt-in list into the connect command it prints and stores
  neither: the server never holds a machine's policy, only the line that sets
  it. A `.restricted` marker
  sits beside it and wins: a push.toml that lost its `restricted = true` to an
  edit or an old backup redacts everything rather than reading as open
- A restricted machine sends every record's counts and strips the identity of
  any project outside `allow`: project, session, cwd and repo all become null.
  A pseudonym per project would have drawn a row each, and the count of private
  projects with a price on each is the shape of the work. `reports.project_display`
  folds every null project into one bucket per account instead —
  `<alias>-aggregated`, or `<account label>/aggregated` where nothing is named,
  or `<uuid>/aggregated` where the record carried no label. The alias replaces
  the whole account segment, slash included. `push.pseudonym` and
  `pseudo_session` are kept with no caller, as is `salt`, so re-introducing a
  grouping key needs no config migration
- Changing `restricted`, `allow`, the local merge rules or
  `push.REDACTION_SHAPE` moves `policy_hash`, which clears the watermark *and*
  sets `replace` on every file — the server's skip is keyed on (mtime_ns, size),
  and the logs that carried the old names are closed and will never change
  again. REDACTION_SHAPE is what a change to `redact` has to move: the salt no
  longer varies with the redaction, so nothing else in that material would
- What the server calls an account is `reports.account_display`: the
  `account_aliases` row, then `server_records.account_label`, then the uuid. The
  /settings/accounts page writes it and `server_records` is never rewritten, so
  the login email stays in the history and leaves the screen. `db.content_stamp` reads
  that table for the same reason it reads `ingest_files` — a rename has no push
  behind it and the dashboard's cache would otherwise keep drawing the email.
  An alias also filters: `db.accounts_with_alias` widens the account clause so a
  name typed off the dashboard selects what the email does
- What it calls a machine is `machines.label`, typed on the /settings/machines
  page and never touched by a push. `db.set_machine_label` stamps
  `label_updated_at` for the same reason an alias stamps `updated_at`, and
  `content_stamp` reads it; a blank field stores the machine_id rather than an
  empty label, which every reader would draw as a machine with no name
- What it calls a project is `project_aliases`, keyed on (machine_id, project)
  and typed on the /settings/projects page: two machines that checked one repo
  out under different names are one row once both pairs carry the same alias,
  and a name is only unique within the machine that pushed it, so the key
  cannot be the name alone. `reports.project_display` reads it,
  `db.projects_with_alias` widens the project clause in `_clauses` *and* in
  `_dedup_clause`, and `content_stamp` reads both its count and its max. The
  pair rides in form fields rather than the path — a project name carries
  slashes. A NULL project has no row here: it is a restricted machine's
  redacted bucket, named off the account
- Revoking a token stamps `revoked_at`; deleting it removes the row. Both stop
  the next push, and which one to reach for is whether the machine is still out
  there. `POST /settings/machines/{id}/delete` takes the machine, its tokens,
  its `ingest_files` and every record it pushed, through the ON DELETE CASCADE
  those three declare — behind the machine id typed into a form field, because
  this server is the only copy of those records once the machine's logs have
  rotated
- The network gate is `on_allowed_network`: a connected UDP socket per CIDR,
  which picks a route without sending a packet, so a VPN handing out an address
  in range counts as being on the network. Every CIDR is parsed before any is
  probed, since a machine that matched the first one would otherwise never
  reach the typo in the second. A blocked push writes no watermark and still
  stamps the attempt
- `push.run_once` calls `scan.refresh_cache()` once a server has cleared every
  gate — after the interval, the terminal state and the network check, so a run
  that sends nothing parses nothing. It sends what `ccreport_files` holds and
  only a parse writes that table, so without this a machine whose reports
  nobody opens offered the corpus as it stood when someone last typed
  `ccreport` while every attempt stamped a success
- The push watermark is `push_state` in cache.db, written from the server's
  response and never from having sent it, so a rejected file is retried. The
  status line spawns `ccreport.push` but never imports it: its gate is one meta
  key, `push_next_at`, that the child writes on every outcome. How far the
  interval widens after a failure and which servers are due live in `push.py`,
  and a 401 is terminal — a revoked token stops the machine rather than
  knocking every interval. The widening is `push.attempt_interval()` alone:
  `due()` and `next_attempt_at()` both call it, so a per-server
  `interval_minutes` doubles from its own base and `MAX_INTERVAL_S` never
  shortens a base set above it
- The attempt stamp moves on every outcome, so it cannot date a push. What
  `ccreport server status` prints as `last push` is the separate `success`
  stamp `write_push_attempt(succeeded=True)` writes on the success path alone,
  and a failed attempt renders as itself with the `reason` stored beside it —
  a count of failures cannot tell connection-refused from a 500
- The dashboard folds grouped rows, never records: `reports.load_grouped` asks
  SQL for one row per (machine, account, project, model, day) and hands back a
  `UsageRecord` per group carrying its summed tokens, its summed cost and its
  call count in `count` — the rollup trick the CLI plays, through the same
  `aggregate.py`. Half a million calls reach Python as a few thousand groups.
  The dedup repeats every filter but the date bounds: which copy of a synced
  call survives depends on the set being deduped, and no ts bound can split a
  pair. `dashboard.cached_build` then holds one view per range toggle against
  `db.content_stamp` and the render's local date, so a page is rebuilt when a
  push moved the records or the day rolled over, and not per request
- Anything that sums or counts `server_records` goes through
  `reports._dedup_clause`, and lives in `reports.py` for that reason —
  `account_overview` is there rather than in `db.py`, which holds no total of
  its own. Two machines that share session logs push the same call twice, so a
  raw `SUM` is not a smaller answer than the deduped one but a different
  number: /settings/accounts drew 2.2x the dashboard until it was moved. The
  one count left raw is `db.machine_overview`'s, which is what a machine
  pushed and what deleting it takes away — the column says `Pushed`
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
- A record's `costUSD` is its cost wherever this machine prices it — the JSONL
  parse (`pricing._line_cost`), a cached record (`pricing._rec_cost`) and the
  reports (`aggregate.UsageRecord.cost`) — and `calc_cost` answers only where
  the log carried none. One reader pricing from tokens while another kept the
  logged figure put a 24h window above `all_time` on the same corpus, so a new
  read path picks neither on its own
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
  line alike. `scan._script_hash()` covers `scan.py` and `project_identity.py`,
  so changing how a name is derived re-parses the corpus and editing a report
  renderer no longer does
- A bare `ccreport` serves days older than `ccreport.ROLLUP_WINDOW_DAYS` from the
  `ccreport_rollups` table — per-day aggregates built from the post-dedup,
  post-override, post-attribution record stream, never from a `GROUP BY` over
  `ccreport_records`. Any filter, `--json` and `adopt` take the full record path:
  a rollup row is one day of one session and has aggregated away what they
  select on. The rows are valid only against a fingerprint written in the same
  transaction, and it hashes `pricing.py` even though `_script_hash()`
  deliberately does not — a rollup freezes each record's cost and nothing
  recomputes a frozen sum
- `ccreport archive` folds the purged half of `ccreport_records` into
  `ccreport_archive` at day grain and deletes the rows. That table is a store,
  not a cache: nothing rebuilds it, and it carries the identity raw —
  `project`, `cwd`, `repo`, `dir_prefix` — plus `min_ts`, so `ccreport merge`
  and `ccreport adopt` still re-attribute an archived day. A file is only ever
  folded whole, only when its JSONL is gone from disk, only behind a cutoff that
  is the older of `ARCHIVE_MIN_AGE_DAYS` and the oldest `rate_limit_snapshots`
  reading minus a window span (`ccreport limits` prices a window against the
  records covering its fill span), and never when a change-log event falls
  inside its span — a row that straddled one could not be split by a later
  adoption. Every path that would read a folded file as a file with no spend
  reads the archive instead: `_load_full` adds it as synthetic records outside
  the dedup, `pricing._build_orphan_alltime` adds its costs,
  `load_ccreport_file_identities` returns its directory, `_ccr_totals` counts
  its calls so the sanity guard stays quiet, and `push.changed_files` skips the
  file outright — the server keys a file on (machine, path) and replaces what it
  holds, so offering an emptied one would erase history it is the only copy of.
  `ccreport server push --full` therefore stops being a way to rebuild a server
  from this machine
- The week bucket alone is also split by model family (`week_model_costs`), for
  the `weekly_scoped` quota's segment. `pricing.model_family()` keys both ends —
  the record's model ID when accumulating, the quota's display name when
  rendering — so neither side may match a family inline. It also decides whether
  that segment shows at all: under `SCOPED_MODE=current`, `_scoped_model_in_use`
  matches the quota's family against the families the session's own log carries
  (`compute_session_usage`), because a Task subagent spends on the model its
  definition names and stdin only ever reports the selected one. It is cached per file in
  `file_costs.week_model_json`, and an entry whose stored shape gains or loses a
  field — or whose pricing rule changes — needs `cache_db._COST_ENTRY_SCHEMA`
  bumped: mtime and size still match, so nothing else re-scans it and what it
  stored totals as it stands
- Read-time dedup goes through `pricing.dedup_identity()`. It falls back to
  record content when the log carried no `message.id` or `requestId`; only the
  log's own key is persisted
- Which account a record billed to comes from the `account_events` change log,
  not the JSONL — a session log names no account. Slow-path renders append an
  event when the config file names a different identity or tier, and `ccreport`
  stamps each record at read time from the newest event at or before it.
  `cache_db._ACCOUNT_IDENTITY_COLS` answers "same account?",
  `cache_db.effective_limit_tier()` picks the user tier over the org one, and
  `ccreport adopt` claims pre-capture history — the rest is
  `docs/calculation-reference.md` section 9
- `account_events.source` says which of three things a row is, and the readers
  turn on it rather than on `ts`: a `capture` is a reading of the config file
  and is permanent, a `backfill` is a plan change declared off a billing
  receipt, an `adopt` is the ts=0 claim. `read_latest_account()` selects
  captures alone — it is what `ccreport adopt` copies, and a claim must not
  decide who a machine is — and `_pre_capture_records` takes its boundary from
  them for the same reason. It stays out of `_ACCOUNT_COLS`, so the identity
  comparison and `_ACCOUNT_SELECT` never see it, and `clear_backfilled_accounts`
  is the only delete besides the adoption's
- A tier is a state, not an event: `accounts._carried_tiers` carries an
  account's last reading forward across an event that recorded none, and never
  across a login. An empty tier column is silence — the columns arrived after
  the log did, Claude Code refreshes the blob they come from only on /login,
  and a declared plan change records no reading at all — so letting one clear
  the tier would end every declared stretch at the next render that caught
  nothing
- `tier_timeline.py` is the declared plan history: the TOML `[[tier]]` format,
  `parse`/`render`, and the bisect that answers a moment with a tier. Both ends
  read it — `ccreport tiers <file>` writes `account_events` rows from it, the
  server stores it per account in `account_tiers` and resolves every folded
  row's tier at read time — so it imports nothing but the stdlib, like
  `protocol.py`. Nothing validates a tier string against a list: the names come
  from Anthropic, and a backfill that refused an unrecognized one would fail on
  the plan it was written to record
- The server's tier is declared, never pushed. A record carries none, and a
  client that learned to send one could only stamp the files it still has —
  `push.changed_files` offers a file whose (mtime_ns, size) moved and skips
  archived ones outright, so a corpus whose older logs have rotated away would
  keep NULL whatever `--full` does. `account_tiers` is read once per report and
  handed to the row builders, the way `account_aliases` is, and
  `db.content_stamp` reads its count and max because typing one in has no push
  behind it. `set_account_tiers` replaces one account's rows wholesale: the
  timeline is a document, and merging a paste into what was there would leave a
  change the person deleted still standing
- How full each rate-limit window got over time lives in `rate_limit_snapshots`,
  appended by slow-path renders from the live percentages, so an unobserved
  window leaves no history. `ccreport limits` is the reader: it groups by
  `(window, model, resets_at)` and prices each instance's rise against the
  records covering its fill span. `docs/calculation-reference.md` section 9.7
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
