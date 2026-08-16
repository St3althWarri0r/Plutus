# Plutus — Architecture

Single-user, self-hosted portfolio dashboard + automated trading engine. Full
spec lives in [CLAUDE.md](CLAUDE.md); this file tracks what is actually built.

## Status: Phase 0 (scaffold) complete

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
  models.py         Snapshot (one row per account per day)
  app.py            FastAPI factory: /healthz, / (mode banner page)
  templates/        Jinja2 (HTMX arrives with the real dashboard in Phase 1)
alembic/            Migrations; env.py resolves db_url from Settings,
                    tests override via `alembic -x db_url=...`
tests/              config resolution, migration-to-head, app boot, gitignore hygiene
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
- **Known debt for Phase 4:** `config.REPO_ROOT` is derived from `__file__`,
  which is only correct for an editable install. For `live.lock` a wrong root
  fails safe (resolves to paper), but the Phase 4 kill switch checks `KILL` in
  the same root and a wrong root there fails unsafe — make the runtime root
  explicit/configurable when building the RiskManager.

## Verification (Phase 0 acceptance)

- `uv run pytest` — 10 passed
- `uv run ruff check .` / `uv run mypy` — clean
- `uv run uvicorn --factory plutus.app:create_app` boots; `/healthz` →
  `{"status":"ok","trading_mode":"paper"}`
