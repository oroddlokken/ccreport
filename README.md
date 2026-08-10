# ccreport

Token usage and cost reporting for Claude Code, and the status line that reads
the same cache.

Both work off the JSONL session logs Claude Code already writes under
`~/.claude/projects/`. Nothing is sent anywhere; the one network call is to the
Anthropic usage endpoint for quota percentages, and to Norges Bank for the
daily USD→NOK rate.

## ccreport

![ccreport](assets/ccreport.png)

```bash
ccreport                       # every report, last 30 days
ccreport daily --since 20260201
ccreport monthly
ccreport project --limit 10
ccreport session --breakdown
ccreport limits -w session     # rate-limit window history
```

Costs are priced per record from a pricing table in `src/ccreport/pricing.py`,
deduplicated by the log's own `message.id`/`requestId`, and grouped into
projects by git remote, then repo-root path, then directory name. A rename the
rules cannot see is a manual rule:

```bash
ccreport overrides                     # list the rules
ccreport merge ren.no ren-platform     # group one name into another
ccreport unmerge ren.no
```

## Status line

![status line](assets/statusline.png)

Model, context fill, session and weekly quota, cost windows, git state. Point
Claude Code's `settings.json` at the wrapper:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /path/to/ccreport/bin/statusline-command_x.sh"
  }
}
```

Segments are toggled with `CLAUDE_STATUSLINE_*` environment variables, which
that wrapper is the place to set. The full list, with defaults, is the module
docstring of `src/ccreport/statusline.py`.

The render is on the critical path of every frame, so it imports nothing
outside the stdlib and reaches the database only when it must. Refreshing the
usage row happens in a detached subprocess that outlives the render.

## Install

```bash
git clone <this repo> && cd ccreport
uv sync
```

Then put `bin/` on `PATH`, or install the CLI on its own:

```bash
uv tool install .
```

`bin/ccreport` and `bin/statusline-command_x.sh` run the modules out of the
checkout rather than an installed copy, so an edit takes effect on the next
invocation.

## Where the data lives

| Path | What |
|---|---|
| `~/.cache/ccreport/cache.db` | Parsed records, costs, usage row, rate-limit samples |
| `~/.local/share/ccreport/snapshots/` | One daily copy of the above, 14 kept |
| `~/.config/ccreport/ccreport.toml` | Optional `repo_roots` for project grouping |

Snapshots sit outside `~/.cache` so a cache-cleanup sweep cannot take the live
database and every backup of it at once.

These three used to live under `macsetup/claude` names. `ccreport migrate` moves
them, and any command does it for itself on the first run that finds the old
layout:

```bash
ccreport migrate --dry-run
ccreport migrate
```

## Development

`just --list` prints every recipe. `just test` runs the suite, `just lint-all`
runs ruff and pyright, `just fmt` applies ruff's autofixes.

`just fmt` deliberately does not run `ruff format`. See `AGENTS.md`.

Detailed calculations, cache schema and the reasoning behind the cost windows:
`docs/calculation-reference.md`.
