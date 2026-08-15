# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder

RUN --mount=type=cache,sharing=locked,target=/var/cache/apk apk update && apk add git

WORKDIR /app

# --no-install-project keeps this layer cacheable: only pyproject.toml and
# uv.lock invalidate it, not a source change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache uv sync --no-dev --no-install-project --frozen \
    && find /app/.venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

FROM ghcr.io/astral-sh/uv:python3.13-alpine

RUN apk add --no-cache sqlite bash

ENV PYTHONPATH="/app/src"
ENV PATH="/app/.venv/bin:$PATH"
# docker-compose.yml mounts ./src read-only, so a .pyc write fails on every
# import.
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /app/.venv /app/.venv
COPY ./src /app/src

WORKDIR /app

# docker seeds an empty named volume from the image path, ownership included,
# so a root-owned /data leaves the server unable to write its database.
RUN find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
    adduser -D -u 1000 app && chown -R app:app /app; \
    mkdir -p /data && chown app:app /data
USER app

EXPOSE 8787

# The repo's own entry point rather than granian directly: it reads the
# CCREPORT_SERVER_* environment. --reload drops it to one worker, which is what
# a shared SQLite file wants.
CMD ["python", "-m", "ccreport.server.fastapi_server", "--host", "0.0.0.0", "--reload"]
