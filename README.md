# Usage

## Installation with uv for development

Make sure uv are installed, and then run the following commands:
```bash
uv sync --group dev
```

## Managing dependencies
To add a new dependency, use:
```bash
uv add <package-name>
```

To add a development dependency, use:
```bash
uv add --group dev <package-name>
```

To update dependencies:
```bash
uv sync --upgrade
```

All dependencies are managed in `pyproject.toml`.

## Formatting / linting
Run `just fmt` to format the code.
Run `just lint` to lint the code.
Run `just lint-all` to run all linters including pyright and sql.

## Testing
Run `just test` to run tests.
Run `just test-changed` to run only tests affected by code changes.
Run `just test-all` to run all tests with coverage.

