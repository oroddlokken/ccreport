# ccreport

Token usage and cost reporting for Claude Code, a quota dashboard, and the
status line: three commands over one cache.

`ccreport` and the status line work off the JSONL session logs Claude Code
already writes under `~/.claude/projects/` (or `~/.config/claude/projects/`).

## Status line

![status line under a Claude Code session, showing model, context, quota and cost windows](assets/statusline.png)

Model, context fill, session and weekly quota, cost windows, git state.

Segments are toggled with `CLAUDE_STATUSLINE_*` environment variables, `1` on
and `0` off. The full list, with defaults, is the module docstring of
`src/ccreport/statusline.py`.

`bin/statusline-command_x.sh` reads each toggle as `${VAR:-default}`, so a
wrapper of your own that exports them and hands over wins. Keep it outside the
checkout and your settings survive every `git pull`:

```bash
#!/usr/bin/env bash
# ~/.claude/statusline-wrapper.sh — chmod +x

export CLAUDE_STATUSLINE_HOSTNAME=1
export CLAUDE_STATUSLINE_DOGCAT=1
export CLAUDE_STATUSLINE_SCOPED_MODE=always

# "$@" carries the arguments Claude Code passes; the JSON arrives on stdin,
# which exec leaves alone.
exec bash /path/to/ccreport/bin/statusline-command_x.sh "$@"
```

Point Claude Code's `settings.json` at that wrapper:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /path/to/statusline-wrapper.sh"
  }
}
```

The status line imports nothing outside the stdlib, so any `python3` runs it;
it works before `uv sync` has. Quota percentages are refreshed by a detached
process, so a fresh reading lands on a later render.

### The SessionStart hook

A render memoizes what it reads from the running claude process in a file under
`TMPDIR`, keyed by session id. `--resume` hands the same id to a new process, so
`bin/clear-statusline-memo.sh` deletes that file. Install it as a `SessionStart`
hook, the one moment those readings change:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash /path/to/ccreport/bin/clear-statusline-memo.sh"
          }
        ]
      }
    ]
  }
}
```

`SessionStart` takes no matcher — it fires on startup, resume, clear, compact
and fork alike. The hook needs `jq`, prints nothing (a `SessionStart` hook's
stdout is appended to the session's context) and exits 0 on anything it cannot
parse.

## ccreport

![ccreport's daily, monthly and project tables in a terminal](assets/ccreport.png)

```bash
ccreport                                    # every report, all history
ccreport daily --since 20260201 --breakdown
ccreport monthly
ccreport project --limit 10
ccreport session --limit 10
ccreport limits -w session                  # rate-limit window history
```

Costs are priced per record from a pricing table in `src/ccreport/pricing.py`,
deduplicated by the log's own `message.id`/`requestId`, and grouped into
projects by git remote, then repo-root path, then directory name. A rename the
rules cannot see needs a manual rule:

```bash
ccreport overrides
ccreport merge company company-platform  # company's records report as company-platform
ccreport unmerge company
```

## ccu

The quota dashboard, the same numbers as Claude Code's `/usage` screen, from
the same endpoint, cached for up to ten minutes:

```bash
ccu             # session, week, per-model and Extra quotas
ccu --force     # bypass the 10-minute cache
ccu --json      # the raw API body, unrendered; always a live fetch, never cached
```

The week gets a pace line comparing usage against elapsed time.
`CLAUDE_CODE_PACE_DAYS` sets the day the quota is meant to be gone by, counting
from whichever weekday the window starts on. It defaults to 7, and both this
and the status line's pace segment answer to it.

## Install

```bash
git clone git@github.com:oroddlokken/ccreport.git && cd ccreport
uv sync
```

Then put `bin/` on `PATH`, or install the CLI on its own:

```bash
uv tool install .
```

Every wrapper in `bin/` runs the modules out of the checkout rather than an
installed copy, so an edit takes effect on the next invocation.

## Where the data lives

| Path | What |
|---|---|
| `~/.cache/ccreport/cache.db` | Parsed records, costs, usage row, rate-limit samples |
| `~/.local/share/ccreport/snapshots/` | One daily copy of the above, 14 kept |
| `~/.config/ccreport/ccreport.toml` | Optional `repo_roots` for project grouping |

Snapshots survive a `~/.cache` wipe.

These three used to live under `macsetup/claude` names. Any command migrates
them for itself on the first run that finds the old cache:

```bash
ccreport migrate --dry-run
ccreport migrate
```

## Development

`just --list` prints every recipe. `just test` runs the suite, `just lint-all`
runs ruff and pyright, `just fmt` applies ruff's autofixes but deliberately not
`ruff format`; see `AGENTS.md` for why.

Detailed calculations, cache schema and the reasoning behind the cost windows:
`docs/calculation-reference.md`.
