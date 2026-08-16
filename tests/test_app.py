"""App boots and reports paper mode."""

from fastapi.testclient import TestClient

from plutus.app import create_app


def test_healthz_ok() -> None:
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "paper"


def test_index_shows_paper_banner() -> None:
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "PAPER" in resp.text
    assert "mode-paper" in resp.text
