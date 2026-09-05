# CHANGELOG

## [Unreleased]

### Development

- **A merged `release/vX.Y.Z` pull request is a release.** `.github/workflows/publish.yml` runs the checks, tags `vX.Y.Z`, builds the wheel and sdist, publishes the GitHub Release with this file's section as its body, and pushes `ghcr.io/oroddlokken/ccreport:X.Y.Z` and `:latest` for linux/amd64. `just release-prep <version>` opens that pull request; `just next` names the candidate versions. The version comes from the tag through hatch-vcs, and `just check-sdist` fails a tarball that grew past 1 MiB or picked up an entry outside the include-list.
- **Every pull request runs lint, tests and the sdist check on GitHub** through `.github/workflows/ci.yml`, with pyright pinned to the version uv.lock resolves and dependabot grouping the action bumps weekly.
- **The Dockerfile's CMD runs one worker with no reloader**; `docker-compose.yml` overrides it with `--reload` for `just docker-up`, and `tests/test_docker_image.py` fails if the pair drifts.
- **`tools/make_synthetic_logs.py` writes a Claude Code session-log tree** the reader parses end to end, so the suite passes on a machine that has never run Claude Code.
