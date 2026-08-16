# Plutus

Single-user, self-hosted trading platform + autotrader. Spec: [CLAUDE.md](CLAUDE.md).
Current state: [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

```bash
uv sync
cp .env.example .env        # fill in keys as phases require them
uv run alembic upgrade head
uv run uvicorn --factory plutus.app:create_app
```

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Paper trading is the permanent default. Live mode requires `TRADING_MODE=live`
in `.env` **and** a `live.lock` file in the repo root **and** per-strategy
`enabled_live: true` — absence of any one resolves to paper.
