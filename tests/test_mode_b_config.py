"""Playbook loader (§9B.5) and scanner filters (§9B.1, IEX-scaled)."""

from pathlib import Path

from plutus.ai.mode_b_config import load_playbook
from plutus.ai.scanner import Candidate, filter_candidates

PLAYBOOK = Path(__file__).resolve().parent.parent / "playbook.yaml"


def test_playbook_loads_five_setups_and_config() -> None:
    pb = load_playbook(PLAYBOOK)
    assert set(pb.setups) == {
        "opening_range_breakout",
        "gap_and_go",
        "vwap_reclaim",
        "first_pullback",
        "failed_breakdown_reversal",
    }
    assert pb.mode_b.max_concurrent == 2
    assert pb.mode_b.daily_stop_r == -2.5
    assert pb.scanner.premarket_volume_min_iex == 5000
    assert "NVDA" in pb.scanner.static_candidates


def test_setup_names_are_closed_set() -> None:
    pb = load_playbook(PLAYBOOK)
    assert pb.is_valid_setup("vwap_reclaim")
    assert not pb.is_valid_setup("my_new_genius_setup")


def make_candidate(**kw: object) -> Candidate:
    base = dict(
        symbol="NVDA",
        gap_pct=3.0,
        premarket_volume=8_000,
        price=180.0,
        adv_iex=50_000,
    )
    base.update(kw)
    return Candidate(**base)  # type: ignore[arg-type]


def test_filters_pass_good_candidate() -> None:
    pb = load_playbook(PLAYBOOK)
    kept = filter_candidates([make_candidate()], pb.scanner)
    assert len(kept) == 1


def test_filters_reject_each_dimension() -> None:
    pb = load_playbook(PLAYBOOK)
    rejects = [
        make_candidate(gap_pct=1.0),            # gap too small
        make_candidate(premarket_volume=100),   # thin premarket (IEX-scaled)
        make_candidate(price=2.0),              # under $5
        make_candidate(price=900.0),            # over $500
        make_candidate(adv_iex=1_000),          # illiquid
    ]
    assert filter_candidates(rejects, pb.scanner) == []


def test_negative_gap_magnitude_counts() -> None:
    pb = load_playbook(PLAYBOOK)
    kept = filter_candidates([make_candidate(gap_pct=-4.0)], pb.scanner)
    assert len(kept) == 1  # |gap| ≥ 2% per §9B.1
