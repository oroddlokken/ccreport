# Agent Instructions

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
