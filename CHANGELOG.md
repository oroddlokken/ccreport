# CHANGELOG

## [Unreleased]

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
