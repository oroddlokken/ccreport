# Releasing

Releasing is the user's call. Read this before running anything in it.

## `just release-prep` is irreversible past the PR merge

`just release-prep <version>` runs `just lint-all` and `just test-all`, stamps `CHANGELOG.md`,
resets an existing `release/v<version>` branch to `master`, force-pushes it with lease, pushes a
`vX.Y.Z-rc.N` tag, and opens a pull request.

The branch work happens in this clone rather than in a worktree. The tree has to be clean, and
the script checks out `release/v<version>` and returns you to the branch you started on.

An existing branch counts whether or not this clone has it. A remote-only branch is fetched,
because branching off `master` instead forks a sibling commit no push can fast-forward.

The tag goes out only after the branch push lands, and a rejected push deletes the local tag and
stops. `tests/test_release_prep.py` drives both paths against a bare local remote.

Merging that pull request fires `.github/workflows/publish.yml`. It tags `vX.Y.Z`, builds the
wheel and sdist, creates the GitHub Release with that version's `CHANGELOG.md` section as its
body, pushes the server image to GHCR, and pushes a formula commit to the
`oroddlokken/homebrew-tap` repo. The merge is the irreversible step, and it is the user's to make.

Run `just release-prep` only when the user names a version and asks for a release, and leave the
merge to them. `just next` reports the candidate patch and minor versions and changes nothing.

The branch name is load-bearing. `publish.yml` fires only for a merged pull request whose head
branch starts with `release/v`; landing the same commits any other way publishes nothing.

`publish.yml` and `ci.yml` both run `just lint-all`, the test suite and `just check-sdist`, and
each sets up its own toolchain and pins its own `PYRIGHT_PYTHON_FORCE_VERSION`. A step added to
one has to be added to the other, or the failure surfaces at release time, when the merge has
already landed on `master`.

## Homebrew formula

`publish.yml` rewrites the `url` and `sha256` fields in `Formula/ccreport.rb` in the tap repo and
pushes the commit, so a version bump needs no hand edit and a hand edit is overwritten by the next
release.

The digest is taken from the wheel downloaded to a file rather than from a pipe. A release asset
takes a moment to become downloadable, and until it is, GitHub serves a 404 body that digests to a
real sha256; a formula carrying that digest fails every `brew install`. The job retries five times
on `curl -f`'s exit code and then exits 1, which leaves the tap untouched. Rerunning the job fixes
that, where a pushed wrong digest needs a hand edit to the formula.

Hand edits cover structural changes only: a new runtime dependency, a changed entry point. Wait for
user confirmation before making one, and say so when a CLI change requires it.

The formula installs the release wheel into its own virtualenv and puts `ccreport` and `ccu` on
`PATH`. The status line and the hooks are not in it: they run out of a checkout.

The tap is a separate repo, checked out at `~/git/homebrew-tap`. The `update-homebrew` job reads
`HOMEBREW_TAP_TOKEN` from this repo's secrets — a PAT with `contents:write` on the tap. The publish
job probes that write access ahead of the tag push, so a token that can only read the tap fails
the release before anything is published.

## `just check-sdist` guards the tarball

`scripts/check-sdist` fails a source distribution over 1 MiB or holding a top-level entry outside
its allowlist. The include-list in `pyproject.toml` (`[tool.hatch.build.targets.sdist]`) is what
keeps the tarball to `src/`, `docs/`, `bin/` and two files; nothing else notices that section
being loosened, and `.dogcats/` and `assets/` are what it would sweep in.

It runs on every pull request in `ci.yml`, and in `publish.yml` ahead of the tag push, because that
push is the first irreversible step. A directory that belongs in the sdist goes into the
include-list and into `ALLOWED` in the script. A cache belongs in the root `.gitignore`, which is
the copy hatchling reads.

## The GHCR image

The `publish-image` job in `publish.yml` runs after `publish` and pushes
`ghcr.io/oroddlokken/ccreport:X.Y.Z` and `:latest`, `linux/amd64` alone, built from the
`Dockerfile` at the merge commit. The version is the same `release/v` prefix strip the publish job
uses, and `docker/metadata-action` writes it into the OCI labels with the revision.

The image's CMD is the one-worker, no-reload form. `docker-compose.yml` overrides it with
`--reload` for `just docker-up`, and `tests/test_docker_image.py` fails if either side drifts.

A checkout is still the other way in: `git pull` in it tracks `master`, and the wheel and sdist
ride along on the GitHub Release as assets. Only a Homebrew install is told it is out of date —
`statusline._render_update` names `brew upgrade` and there is no other upgrade route it can
name.
