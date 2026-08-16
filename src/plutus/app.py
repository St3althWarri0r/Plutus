"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from plutus import __version__
from plutus.config import effective_trading_mode
from plutus.logging_setup import configure_logging, get_logger

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    configure_logging()
    log = get_logger("plutus.app")

    app = FastAPI(title="Plutus", version=__version__)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    log.info("app_created", trading_mode=effective_trading_mode())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "trading_mode": effective_trading_mode(),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"trading_mode": effective_trading_mode(), "version": __version__},
        )

    return app
