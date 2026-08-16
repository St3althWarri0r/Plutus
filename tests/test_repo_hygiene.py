"""Secrets and runtime state must never be committable (CLAUDE.md rule 3)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def gitignore_lines() -> list[str]:
    text = (REPO_ROOT / ".gitignore").read_text()
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_env_is_gitignored() -> None:
    assert ".env" in gitignore_lines()


def test_runtime_state_files_are_gitignored() -> None:
    lines = gitignore_lines()
    assert "live.lock" in lines
    assert "KILL" in lines
