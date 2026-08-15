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
lint-pyright:
    PYRIGHT_PYTHON_FORCE_VERSION=latest uv run pyright src tests

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
# start the server in docker on http://127.0.0.1:8787
docker-up:
    docker compose -f ./docker-compose.yml -p ccreport up -d --build --wait

# stop the server and remove its container; the database volume survives
docker-down:
    docker compose -f ./docker-compose.yml -p ccreport down

# Every machine, token and pushed record in the local server database goes with
# it. Each machine then needs a token minted again, and `ccreport push --full`
# to resend what its watermark now considers already sent.
# stop the server and delete its database volume
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
