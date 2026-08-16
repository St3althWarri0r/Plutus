# Build Spec: "Helm" — Personal Trading Platform + Autotrader

**How to use this file:** Create an empty git repo, save this as `CLAUDE.md` in the root, open Claude Code, and say: *"Read CLAUDE.md. Confirm your understanding of the architecture in ≤200 words, ask me only for missing credentials/decisions, then begin Phase 0."* Work one phase per session. Do not let it one-shot the whole thing.

---

## 0. What this is

A single-user, self-hosted portfolio dashboard and automated trading engine for **my personal accounts only**. Not multi-tenant, not a commercial product, no other users ever. Think: M1's dashboard + Composer's strategy engine + my own risk layer, running on my hardware.

Two halves:

1. **Read layer (all accounts):** aggregate positions/balances across M1, Vanguard, Schwab, and Alpaca into one net-worth dashboard with daily history.
2. **Execution layer (API brokers only):** run systematic strategies and an AI-analyst layer that trade through Alpaca (primary, has paper trading) and Schwab (secondary, live-only API). M1 and Vanguard are **read-only forever** — no official APIs; do not scrape or use reverse-engineered endpoints.

## 1. Ground rules for you (Claude Code)

1. Build in the phases defined in §12. Each phase ends with: tests passing, `ARCHITECTURE.md` updated, a git commit, and a ≤10-line summary of what changed.
2. **Paper trading is the permanent default.** Live mode requires ALL of: `TRADING_MODE=live` in `.env`, a `live.lock` file present in the repo root, AND per-strategy `enabled_live: true` in config. Absence of any one → paper.
3. Never write secrets into code, logs, or commits. All credentials via `.env` + `pydantic-settings`. Add `.env` to `.gitignore` in Phase 0.
4. Ask before adding any dependency not listed in §2. Prefer boring, maintained libraries.
5. Every function that can place, modify, or cancel an order must route through the RiskManager (§8). No exceptions, including "temporary" test scripts.
6. When my instructions conflict with safety of capital, stop and ask. When a broker API behaves unexpectedly (partial fill, unknown state), the correct behavior is: halt strategy, alert me, reconcile — never guess.
7. Write property/unit tests with mocked brokers. Never run tests against live credentials. Include "chaos" tests: API timeout mid-order, duplicate webhook, stale data.

## 2. Stack (decided — don't relitigate)

- Python 3.12+, managed with `uv`
- FastAPI backend; server-rendered dashboard with Jinja2 + HTMX (no SPA unless a component truly needs it)
- SQLite via SQLAlchemy 2.0 + Alembic migrations (design schema so a later Postgres move is a connection-string change)
- APScheduler for market-hours jobs; `exchange_calendars` for NYSE sessions/half-days
- `alpaca-py` (broker #1 + market data), `schwab-py` (broker #2), `anthropic` SDK (AI layer), `plaid-python` (read-only aggregation)
- Backtesting: `vectorbt` for vectorized daily strategies; a small custom event-driven loop for intraday
- `pytest`, `ruff`, `mypy`; Docker Compose for deployment; structured JSON logging (`structlog`)

## 3. Broker abstraction layer

Define one interface; brokers are adapters behind it:

```python
class BrokerAdapter(Protocol):
    def get_account(self) -> AccountState          # equity, cash, buying power, margin
    def get_positions(self) -> list[Position]
    def submit_order(self, order: OrderIntent) -> OrderReceipt
    def cancel_order(self, broker_order_id: str) -> None
    def get_order_status(self, broker_order_id: str) -> OrderStatus
    def get_fills(self, since: datetime) -> list[Fill]
```

- **AlpacaAdapter** (build first): supports paper (`https://paper-api.alpaca.markets`) and live endpoints from the same code path; websocket trade updates for fill confirmations.
- **SchwabAdapter** (build second): OAuth2 flow with token persistence; access tokens expire ~30 min, and the refresh token itself expires every **7 days** — build a token-health check that alerts me 24h before expiry so I can re-auth (this is Schwab's known limitation; the bot must degrade gracefully to "halted, awaiting re-auth," never trade on stale auth). Schwab has **no paper environment** — its sandbox is synthetic data only — so all Schwab strategy validation happens on Alpaca paper first, then Schwab live at minimum size.
- Every `OrderIntent` carries a client-generated idempotency key (UUID); adapters must never submit the same key twice (protects against retry-after-timeout double orders).
- `IBKRAdapter` — stub only, don't implement.

## 4. Account aggregation (read-only)

- Plaid Investments integration for M1 and Vanguard: holdings, balances, transactions. Nightly sync job + on-demand refresh button.
- Manual CSV import fallback (M1 and Vanguard both export CSVs) for anything Plaid misses.
- `snapshots` table: one row per account per day (equity, cash, positions JSON) → powers net-worth-over-time chart.
- Alpaca/Schwab positions come from their APIs directly, not Plaid.

## 5. Market data

- v1: Alpaca Market Data API (free IEX feed) — minute + daily bars, cached in a `bars` table so backtests never re-fetch.
- Wrap it in a `DataProvider` interface so Polygon/Databento can slot in later without touching strategies.
- Data-quality gate: if the latest bar is older than 2× its interval during market hours, mark the feed stale and block new entries.

## 6. Strategy engine

A `Strategy` is a class with `warmup_bars`, `universe`, `schedule` (e.g., "daily @ 15:50 ET" or "every 5m"), and `on_bar(context) -> list[TargetPosition]`. The engine converts targets to orders, diffs against current positions, and hands `OrderIntent`s to the RiskManager. Strategies never talk to brokers directly.

**Strategy #1 — port my existing TQQQ rotation (daily, runs 15:50 ET):**

```
IF TQQQ close > TQQQ 200d SMA:
    IF RSI(10, TQQQ) > 79:  hold 50% UVXY / 50% BSV      # overbought hedge, split per Aug-2026 revision
    ELSE:                   hold 100% TQQQ
ELSE:
    IF RSI(10, TQQQ) < 31:  hold 100% TECL
    ELIF TQQQ close > TQQQ 20d SMA:  hold 100% TQQQ
    ELSE:                   hold 100% of higher-RSI(10) of {BSV, SQQQ}
# Note: legacy SOXL leg deliberately removed. Regime filter is TQQQ-based, not QQQ-based.
```

**Strategy #2 — one intraday template (opening-range breakout on SPY/QQQ)** so the intraday code path, bracket orders, and end-of-day auto-flatten all get exercised. Parameters in config, not code.

## 7. Backtester requirements

- Costs model: configurable slippage in bps + half-spread + per-share commission (default 0 commission, 2 bps slippage).
- **Must report every daily strategy under two execution conventions: (a) same-close (signal and fill on the same close) and (b) t+1 (signal at close, fill next open/close).** My prior analysis showed the same strategy swinging from ~1.77 Sharpe / −47% maxDD to ~1.13 / −83% between conventions — plan and size around the *lagged* numbers, and flag any strategy whose edge dies under (b).
- Walk-forward: fit/tune on rolling in-sample windows, validate out-of-sample; report OOS-only metrics separately.
- Standard report per run: CAGR, Sharpe, Sortino, maxDD, exposure %, turnover, trade count, worst 5 trades, yearly returns table. Persist runs to DB so results are comparable over time.
- A strategy cannot be promoted to paper trading without a backtest covering ≥5 years including 2020 and 2022, and cannot be promoted to live without ≥30 paper sessions matching backtest expectations (see §13).

## 8. RiskManager (deterministic, runs before EVERY order)

Hard gates, all configurable, these defaults:

| Gate | Default |
|---|---|
| Max position size | 20% of strategy equity per symbol |
| Max risk per trade (stop-based sizing) | 1% of strategy equity |
| Max concurrent positions (intraday strats) | 3 |
| Daily loss halt | −3% strategy equity → flatten, disable strategy until manual re-enable |
| Leveraged-ETF (2x/3x) notional cap | 25% of total bot equity |
| Order rate limit | 10 orders/min, 100/day per strategy |
| Market-hours check | reject entries outside RTH; auto-flatten intraday positions by 15:55 ET |
| Kill switch | `KILL` file in repo root or dashboard button → cancel all open orders, flatten all bot positions, disable all strategies |
| Reconciliation | at 09:15 and 16:15 ET compare DB positions vs broker; any mismatch → halt + alert |

The RiskManager is the only component allowed to call `BrokerAdapter.submit_order`.

## 9. AI layer (Anthropic API)

Two modes, both fully implemented. **Mode A** supervises the systematic strategies. **Mode B is the day trader** — an agentic loop that replicates a disciplined human discretionary day trader's full workday. Mode B is the centerpiece of this build: implement it to behave identically in paper and live, so promotion changes nothing about how it trades.

Shared constraints (both modes):
- Every AI call and response logged verbatim to an `ai_audit` table (prompt hash, model, tokens, latency, cost, decision).
- Strict JSON outputs via tool-use schema; malformed → one retry → no-op.
- Models config-switchable; default decision model `claude-sonnet-4-6`.

### Mode A — supervisor for systematic strategies

1. **Pre-market brief (08:30 ET):** overnight headlines, futures, VIX, current positions → `{regime: risk_on|neutral|risk_off, notes, watch_items}`. Regime feeds strategies as an input (risk_off halves sizing).
2. **Trade review (per signal):** `{action: approve|veto|resize, size_multiplier, rationale}`; multiplier hard-clamped to [0.5, 1.5] in code; approvals still pass all §8 gates.
3. **Post-market journal (16:30 ET):** P&L attribution + anomalies.
4. On AI failure/timeout (>20s): systematic strategies proceed deterministically at 0.5× size — AI unavailability never blocks risk-reducing exits.

### Mode B — the AI day trader

Design goal: replicate the workflow of a disciplined human discretionary day trader (momentum/technical, prop-desk style). Not HFT, not scalping, not swing trading. The model makes the trading decisions; deterministic code enforces the discipline a good human trader imposes on themselves.

**9B.1 Session structure (ET) — the trader's day:**
- **07:30–09:15 Pre-market prep:** a deterministic scanner pulls candidates (|gap| ≥ 2%, pre-market volume ≥ 200k, price $5–$500, 20-day ADV ≥ 1M shares, spread ≤ 10 bps). Agent receives candidates + per-name headlines + market context (futures, VIX, prior day) and writes a **day plan**: ≤6 watchlist names, each with bias, playbook setup, entry trigger level, stop, targets, and invalidation. Plan persists to DB and anchors the session — plan the trade, trade the plan.
- **09:15–09:30:** agent re-checks plan against opening indications. No orders before 09:30.
- **09:30–11:30 prime window:** full decision loop (9B.2).
- **11:30–13:30 midday chop:** no new entries unless the agent explicitly grades a setup A+ and regime is risk_on (both logged). Manage open positions only.
- **13:30–15:30:** normal loop. **15:30:** no new entries. **15:55:** RiskManager force-flattens anything still open. Flat overnight, always.
- **16:15 journal:** per-trade entries (setup, thesis, execution grade A–F, what went wrong/right) + session summary + running stats. Weekly, the agent reviews its own journal and stats and proposes playbook emphasis changes (proposals only — I approve).

**9B.2 Decision loop:**
- Triggers: every completed 1-min bar for watchlist names; every 15s for open positions; and on events (price crosses a plan level, halt/resume, position hits ±1R).
- Input state packet (server-computed, compact, target ≤4k tokens): summarized last 30×1m + 10×5m bars, VWAP, RVOL vs 20-day, EMA9/EMA20, pre-market high/low, prior day high/low/close, open position & orders, session P&L in R, trades taken today, time of day, the morning day plan, and the session tape (9B.3).
- Output schema: `{action: enter|exit|scale_out|move_stop|cancel|hold|stand_down, symbol, side, setup_name, entry_type: market|limit, limit_price?, stop_price, targets[], size_R, confidence, reason}`.
- **All entries are bracket orders** (entry + stop-loss + take-profit resting at the broker). An entry without a stop is rejected in code. If the agent process dies mid-position, broker-side brackets still protect it.
- Every action routes through the §8 RiskManager plus the 9B.4 gates.

**9B.3 Session memory — the trader's tape:** the agent maintains a running narrative it updates after each decision (≤1,500 tokens, oldest details auto-compacted). Its 14:00 self must remember the 09:45 stop-out and the plan it wrote at 08:50. Persist to DB; on restart, reload plan + tape, reconcile positions vs broker, then resume.

**9B.4 Day-trader discipline (enforced in code, not prompt):**

| Rule | Default |
|---|---|
| 1R (risk unit) | 0.75% of Mode B allocation |
| Position size | R$ ÷ (entry − stop) |
| Max concurrent positions | 2 |
| Max round trips/day | 8 |
| Daily stop | −2.5R (realized + unrealized) → flatten, done for the day |
| Anti-tilt cooldown | 2 consecutive full-R losses → 30 min no new entries |
| Breakeven rule | stop auto-moves to entry at +1R unless agent overrides with logged reason |
| Scale-out rule | mandatory ≥1/3 off at +2R |
| Adding to losers | rejected in code, always |
| Off-plan trades | agent may take ONE unplanned momentum trade/day, at half size |

**9B.5 Playbook (`playbook.yaml`, not prose):** opening-range breakout (5m/15m), gap-and-go continuation, VWAP reclaim, first pullback in trend, failed-breakdown reversal at a key level. Each defined with entry criteria, stop logic, target logic. The agent's discretion is *which* setup, *when*, and *whether* — it names the setup on every entry and cannot invent new ones. New setups get added by editing the YAML.

**9B.6 Models, cost, latency:**
- Tiered calls: a Haiku-class model runs the per-bar "anything actionable?" monitor; escalate to Sonnet for the day plan and all enter/exit/adjust decisions. Prompt-cache the system prompt + playbook.
- Expected volume ≈ 400–700 monitor calls + 30–80 decision calls/day → roughly $2–5/day tiered (vs $8–15/day all-Sonnet). Log per-call cost; show $/day on the dashboard.
- Latency budget: bar close → packet build (<300ms) → LLM (2–8s) → gates (<50ms) → order (~200ms). Fills land 3–10s after signal — acceptable for liquid momentum names, which is exactly why the liquidity floor in 9B.1 exists. Alert if p95 decision latency exceeds 12s.
- On model outage mid-session: cancel pending entries; open positions ride their broker-side brackets. Never freeze with unprotected exposure, never enter without a fresh decision.

**9B.7 Promotion bar (paper → live):** Mode B starts hard-locked to paper; Mode B + live keys must raise at startup. The lock comes off only when ALL hold, shown live on a stats page: ≥60 sessions AND ≥100 completed trades; profit factor ≥ 1.3; expectancy ≥ +0.15R/trade net of modeled costs; daily stop never breached; zero risk-gate violations. Below the bar, it stays paper. I flip the lock manually — never you.

## 10. Dashboard (FastAPI + HTMX)

- Header: giant PAPER (green) / LIVE (red) mode banner. No ambiguity, ever.
- Net worth: total + per-account cards (M1, Vanguard, Schwab, Alpaca), 1M/1Y/all-time chart from snapshots.
- Bot page: per-strategy equity curve, open positions, today's orders/fills, regime badge, AI journal feed, enable/disable toggles, KILL button (double-confirm).
- Backtest page: run history, side-by-side convention (a) vs (b) metrics.
- Auth: single local password + served only on LAN/Tailscale. Never expose to the public internet.

## 11. Ops

- Alerts via Telegram bot (fallback: email): every live fill, every halt, daily loss >1.5%, token-expiry warnings, reconciliation failures, process crash.
- Docker Compose with `restart: unless-stopped`; on restart, the engine reconciles state vs brokers before resuming (crash-safe: DB is the source of intent, broker is the source of truth).
- Nightly DB backup to a second disk; retain 30 days.

## 12. Phase plan (acceptance criteria in parentheses)

- **Phase 0 — Scaffold:** repo layout, config, logging, DB schema, migrations, CI-style test runner. (App boots; `pytest` green.)
- **Phase 1 — Alpaca read + paper orders:** AlpacaAdapter, account/positions display, manual paper order form on dashboard. (Round-trip a paper order with fill confirmation.)
- **Phase 2 — Data + backtester:** bar cache, cost model, both execution conventions, report persistence. (Reproduce a known SMA-cross backtest within tolerance.)
- **Phase 3 — Strategy #1 port + validation:** implement §6 tree; backtest ≥2015–present under both conventions; walk-forward. (Metrics reviewed by me before proceeding.)
- **Phase 4 — RiskManager + scheduler:** all §8 gates with tests, incl. kill-switch and reconciliation drills. (Chaos tests pass.)
- **Phase 5 — Paper autotrading:** Strategy #1 + #2 live on Alpaca paper, alerts on. (≥30 sessions, zero unhandled errors, fills match expectations.)
- **Phase 6 — AI Mode A:** brief/review/journal + audit table. (Outage drill passes at 0.5× fallback.)
- **Phase 6b — AI day trader (Mode B):** scanner, day-plan generator, tiered decision loop, session tape, `playbook.yaml`, bracket-order entries, 9B.4 discipline gates, stats page. (One full paper session runs end-to-end; killing the agent process mid-position leaves a broker-side bracket protecting it; per-day cost visible.)
- **Phase 7 — Aggregation dashboard:** Plaid for M1/Vanguard, CSV fallback, snapshots + net-worth chart. (All four accounts visible.)
- **Phase 8 — Schwab adapter:** OAuth + token-health alerts + minimum-size live test of a single order, manually triggered. (One clean round-trip.)
- **Phase 9 — Live, small:** Strategy #1 live on designated account at reduced size per my instruction. (First week: daily manual review checklist generated by the app.)

## 13. Definition of done for live trading

All true, verified by an automated checklist page: 30+ consecutive clean paper sessions; paper P&L within expected band of backtest (convention **b**); reconciliation clean for 10 straight sessions; kill switch and daily-halt drills executed successfully in paper; alerting confirmed on my phone; `live.lock` created by me manually.

## Appendix — `.env` keys to expect

```
TRADING_MODE=paper
ALPACA_API_KEY= / ALPACA_SECRET_KEY=
ALPACA_PAPER_KEY= / ALPACA_PAPER_SECRET=
SCHWAB_APP_KEY= / SCHWAB_APP_SECRET= / SCHWAB_TOKEN_PATH=
PLAID_CLIENT_ID= / PLAID_SECRET= / PLAID_ENV=production
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN= / TELEGRAM_CHAT_ID=
DB_URL=sqlite:///helm.db
```

**First question to ask me before Phase 0:** which Alpaca account type I've opened (paper keys are instant; live can wait), and whether Schwab developer-app approval is done yet (registration at developer.schwab.com takes days — start it in parallel).
