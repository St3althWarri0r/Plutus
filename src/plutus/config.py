"""Application settings and trading-mode resolution.

Paper trading is the permanent default. Live mode requires ALL of:
TRADING_MODE=live in the environment, a live.lock file in the repo root,
and (from Phase 3 onward) per-strategy enabled_live=true. Absence of any
one of these resolves to paper.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TradingMode = Literal["paper", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_mode: TradingMode = "paper"
    db_url: str = "sqlite:///plutus.db"

    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper_key: str | None = None
    alpaca_paper_secret: str | None = None

    schwab_app_key: str | None = None
    schwab_app_secret: str | None = None
    schwab_token_path: str | None = None

    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_env: str = "production"

    anthropic_api_key: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def effective_trading_mode(
    settings: Settings | None = None,
    repo_root: Path = REPO_ROOT,
) -> TradingMode:
    """Resolve the platform-wide trading mode.

    Live requires BOTH TRADING_MODE=live and a live.lock file in the repo
    root; anything else is paper. Per-strategy enabled_live is a third,
    separate gate applied where strategies are loaded (later phase) — this
    function can only ever grant platform-level live.
    """
    settings = settings or get_settings()
    if settings.trading_mode == "live" and (repo_root / "live.lock").is_file():
        return "live"
    return "paper"
