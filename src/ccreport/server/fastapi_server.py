"""Granian entry point for the ccreport server.

In production, run Granian directly:
    granian --interface asgi --host 0.0.0.0 --port 8787 \
        ccreport.server.factory:create_app --factory

For development with auto-reload:
    just serve

Host, port, database path and the web UI's allowed networks come from the
environment; see config.py.
"""

from __future__ import annotations

import argparse

from ccreport.server.config import load_config


def main(argv: list[str] | None = None) -> None:
    """Serve the app with Granian."""
    from granian import Granian
    from granian.constants import Interfaces

    args = parse_args(argv)
    Granian(
        "ccreport.server.factory:create_app",
        address=args.host,
        port=args.port,
        interface=Interfaces.ASGI,
        factory=True,
        reload=args.reload,
        workers=1 if args.reload else args.workers,
    ).serve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line over the environment defaults.

    A flag wins over its environment variable, which is what makes `just serve`
    able to move the port without exporting anything.
    """
    config = load_config()
    parser = argparse.ArgumentParser(description="Run the ccreport server")
    parser.add_argument("--host", default=config.host, help="Address to bind")
    parser.add_argument("--port", type=int, default=config.port, help="Port to bind")
    parser.add_argument("--workers", type=int, default=2, help="Worker processes")
    parser.add_argument("--reload", action="store_true", help="Reload on source changes, one worker")
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()
