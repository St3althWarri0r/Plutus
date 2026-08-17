"""Telegram alerting (§11). Degrades to structured logs when no token is set;
a transport failure logs and returns — an alert must never take down the
engine. The user creates the bot via BotFather and supplies the token/chat id
through .env; this code never handles the credential beyond reading settings.
"""

from collections.abc import Callable
from typing import Protocol

import requests

from plutus.config import Settings
from plutus.logging_setup import get_logger

log = get_logger("plutus.alerts")

Transport = Callable[[str, dict[str, str], float], None]


def _requests_transport(url: str, data: dict[str, str], timeout: float) -> None:
    requests.post(url, data=data, timeout=timeout).raise_for_status()


class AlerterProtocol(Protocol):
    def __call__(self, severity: str, message: str) -> None: ...


class TelegramAlerter:
    def __init__(self, settings: Settings, transport: Transport = _requests_transport) -> None:
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def __call__(self, severity: str, message: str) -> None:
        log_fn = log.error if severity == "critical" else log.warning
        log_fn("alert", severity=severity, message=message)
        if not self.configured:
            return
        try:
            self._transport(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                {"chat_id": str(self._chat_id), "text": f"[{severity.upper()}] {message}"},
                10.0,
            )
        except Exception as exc:
            log.error("alert_delivery_failed", error=str(exc), message=message)
