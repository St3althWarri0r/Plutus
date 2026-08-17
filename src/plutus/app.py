"""FastAPI application factory.

Phase 1 wiring: an AlpacaAdapter (paper keys unless the platform resolves to
live) behind a RiskManager, plus the dashboard — account card, positions,
manual paper order form, and an order list that polls broker status until
fill confirmation. Tests inject a fake adapter and an in-memory DB.
"""

from collections.abc import Callable
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
from plutus.models import Order, StrategyState
from plutus.risk import RiskManager

TEMPLATES_DIR = Path(__file__).parent / "templates"

_UNSET = object()


def _default_adapter() -> BrokerAdapter | None:
    try:
        return alpaca_adapter_from_settings(get_settings(), effective_trading_mode())
    except MissingCredentialsError:
        return None


def _default_price_lookup(
    factory: sessionmaker[Session],
) -> Callable[[str], float | None]:
    """Stocks: last cached daily close. Crypto pairs: latest quote midpoint."""
    from datetime import UTC, datetime, timedelta

    from plutus.data.alpaca_data import alpaca_data_provider_from_settings
    from plutus.data.cache import CachedDataProvider
    from plutus.market_calendar import is_crypto

    try:
        cache: CachedDataProvider | None = CachedDataProvider(
            alpaca_data_provider_from_settings(get_settings()), factory
        )
    except MissingCredentialsError:
        cache = None

    def lookup(symbol: str) -> float | None:
        try:
            if is_crypto(symbol):
                from alpaca.data.historical import CryptoHistoricalDataClient
                from alpaca.data.requests import CryptoLatestQuoteRequest

                quotes = CryptoHistoricalDataClient().get_crypto_latest_quote(
                    CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                )
                quote = quotes[symbol]
                return (float(quote.ask_price) + float(quote.bid_price)) / 2
            if cache is None:
                return None
            now = datetime.now(UTC)
            bars = cache.get_bars(symbol, "1d", now - timedelta(days=10), now)
            return float(bars["close"].iloc[-1]) if len(bars) else None
        except Exception:
            return None  # unpriceable → the risk gate fails closed

    return lookup


def create_app(
    adapter: BrokerAdapter | None | object = _UNSET,
    session_factory: sessionmaker[Session] | None = None,
    risk_manager: RiskManager | None = None,
) -> FastAPI:
    configure_logging()
    log = get_logger("plutus.app")

    if adapter is _UNSET:
        broker: BrokerAdapter | None = _default_adapter()
    else:
        broker = adapter  # type: ignore[assignment]
    factory = session_factory or make_session_factory(make_engine())
    if risk_manager is not None:
        risk: RiskManager | None = risk_manager
    elif broker is not None:
        risk = RiskManager(
            adapter=broker,
            session_factory=factory,
            price_lookup=_default_price_lookup(factory),
        )
    else:
        risk = None

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
            strategies = list(
                session.scalars(select(StrategyState).order_by(StrategyState.strategy)).all()
            )
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
                "strategies": strategies,
            },
        )

    @app.post("/strategies/{name}/enable", response_class=HTMLResponse)
    async def enable_strategy(name: str, request: Request) -> HTMLResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        # §8 halts end only by deliberate manual re-enable
        if form.get("confirm") != "ENABLE":
            return HTMLResponse(
                "Type ENABLE in the confirmation box to re-enable this strategy",
                status_code=400,
            )
        with factory() as session:
            state = session.scalars(
                select(StrategyState).where(StrategyState.strategy == name)
            ).one_or_none()
            if state is None:
                return HTMLResponse(f"unknown strategy {name!r}", status_code=404)
            state.enabled = True
            state.halt_reason = None
            session.commit()
        log.info("strategy_reenabled", strategy=name)
        return HTMLResponse(f'<p class="notice">{name} re-enabled.</p>')

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
                    "time_in_force": form.get("time_in_force") or "day",
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

    @app.get("/net-worth", response_class=HTMLResponse)
    def net_worth(request: Request, range: str = "1y") -> HTMLResponse:
        from plutus.aggregation import net_worth_series

        days = {"1m": 30, "1y": 365, "all": 36500}.get(range, 365)
        series = net_worth_series(factory, days=days)
        points = ""
        if len(series.total) >= 2:
            xs = [d.toordinal() for d, _ in series.total]
            ys = [v for _, v in series.total]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            xr = max(x1 - x0, 1)
            yr = max(y1 - y0, 1e-9)
            points = " ".join(
                f"{(x - x0) / xr * 780 + 10:.1f},{230 - (y - y0) / yr * 210:.1f}"
                for x, y in zip(xs, ys, strict=True)
            )
        total_now = series.total[-1][1] if series.total else 0.0
        cards = [
            ("M1", "m1"),
            ("Vanguard", "vanguard"),
            ("Schwab", "schwab"),
            ("Alpaca (paper)", "alpaca_paper"),
        ]
        return templates.TemplateResponse(
            request,
            "net_worth.html",
            {
                "trading_mode": effective_trading_mode(),
                "total_now": total_now,
                "points": points,
                "range": range,
                "cards": [
                    (label, key, series.latest_by_account.get(key)) for label, key in cards
                ],
            },
        )

    @app.post("/import-csv", response_class=HTMLResponse)
    async def import_csv(request: Request) -> HTMLResponse:
        from plutus.aggregation import CsvParseError, parse_holdings_csv, snapshot_account

        body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        institution = form.get("institution", "")
        try:
            parsed = parse_holdings_csv(form.get("csv_text", ""), institution=institution)
        except CsvParseError as exc:
            return HTMLResponse(f"CSV import failed — {exc}", status_code=422)
        snapshot_account(
            factory,
            account=institution,
            equity=parsed.equity,
            cash=parsed.cash,
            positions=parsed.holdings,
        )
        return HTMLResponse(
            f'<p class="notice">{institution} snapshot saved: '
            f"${parsed.equity:,.2f} across {len(parsed.holdings)} holdings.</p>"
        )

    @app.post("/refresh-snapshots", response_class=HTMLResponse)
    def refresh_snapshots() -> HTMLResponse:
        from plutus.aggregation import snapshot_alpaca

        written = []
        if broker is not None:
            snapshot_alpaca(factory, adapter=broker, mode=effective_trading_mode())
            written.append("alpaca")
        try:
            from plutus.plaid_sync import plaid_sync_from_settings

            plaid = plaid_sync_from_settings(get_settings(), factory)
            if plaid is not None:
                for institution in ("m1", "vanguard"):
                    if plaid.sync_holdings(institution):
                        written.append(institution)
        except Exception as exc:
            log.warning("plaid_refresh_failed", error=str(exc))
        return HTMLResponse(
            f'<p class="notice">Snapshots refreshed: {", ".join(written) or "none"}.</p>'
        )

    @app.get("/mode-b", response_class=HTMLResponse)
    def mode_b_stats(request: Request) -> HTMLResponse:
        """§9B.7 promotion-bar stats, live. The lock stays until the user
        flips it by hand — this page only reports."""
        from sqlalchemy import func as sqlfunc

        from plutus.ai.mode_b_accounting import compute_stats
        from plutus.models import AiAudit, ModeBTrade

        stats = compute_stats(factory)
        with factory() as session:
            recent = session.scalars(
                select(ModeBTrade).order_by(ModeBTrade.id.desc()).limit(20)
            ).all()
            cost_rows = session.execute(
                select(
                    sqlfunc.date(AiAudit.created_at),
                    sqlfunc.coalesce(sqlfunc.sum(AiAudit.cost_usd), 0),
                )
                .group_by(sqlfunc.date(AiAudit.created_at))
                .order_by(sqlfunc.date(AiAudit.created_at).desc())
                .limit(10)
            ).all()
        return templates.TemplateResponse(
            request,
            "mode_b.html",
            {
                "trading_mode": effective_trading_mode(),
                "stats": stats,
                "trades": recent,
                "costs": [(str(d), float(c)) for d, c in cost_rows],
            },
        )

    @app.post("/kill", response_class=HTMLResponse)
    async def kill(request: Request) -> HTMLResponse:
        if risk is None:
            return HTMLResponse("Broker not configured — nothing to kill", status_code=503)
        body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        # double-confirm: the literal text KILL must be typed
        if form.get("confirm") != "KILL":
            return HTMLResponse(
                'Type KILL in the confirmation box to engage the kill switch',
                status_code=400,
            )
        report = risk.kill(source="dashboard")
        return HTMLResponse(
            f'<p class="notice">KILL engaged — canceled {report.canceled} orders, '
            f"flattened {len(report.flattened)} positions, "
            f"disabled {len(report.disabled)} strategies. "
            "Remove the KILL file manually to resume.</p>"
        )

    return app
