"""playbook.yaml loader (§9B.5): setups are a closed set the agent cannot
extend; discipline numbers and scanner thresholds live here, not in code."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class SetupSpec(BaseModel):
    description: str
    entry: str
    stop: str
    target: str


class ScannerConfig(BaseModel):
    # IEX-scaled (§9B.1 deviation): our feed carries ~2-3% of consolidated
    # volume; spec thresholds are divided accordingly (probe-calibrated)
    min_gap_pct: float = 2.0
    premarket_volume_min_iex: float = 5_000
    adv_min_iex: float = 30_000
    price_min: float = 5.0
    price_max: float = 500.0
    static_candidates: list[str] = Field(default_factory=list)
    max_watchlist: int = 6


class ModeBConfig(BaseModel):
    allocation_fraction_r: float = 0.0075  # 1R = 0.75% of allocation (§9B.4)
    max_concurrent: int = 2
    max_round_trips_per_day: int = 8
    daily_stop_r: float = -2.5
    anti_tilt_consecutive_losses: int = 2
    anti_tilt_cooldown_minutes: int = 30
    breakeven_at_r: float = 1.0
    scale_out_at_r: float = 2.0
    scale_out_fraction: float = 0.34
    off_plan_trades_per_day: int = 1
    off_plan_size_factor: float = 0.5
    no_new_entries_after: str = "15:30"


class Playbook(BaseModel):
    setups: dict[str, SetupSpec]
    scanner: ScannerConfig
    mode_b: ModeBConfig

    def is_valid_setup(self, name: str) -> bool:
        return name in self.setups

    def setups_prompt_block(self) -> str:
        """The system-prompt section describing the closed setup set —
        cache-controlled upstream, so keep it deterministic."""
        lines = ["PLAYBOOK (the ONLY setups you may name):"]
        for name, spec in sorted(self.setups.items()):
            lines.append(f"- {name}: {spec.description}")
            lines.append(f"  entry: {spec.entry} | stop: {spec.stop} | target: {spec.target}")
        return "\n".join(lines)


def load_playbook(path: Path) -> Playbook:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Playbook.model_validate(data)
