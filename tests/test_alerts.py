"""Telegram alerting (§11): degrade to log without a token; never raise."""

from plutus.alerts import TelegramAlerter
from plutus.config import Settings


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []
        self.fail = fail

    def __call__(self, url: str, data: dict[str, str], timeout: float) -> None:
        if self.fail:
            raise ConnectionError("telegram unreachable")
        self.sent.append((url, data))


def settings(
    telegram_bot_token: str | None = None, telegram_chat_id: str | None = None
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )


def test_sends_when_configured() -> None:
    transport = FakeTransport()
    alerter = TelegramAlerter(
        settings(telegram_bot_token="tok123", telegram_chat_id="chat9"),
        transport=transport,
    )
    alerter("critical", "reconcile mismatch on SPY")

    ((url, data),) = transport.sent
    assert "bottok123/sendMessage" in url
    assert data["chat_id"] == "chat9"
    assert "CRITICAL" in data["text"] and "reconcile mismatch" in data["text"]
    assert alerter.configured


def test_no_token_degrades_to_log_only() -> None:
    transport = FakeTransport()
    alerter = TelegramAlerter(settings(), transport=transport)
    alerter("warning", "hello")
    assert transport.sent == []
    assert not alerter.configured


def test_transport_failure_never_raises() -> None:
    alerter = TelegramAlerter(
        settings(telegram_bot_token="tok", telegram_chat_id="c"),
        transport=FakeTransport(fail=True),
    )
    alerter("critical", "must not blow up the trading loop")  # no exception
