"""FastAPI application factory.

Phase 1 wiring: an AlpacaAdapter (paper keys unless the platform resolves to
live) behind a RiskManager, plus the dashboard — account card, positions,
manual paper order form, and an order list that polls broker status until
fill confirmation. Tests inject a fake adapter and an in-memory DB.
"""

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus import __version__
from plutus.brokers.alpaca import MissingCredentialsError, alpaca_adapter_from_settings
from plutus.brokers.base import BrokerAdapter, OrderIntent, OrderStatus
from plutus.config import effective_trading_mode, get_settings
from plutus.db import make_engine, make_session_factory
from plutus.logging_setup import configure_logging, get_logger
from plutus.models import Order
from plutus.risk import RiskManager

TEMPLATES_DIR = Path(__file__).parent / "templates"

_UNSET = object()


def _default_adapter() -> BrokerAdapter | None:
    try:
        return alpaca_adapter_from_settings(get_settings(), effective_trading_mode())
    except MissingCredentialsError:
        return None


def create_app(
    adapter: BrokerAdapter | None | object = _UNSET,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    configure_logging()
    log = get_logger("plutus.app")

    if adapter is _UNSET:
        broker: BrokerAdapter | None = _default_adapter()
    else:
        broker = adapter  # type: ignore[assignment]
    factory = session_factory or make_session_factory(make_engine())
    risk = None if broker is None else RiskManager(adapter=broker, session_factory=factory)

    app = FastAPI(title="Plutus", version=__version__)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    log.info(
        "app_created",
        trading_mode=effective_trading_mode(),
        broker_configured=broker is not None,
    )

    def refresh_open_orders(session: Session) -> list[Order]:
        """Poll broker status for non-terminal orders; persist transitions."""
        orders = list(
            session.scalars(select(Order).order_by(Order.id.desc()).limit(20)).all()
        )
        if broker is None:
            return orders
        for row in orders:
            if row.broker_order_id and not OrderStatus(row.status).is_terminal:
                latest = broker.get_order_status(row.broker_order_id)
                if str(latest) != row.status:
                    row.status = str(latest)
        session.commit()
        return orders

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "trading_mode": effective_trading_mode(),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        account = broker.get_account() if broker is not None else None
        positions = broker.get_positions() if broker is not None else []
        with factory() as session:
            orders = refresh_open_orders(session)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "trading_mode": effective_trading_mode(),
                "version": __version__,
                "broker_configured": broker is not None,
                "account": account,
                "positions": positions,
                "orders": orders,
            },
        )

    @app.get("/partials/orders", response_class=HTMLResponse)
    def orders_partial(request: Request) -> HTMLResponse:
        with factory() as session:
            orders = refresh_open_orders(session)
        return templates.TemplateResponse(request, "_orders.html", {"orders": orders})

    @app.post("/orders", response_class=HTMLResponse)
    async def place_order(request: Request) -> HTMLResponse:
        if risk is None:
            return HTMLResponse(
                "Broker not configured — set ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET in .env",
                status_code=503,
            )
        # stdlib parse of the urlencoded body — starlette's form() (and FastAPI's
        # Form()) require python-multipart, a dependency §2 doesn't approve
        body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        limit_price = form.get("limit_price") or ""
        try:
            intent = OrderIntent.model_validate(
                {
                    "symbol": form.get("symbol"),
                    "side": form.get("side"),
                    "qty": form.get("qty"),
                    "order_type": form.get("order_type"),
                    **({"limit_price": limit_price} if limit_price.strip() else {}),
                }
            )
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(loc) for loc in e['loc']) or 'order'}: {e['msg']}"
                for e in exc.errors()
            )
            return HTMLResponse(f"Invalid order — {errors}", status_code=422)

        row = risk.submit(intent)
        # one-line ack only — the orders table below polls itself and would
        # end up duplicated if this response re-rendered the whole partial
        detail = f" ({row.broker_order_id})" if row.broker_order_id else ""
        if row.reject_reason:
            detail += f" — {row.reject_reason}"
        return HTMLResponse(
            f'<p class="order-ack status-{row.status}">'
            f"{row.symbol} {row.side} {row.qty} — {row.status}{detail}</p>"
        )

    return app
