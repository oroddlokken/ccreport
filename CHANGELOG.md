# CHANGELOG

## [Unreleased]

### Fixed

- **A push no longer sends records under a placeholder account.** A cache whose `account_events` table does not reach back to a record stamped it `"unknown"`, and the server grouped those rows as an account of their own — one cold-cache run put 80,078 records and $4,256 of real spend under a name no alias reaches. `push._attribution` is now the one place the record, sample and Extra-reading builders resolve an account, and a row it cannot attribute stays on the machine. A file whose every record is uncovered is left out of the batch rather than offered empty, because the server replaces what it holds per (machine, path); a file the log covers in part sends that part. `ccreport server push` reports the count it left and names `ccreport adopt`.

## 0.1.3 (2026-09-06)

### Development

- **No test reaches the live Norges Bank API, and none reads another test's rate store.** `server.factory.create_app` points `exchange._store` at the server database it opened and nothing put it back, so a test that built an app left every later test on the same xdist worker saving rates to the isolated cache and loading them from a closed database in a previous test's `tmp_path`. `ccreport.main` calls `exchange.start_prefetch()` before the corpus load, and six CLI tests put a Norges Bank request on a daemon thread that outlived them. `tests/conftest.py` now restores the store per test, returns `None` from `start_prefetch` unless a test asks for `live_prefetch`, and takes `urlopen` away for the whole session rather than per test — a daemon thread runs after a function-scoped patch is undone. The block raises a `RuntimeError` subclass, since `exchange._fetch_api` catches `OSError` and reports it as an API with nothing to say.

## 0.1.2 (2026-09-05)

### Added

- **A wheel install reaches the status line and the quota guard.** `ccreport-statusline` and `ccreport-quota-guard` join `ccreport` and `ccu` on `PATH`, and the four wrappers Claude Code's settings.json points at ship inside the package at `ccreport/scripts/`, off `PATH` because a hook takes a path rather than a command. `ccreport scripts` prints where they landed. Both wrappers resolve the checkout layout and the installed one, and in an install they run the interpreter that owns the package rather than the first `python3` on `PATH`, which can be older than the package needs.

### Changed

- **The update line reports a Homebrew install and names `brew upgrade`.** It compares `importlib.metadata.version` against the newest `vX.Y.Z` release tag, which is what the tap's formula is rewritten for, and renders `run 'brew upgrade oroddlokken/tap/ccreport'`. A keg is matched on a `Cellar/ccreport` pair in the resolved package path, so `/opt/homebrew`, `/usr/local` and linuxbrew all answer without running `brew`. A checkout, a `uv tool install` and a bare wheel each update by a route this cannot name, so none of them checks at all — no line, no request.

### Removed

- **`ccreport update` and `ccreport update --pull`.** Nothing updates itself now: the fast-forward and the origin/master compare behind it are gone, and a checkout pulls with git like any other. The status line's `CLAUDE_STATUSLINE_UPDATE` toggle and its twice-a-day detached check stay, against the release tag instead of a commit count.

## 0.1.1 (2026-09-05)

### Fixed

- **A pool connection is closed by the thread that opened it.** A worker thread that ends drops its `threading.local` storage, and the connection went with it — finalized by the GC rather than closed. A holder now sits in that storage and closes on the thread's own teardown, the only thread sqlite3 permits a close from; the suite went from 1758 warnings to 8.

### Development

- **`brew install oroddlokken/tap/ccreport` installs `ccreport` and `ccu`.** The `update-homebrew` job in `publish.yml` rewrites the wheel URL and sha256 in `Formula/ccreport.rb` on every release, and the publish job probes the tap token's write access before the tag goes out. The job downloads the wheel to a file and retries on `curl -f`'s exit code: GitHub's 404 body for an asset that is not yet available digests to a real sha256, and a formula carrying that digest fails every `brew install` until the tap is edited by hand.

## 0.1.0 (2026-09-05)

### Development

- **A merged `release/vX.Y.Z` pull request is a release.** `.github/workflows/publish.yml` runs the checks, tags `vX.Y.Z`, builds the wheel and sdist, publishes the GitHub Release with this file's section as its body, and pushes `ghcr.io/oroddlokken/ccreport:X.Y.Z` and `:latest` for linux/amd64. `just release-prep <version>` opens that pull request; `just next` names the candidate versions. The version comes from the tag through hatch-vcs, and `just check-sdist` fails a tarball that grew past 1 MiB or picked up an entry outside the include-list.
- **Every pull request runs lint, tests and the sdist check on GitHub** through `.github/workflows/ci.yml`, with pyright pinned to the version uv.lock resolves and dependabot grouping the action bumps weekly.
- **The Dockerfile's CMD runs one worker with no reloader**; `docker-compose.yml` overrides it with `--reload` for `just docker-up`, and `tests/test_docker_image.py` fails if the pair drifts.
- **`tools/make_synthetic_logs.py` writes a Claude Code session-log tree** the reader parses end to end, so the suite passes on a machine that has never run Claude Code.
