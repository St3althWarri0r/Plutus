# Plutus — Architecture

Single-user, self-hosted portfolio dashboard + automated trading engine. Full
spec lives in [CLAUDE.md](CLAUDE.md); this file tracks what is actually built.

## Status: Phase 5 (paper autotrading) built — 30-session acceptance clock running

## System shape (target)

Two halves behind one FastAPI app:

1. **Read layer** — aggregates positions/balances across M1, Vanguard (both
   Plaid, read-only forever), Schwab, and Alpaca into a net-worth dashboard
   backed by daily `snapshots`.
2. **Execution layer** — systematic strategies + an AI layer trading through
   `BrokerAdapter` implementations (Alpaca first, Schwab second). Every order
   passes through the RiskManager; it is the only component allowed to call
   `submit_order`.

## What exists today

```
src/plutus/
  config.py         Settings (pydantic-settings, .env) + effective_trading_mode()
  logging_setup.py  structlog JSON logging
  db.py             SQLAlchemy 2.0 engine/session; Base for all models
  models.py         Snapshot; Order (one row per intent, unique idempotency_key)
  risk.py           RiskManager — the ONLY caller of BrokerAdapter.submit_order
  brokers/
    base.py         OrderIntent/OrderReceipt/Position/AccountState/Fill +
                    BrokerAdapter Protocol (§3)
    alpaca.py       AlpacaAdapter over alpaca-py TradingClient; paper/live key
                    selection; timeout-recovery via client_order_id lookup
  app.py            FastAPI factory: /healthz, / (dashboard), POST /orders,
                    GET /partials/orders (HTMX 3s status poll)
  templates/        index.html (mode banner, account cards, positions, order
                    form), _orders.html (order table partial)
  data/
    provider.py     DataProvider protocol (UTC-indexed OHLCV frames) +
                    is_stale() 2×-interval helper (wired in Phase 4/5)
    alpaca_data.py  AlpacaDataProvider: IEX feed, adjustment=all
    cache.py        CachedDataProvider: bars + bar_coverage ranges; daily ts
                    normalized to UTC midnight at this boundary
  backtest/
    costs.py        CostModel: slippage bps + half-spread bps + per-share comm
    engine.py       weights→returns engine; REQUIRED fill param:
                    same_close | next_open | next_close; costs on fill day
    metrics.py      CAGR/Sharpe/Sortino/maxDD/exposure/turnover/trades/yearly
    persist.py      save_run/load_run → backtest_runs
    walkforward.py  rolling IS fit over a param grid, stitched OOS returns,
                    OOS-only metrics; one full-series backtest per param
  strategies/
    indicators.py   sma, rsi_wilder (default; SMA-seeded recursion),
                    rsi_cutler (kept to quantify definition divergence)
    tqqq_rotation.py §6 tree, ported exactly: strict >79/<31, TQQQ-based
                    regime, 50/50 UVXY+BSV hedge, no SOXL; BSV on RSI tie;
                    weights_history() only — no broker access
  risk.py           full §8 gate chain (see below) + fills/reconcile/daily-
                    loss/kill + manual baseline; sole caller of submit_order
  market_calendar.py XNYS RTH + session-day checks; is_crypto (pair slash)
  scheduler.py      APScheduler jobs (ET): 09:15/16:15 reconcile, 09:30
                    day-start mark, */5m daily-loss check (guarded to
                    09:30–16:00), 15:55 intraday flatten; returned UNSTARTED
  alerts.py         TelegramAlerter — log-degrade without token, never raises
  pnl.py            watermarked fill ingestion → fills table; equity_now =
                    day-start + cash flow + MTM; 1.5% crossing warn monitor
  execution.py      weights → floored whole-share orders, $50 dead-band,
                    sells before buys; provisional-close append (the cache
                    clamps daily bars to completed days — without the append
                    the 15:50 signal would be yesterday's)
  strategies/orb.py Strategy #2: opening-range breakout, bracket entries
                    (stop = range low, target = entry + 2R), params from
                    strategies.toml (tomllib, zero new deps)
  engine.py         `python -m plutus.engine`: crash-safe startup (session
                    ledger → crash alert → baseline → fills → reconcile →
                    mark-if-missed → scheduler), SIGTERM clean stop; jobs:
                    rotation 15:50, ORB per-minute 9–16h, fills every 20s,
                    loss watch every 60s
alembic/            Migrations; env.py resolves db_url from Settings,
                    tests override via `alembic -x db_url=...`
tests/              fakes.py (FakeAdapter double) + config, migrations, order
                    model, risk, alpaca adapter (chaos + key safety), dashboard
```

## Key decisions

- **Trading mode:** paper is the hard default. Platform-level live requires
  `TRADING_MODE=live` **and** a `live.lock` file in the repo root
  (`config.effective_trading_mode`). A third per-strategy `enabled_live` gate
  is applied at strategy load (later phase). Tests pin all fail-to-paper paths.
- **DB:** SQLite via SQLAlchemy 2.0 + Alembic. No SQLite-only features in the
  schema, so a Postgres move is a connection-string change.
- **Package layout:** `src/plutus/` with `uv` for env/deps. Dev tooling:
  pytest, ruff (line 100, py312), mypy `strict`.
- **Secrets:** `.env` only, never committed; `live.lock` and `KILL` are
  runtime state and gitignored too.
- Dependencies beyond §2's list: `uvicorn` (serving FastAPI) and `httpx`
  (FastAPI TestClient) — implied by the stack, flagged per rule 4.
- **RiskManager (Phase 1 shim):** enforces the routing contract (sole caller of
  `submit_order`), dedupes idempotency keys against the `orders` table, and
  rejects any non-paper effective mode outright — Phase 1 has no live
  enablement. The full §8 gate set (sizing, loss halts, rate limits, kill
  switch, reconciliation) is deliberately deferred to Phase 4.
- **Idempotency / timeout recovery:** the OrderIntent UUID rides Alpaca's
  `client_order_id`. On an ambiguous submit failure (timeout/connection drop)
  the adapter looks the order up by that key: found → return its receipt;
  absent → raise `OrderSubmitError`, never auto-resubmit. A rejected row also
  permanently retires its key (a retry needs a fresh intent), which fails safe.
- **Key selection is a safety property:** the adapter factory takes the
  *effective* mode; the paper path reads only the paper key pair (pinned by
  test — live keys are never touched when mode resolves to paper). Missing
  keys raise `MissingCredentialsError`; the dashboard degrades to a
  configure-keys notice instead of crashing.
- **No python-multipart:** the order form is parsed from the urlencoded body
  with stdlib `parse_qs`, because FastAPI's `Form()`/Starlette's `form()`
  would drag in `python-multipart`, which §2 does not approve.
- **Fill confirmation is polling, not websocket:** `/partials/orders` refreshes
  non-terminal orders via `get_order_status` on a 3s HTMX cycle. §3's
  websocket trade-updates stream belongs with the continuously running engine
  (Phase 5); the dashboard's HTMX script is loaded from unpkg (pinned 1.9.12).
- **vectorbt is the oracle, not the engine (reviewable deviation from §2's
  plain reading):** the daily backtester is a thin pandas weights→returns
  engine whose convention semantics are pinned by hand-computed micro-case
  tests; vectorbt (§2's library) independently reproduces the SMA-cross
  acceptance backtest to 1e-6 relative. Rationale: the fill convention is the
  load-bearing knob (prior analysis swung ~1.77→1.13 Sharpe / −47%→−83% maxDD
  across conventions) and its math must be first-party and exactly testable.
- **`fill` is a required engine parameter** (`same_close` / `next_open` /
  `next_close`) — no default, callers must name the convention; it persists in
  every backtest_runs record. §7's (b) is ambiguous about next-open vs
  next-close, so both exist and reports can bracket all three.
- **Adjusted bars only** (`adjustment=all` on IEX): TQQQ splits would poison
  SMA/RSI signals otherwise. A future split invalidates cached history for
  that symbol — flush its bars + bar_coverage rows and re-fetch (manual for
  now; the bars table records the adjustment mode).
- **Cache design:** bar_coverage stores contiguous fetched ranges per
  (symbol, interval); requests inside coverage never touch the vendor. Daily
  timestamps normalize to UTC midnight at the cache boundary (vendors stamp
  session-open ET). Deliberately avoids exchange-calendar hole accounting.
  Two edge rules matter because coverage never re-fetches: the vendor request
  end is padded +1 day for daily bars (vendor stamps sit inside the session,
  after UTC midnight — an unpadded request silently drops the final day), and
  the end bound is clamped to the last completed UTC day (a request "through
  today" must not claim coverage for a bar still forming).
- Report conventions: √252/252 annualization, rf=0, Sortino = downside
  deviation over all periods (target 0), trade = contiguous nonzero-weight run
  per symbol with gross-of-cost P&L.
- Dependencies added: `vectorbt`, `pandas` promoted to direct (§2-approved);
  `pandas-stubs` dev-only for mypy strict.
- **Data reality (Phase 3, blocking item for user review):** Alpaca IEX daily
  history for this account starts 2020-07-27 (~6y rolling window, apparently a
  free-tier limit). With the 200-bar warmup the effective backtest range is
  2021-05-14 → present — the spec's "≥2015" and the §7 "must include 2020"
  promotion requirement are unreachable on this feed. Options (user decision):
  accept the shorter window, pay for SIP/deeper data, or approve a new data
  dependency (rule 4). Also: IEX auction closes ≠ consolidated closes — signal
  noise vs Composer-era numbers.
- **RSI definition is a live question:** the tree came from a platform whose
  RSI definition can't be verified. Wilder's is our default; under Cutler's
  the allocation differs on 13.7% of days and next_close Sharpe moves
  1.21→0.84. Flagged for user review rather than silently chosen.
- Backtest runs 1–3 in backtest_runs are the Phase 3 fixed-tree records
  (same_close / next_close / next_open, 2 bps, Wilder).
- **Phase 3 review resolutions (user, 2026-08-16):** shorter data window
  (2020-07→present) accepted; RSI stays Wilder's.
- **§8 gate chain (Phase 4):** kill → mode (paper-only until Phase 9) →
  dedupe → strategy-enabled → stale-data → market-hours → rate-limits →
  priced gates (position size, stop-based risk, leveraged cap, concurrent).
  Exits bypass entry gates AND the rate limiter (a flatten must never
  throttle); a flip past flat counts as an entry; crypto pairs are exempt
  from market-hours (24/7). Unpriceable entries fail closed. **Behavior
  change:** manual equity entries outside RTH are now rejected (the Phase 1
  Sunday SPY order would no longer be accepted); crypto is unaffected.
- **Positions book on FILLS, never on acceptance** (bot_positions). Reconcile
  is scoped to bot-owned symbols: mismatch → halt owners + critical alert;
  unknown broker positions (the human's own holdings) → warning only;
  in-flight orders are resolved first, never halted on. Symbols normalize
  slash-less for broker comparison (BTC/USD ↔ BTCUSD).
- **Dollar allocations, not account fractions:** strategy risk budgets are
  absolute USD (strategy_state.allocation_usd, default RiskConfig value) —
  the $42M account equity is dominated by non-bot holdings. Daily-loss halt
  measures against the 09:30 day-start mark; equity per strategy comes from
  an injectable lookup (real P&L accounting arrives with the Phase 5 engine).
- **Kill switch:** KILL file (checked in runtime_root, fail-closed on a bad
  root) or POST /kill with typed double-confirm → cancel all open orders,
  flatten every bot position, disable every strategy; un-kill is manual file
  removal. Alerting is a pluggable callable (log-backed until Telegram in
  Phase 5). stop_price on OrderIntent is sizing-only until 6b brackets.
- The routine 15:55 auto-flatten runs with disable=False and never touches
  enable state — a daily-loss halt from earlier in the day survives it
  (pinned by chaos test). Partial fill followed by cancel/expiry halts the
  owning strategy with a critical alert (rule 6: never guess).
- **Known limitation for Phase 5 (shared-symbol reconcile):** reconcile
  compares bot-expected qty against TOTAL broker qty per symbol — one
  account, so manual holdings add in. The 1-share manual SPY order filling
  Monday collides with Strategy #2 (ORB on SPY): first bot SPY trade would
  mismatch and halt. Phase 5 needs a manual-baseline offset (likely marked
  at day start); until then, close manual test positions in bot symbols.
- **Phase 5 resolutions:** daily_loss_check now guarded to 09:30–16:00 ET;
  the manual baseline absorbs the user's own holdings (LINKUSD/BTC no longer
  alert every reconcile, and the shared-symbol SPY collision is retired —
  expected = bot book + baseline). Mid-day manual trades still mismatch
  until the next mark: §8-correct, by design.
- **Fill confirmation is polling (reviewable deviation from §3's websocket):**
  the engine syncs/ingests fills every 20s. Broker-side brackets protect ORB
  positions regardless of process health; Strategy #1 trades once a day.
  Websocket revisits at Phase 6b if Mode B's latency budget requires it.
- **Whole shares only, floored,** with a $50 rebalance dead-band against
  daily 1-share churn. Brackets don't combine with fractional orders.
- **Per-strategy position-cap overrides:** the §8 20% default contradicts a
  100%-allocation rotation strategy by construction; RiskConfig carries
  max_position_pct_overrides (tqqq_rotation → 100%). ORB entries on tight
  stops can exceed the 20% notional cap and be rejected — the gate wins; a
  cap-to-fit sizing negotiation is 6b's problem.
- **Qty precision:** broker crypto quantities carry 9+ decimals; qty columns
  widened to Numeric(24,10) and reconcile tolerance is scale-aware
  (max(1e-6, 1e-8·qty)) — found by live smoke, pinned by regression test.
- **Session ledger** (engine_sessions) is the §12/§13 acceptance clock:
  ≥30 clean sessions. A crash can't alert itself; the next startup alerts on
  any prior unclean row.
- **Known debt for Phase 4:** `config.REPO_ROOT` is derived from `__file__`,
  which is only correct for an editable install. For `live.lock` a wrong root
  fails safe (resolves to paper), but the Phase 4 kill switch checks `KILL` in
  the same root and a wrong root there fails unsafe — make the runtime root
  explicit/configurable when building the RiskManager.

## Verification (Phase 5 build — acceptance clock runs in calendar time)

- `uv run pytest` — 172 passed; ruff + mypy strict clean
- Live smoke (2026-08-16, real paper account): engine composition starts,
  marks baseline (LINKUSD + BTCUSD at full precision), ingests 2 historical
  fills, reconciles clean with the pending manual SPY order in-flight,
  writes and cleanly closes an engine_sessions row. Scheduler job registry
  verified. Telegram unconfigured → log degrade confirmed.
- **Acceptance (≥30 clean sessions, zero unhandled errors, fills match
  expectations) accrues from the first live session onward** — the build is
  done; the clock is calendar time.

## Verification (Phase 4 acceptance — chaos suite)

- `uv run pytest` — 142 passed; ruff + mypy strict clean
- Chaos drills green: kill drill (file + endpoint; cancel-failure alerts and
  continues; wrong runtime root fails closed), reconcile mismatch → halt +
  critical alert, unknown broker position → warning without halt, in-flight
  order → no false halt then clean after fill, daily-loss breach → flatten +
  disable with a 1/min rate limit proven inert, duplicate fill sync books
  once, rate-limit bursts, RTH rejection with crypto carve-out, stale-data
  entry block, flatten failure mid-halt → alert + disabled without crash,
  scheduler jobs skip holidays.

## Verification (Phase 2 acceptance)

- `uv run pytest` — 71 passed: engine equity paths asserted against explicit
  hand arithmetic per fill mode; next_close ≡ same_close∘shift property;
  costs-on-fill-day; cache hit/merge/normalization + final-day/clamp
  regressions; SMA(10/30) cross matches vectorbt `Portfolio.from_signals` to
  1e-6 relative; run persistence round-trip
- `uv run ruff check .` / `uv run mypy` (strict) — clean
- **Real-data smoke (2026-08-16, paper keys, IEX):** 261 TQQQ daily bars
  2025-08-01→2026-08-14 fetched in one vendor call (0.15s); identical second
  request served from SQLite in 3ms with zero vendor calls. (An earlier smoke
  exposed the dropped-final-day cache bug fixed above.)

## Verification (Phase 1 acceptance)

- 35 tests incl. chaos (timeout mid-order → idempotency-key lookup, no double
  submit), key-selection safety, fill-confirmation polling, live-mode
  rejection.
- **Acceptance round-trip (2026-08-16, real paper account):** dashboard form →
  RiskManager → AlpacaAdapter → Alpaca paper API. A BTC/USD 0.001 market order
  (gtc — crypto trades 24/7 and rejects day TIF) filled and the 3s status poll
  flipped the row to `filled`; the position and cash updated on refresh. A
  1-share SPY day order was accepted and queued (submitted on a Sunday; fills
  at next RTH open). The order form has a TIF select (day/gtc) for this.
