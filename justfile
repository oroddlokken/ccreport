# list all targets
default:
    @just --list

# list all variables
var:
    @just --evaluate

# apply ruff's autofixes — no `ruff format`, the wrapping here is hand-chosen
fmt:
    uv run ruff check --fix --unsafe-fixes src tests tools

# all of formatting is `just fmt` — `ruff format` stays out on purpose
fmt-all:
    just fmt

# lint the code
lint:
    uv run ruff check src tests tools

# lint using pyright
# latest unless the caller pins one, which ci.yml does to the version uv.lock
# resolves: a pyright release upstream must not turn main red on its own
lint-pyright:
    PYRIGHT_PYTHON_FORCE_VERSION=${PYRIGHT_PYTHON_FORCE_VERSION:-latest} uv run pyright src tests

# run all linters
lint-all:
    just lint
    just lint-pyright

# find dead code with vulture
vulture:
    uv run vulture src tests vulture_whitelist.py

# render every banner and colored token the status line can emit
banners:
    uv run python tools/banner_demo.py

# measure what a status line render costs in wall time, energy and processes
bench:
    uv run python tools/benchmark_statusline_energy.py

# serve the merged-records server with reload, one worker
serve:
    uv run python -m ccreport.server.fastapi_server --reload

# Rebuilds first: the image carries the venv, and a uv.lock change is invisible
# to `up` on its own. The source is bind-mounted, so an edit needs no rebuild.
#
# The database lives in ./data, bind-mounted at /data. It used to live in the
# named volume ccreport_ccreport-data. A machine that still has that volume and
# an empty ./data would start on an empty database, so the recipe refuses and
# says how to copy the old data across first.
# start the server in docker on http://127.0.0.1:8787
docker-up:
    #!/usr/bin/env bash
    set -euo pipefail
    if docker volume inspect ccreport_ccreport-data >/dev/null 2>&1 && [[ -z "$(ls -A data 2>/dev/null)" ]]; then
        echo "The old named volume ccreport_ccreport-data still holds this stack's data, and ./data is empty." >&2
        echo "Copy it across with the stack stopped, check ./data, then remove the volume:" >&2
        echo '  mkdir -p data && docker run --rm -v ccreport_ccreport-data:/from -v "$PWD/data":/to alpine cp -a /from/. /to/' >&2
        echo '  docker volume rm ccreport_ccreport-data' >&2
        exit 1
    fi
    mkdir -p data
    docker compose -f ./docker-compose.yml -p ccreport up -d --build --wait

# stop the server and remove its container; ./data survives
docker-down:
    docker compose -f ./docker-compose.yml -p ccreport down

# runs docker-down and docker-up
docker-restart:
    just docker-down
    just docker-up

# The database stays in ./data. Delete that directory by hand to start over:
# every machine, token and pushed record goes with it, each machine then needs
# a token minted again, and `ccreport push --full` to resend what its watermark
# now considers already sent.
# stop the server and remove its container and any anonymous volumes
docker-remove:
    docker compose -f ./docker-compose.yml -p ccreport down -v --remove-orphans

# run tests
test:
    uv run pytest --timeout 30 -n 8 tests

# run only tests affected by code changes since last run
test-changed:
    uv run pytest --testmon --timeout 60 -n 8 tests

# run all tests with coverage
test-all:
    uv run pytest --timeout 60 -n 8 tests --cov-report=html --cov=src/ccreport

# build an sdist and check what it carries
check-sdist:
    #!/usr/bin/env bash
    set -euo pipefail
    # Built into a temp dir rather than dist/, which holds artifacts from
    # earlier builds that would make the tarball ambiguous.
    out=$(mktemp -d)
    trap 'rm -rf "$out"' EXIT
    uv build --sdist --out-dir "$out" -q
    ./scripts/check-sdist "$out"/*.tar.gz

# show next possible versions (patch or minor bump)
next:
    #!/usr/bin/env bash
    set -euo pipefail
    LATEST=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sed 's/^v//; s/-rc\..*//' | sort -t. -k1,1n -k2,2n -k3,3n -u | tail -1)
    LATEST=${LATEST:-0.0.0}
    IFS='.' read -r MAJOR MINOR PATCH <<< "$LATEST"
    RC=$(git tag -l "v${LATEST}-rc.*" | sort -V | tail -1 | sed -n 's/.*-rc\.//p')
    RELEASED=$(git tag -l "v${LATEST}" | head -1)
    if [ -n "$RC" ] && [ -z "$RELEASED" ]; then
        echo "Current: ${LATEST} (rc.${RC}, unreleased)"
    else
        echo "Current: ${LATEST}"
    fi
    echo "  patch: ${MAJOR}.${MINOR}.$((PATCH + 1))"
    echo "  minor: ${MAJOR}.$((MINOR + 1)).0"

# The user's call, never an agent's: docs/releasing.md says why.
# prepare a release: create RC tag, push branch, open PR
release-prep *args:
    ./scripts/release-prep {{args}}
    git fetch --all --tags --prune
    git pull origin main
