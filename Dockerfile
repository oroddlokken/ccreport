# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:e9a8312ed6a98f515208dd792c61178a0b7c8fbfb807af01534f0e6fe10b24f5 AS builder

RUN --mount=type=cache,sharing=locked,target=/var/cache/apk apk update && apk add git

WORKDIR /app

# --no-install-project keeps this layer cacheable: only pyproject.toml and
# uv.lock invalidate it, not a source change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache uv sync --no-dev --no-install-project --frozen \
    && find /app/.venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:e9a8312ed6a98f515208dd792c61178a0b7c8fbfb807af01534f0e6fe10b24f5

RUN apk add --no-cache sqlite bash

# The account exists before the copies so each COPY stamps its own ownership.
# A `chown -R app:app /app` after them instead sits below the source COPY, and
# overlayfs stores a metadata-only change as a copy-up of the whole file, so one
# edited source line rewrites, pushes and pulls another ~110 MB of venv.
RUN adduser -D -u 1000 app && mkdir -p /app && chown app:app /app

ENV PYTHONPATH="/app/src"
ENV PATH="/app/.venv/bin:$PATH"
# docker-compose.yml mounts ./src read-only, so a .pyc write fails on every
# import.
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app ./src /app/src

WORKDIR /app

# docker seeds an empty named volume from the image path, ownership included,
# so a root-owned /data leaves the server unable to write its database.
RUN mkdir -p /data && chown app:app /data
USER app

EXPOSE 8787

# The repo's own entry point rather than granian directly: it reads the
# CCREPORT_SERVER_* environment. --workers is spelled out because argparse
# defaults it to 2 and one SQLite file wants one writer; docker-compose.yml
# overrides this command with --reload for development.
CMD ["python", "-m", "ccreport.server.fastapi_server", "--host", "0.0.0.0", "--workers", "1"]
