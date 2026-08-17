"""Trading-mode resolution: paper is the permanent default (CLAUDE.md rule 2)."""

from pathlib import Path
from typing import Any

from plutus.config import Settings, effective_trading_mode


def make_settings(**overrides: Any) -> Settings:
    # _env_file=None keeps a developer's local .env from leaking into tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_default_mode_is_paper(tmp_path: Path) -> None:
    assert effective_trading_mode(make_settings(), repo_root=tmp_path) == "paper"


def test_live_env_without_lock_file_is_paper(tmp_path: Path) -> None:
    settings = make_settings(trading_mode="live")
    assert effective_trading_mode(settings, repo_root=tmp_path) == "paper"


def test_lock_file_without_live_env_is_paper(tmp_path: Path) -> None:
    (tmp_path / "live.lock").touch()
    assert effective_trading_mode(make_settings(), repo_root=tmp_path) == "paper"


def test_live_requires_env_and_lock_file(tmp_path: Path) -> None:
    (tmp_path / "live.lock").touch()
    settings = make_settings(trading_mode="live")
    assert effective_trading_mode(settings, repo_root=tmp_path) == "live"


def test_live_lock_must_be_a_file_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "live.lock").mkdir()
    settings = make_settings(trading_mode="live")
    assert effective_trading_mode(settings, repo_root=tmp_path) == "paper"


def test_runtime_root_defaults_to_repo_root() -> None:
    from plutus.config import REPO_ROOT, resolve_runtime_root

    assert resolve_runtime_root(make_settings()) == REPO_ROOT


def test_runtime_root_overridable_via_settings(tmp_path: Path) -> None:
    from plutus.config import resolve_runtime_root

    settings = make_settings(runtime_root=str(tmp_path))
    assert resolve_runtime_root(settings) == tmp_path
