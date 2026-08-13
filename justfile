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

# measure what a status line render costs in wall time and energy
bench:
    uv run python tools/benchmark_statusline_energy.py

# serve the merged-records server with reload, one worker
serve:
    uv run python -m ccreport.server.fastapi_server --reload

# run tests
test:
    uv run pytest --timeout 30 -n 8 tests

# run only tests affected by code changes since last run
test-changed:
    uv run pytest --testmon --timeout 60 -n 8 tests

# run all tests with coverage
test-all:
    uv run pytest --timeout 60 -n 8 tests --cov-report=html --cov=src/ccreport
