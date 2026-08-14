"""App factory for the ccreport server."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from starlette.staticfiles import StaticFiles

from ccreport import exchange
from ccreport.server import db, ingest, pages, report_api
from ccreport.server.config import ServerConfig, load_config
from ccreport.server.middleware import NetworkGated, restrict_remote_addr_dep


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the FastAPI app over the merged database.

    *config* is for tests and for a caller that has already read the
    environment; production passes nothing and gets load_config().

    Granian imports this by name and calls it per worker, so everything here
    has to be safe to run several times in one machine's life — opening the
    database is, since connect() creates the schema only when the stamp says
    this build has not applied it.
    """
    config = config or load_config()
    app = FastAPI(title="ccreport")
    app.state.config = config
    app.state.db = database = db.Database(config.db_path)
    # The server converts every client's records to NOK, so exchange.py's cache
    # has to land in this database rather than in the operator's own client
    # cache. The walk-back and the negative cache stay exchange.py's.
    exchange.use_rate_store(db.RateStore(database))

    # Ingest first and without the gate: a machine pushes from wherever it is,
    # and its token is the whole of what admits it. Everything a person opens
    # is behind the allowlist instead, including the static files, so a
    # disallowed address gets 403 from the UI and nothing else.
    app.include_router(ingest.router)
    gate = [Depends(restrict_remote_addr_dep(config.networks))]
    app.include_router(report_api.router, dependencies=gate)
    # Before the pages: they end in /{dimension}/{key}, which would otherwise
    # match /static/app.css and answer 404 for every asset on the site.
    app.mount(
        "/static",
        NetworkGated(
            StaticFiles(directory=str(Path(__file__).with_name("static"))),
            config.networks,
        ),
        name="static",
    )
    app.include_router(pages.router, dependencies=gate)
    return app
