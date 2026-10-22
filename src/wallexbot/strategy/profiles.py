"""Strategy profiles for the bot: `strict` (S1 as specified) and `demo` (an EXERCISE profile).

strict  MomentumParams() unchanged. Under realistic costs (0.35 % taker per side, see
        docs/COSTS.md and reports/v2_summary.md) its cost gate is practically unreachable, so a
        strict mock/shadow run usually shows abstentions only. That is the honest default.

demo    Relaxed thresholds whose ONLY purpose is to make the pipeline do something observable in
        mock/shadow runs: signals, risk checks, orders, fills, exits, guardian ticks. NO EDGE IS
        CLAIMED; these values were not selected on any validation data and must never be read as a
        strategy. Changes against strict (everything else identical):
          cost_mult / cost_mult_tight   3.0 / 4.0  ->  0.5 / 0.5   (expected move >= 0.5x costs)
          volume_mult / _tight          1.8 / 2.2  ->  1.2 / 1.5
          atr_pct_min                   0.002      ->  0.0005      (lower ATR band)
          roc_z_min                     1.0        ->  0.5
          er_min                        0.3        ->  0.2
          min_score                     0.6        ->  0.5
        It is refused for any live path (assert_profile_allowed; config mode 'live' included).
"""
from __future__ import annotations

from dataclasses import replace

from wallexbot.strategy.config import MomentumParams

EXERCISE_BANNER = "EXERCISE PROFILE - NO EDGE CLAIMED - SHADOW/PAPER ONLY"
PROFILES = ("strict", "demo")
DEMO_ALLOWED_MODES = ("mock", "shadow")

DEMO_PARAMS = replace(MomentumParams(), cost_mult=0.5, cost_mult_tight=0.5, volume_mult=1.2, volume_mult_tight=1.5,
                      atr_pct_min=0.0005, roc_z_min=0.5, er_min=0.2, min_score=0.5)


class ProfileError(ValueError):
    pass


def assert_profile_allowed(profile: str, mode: str, config_mode: str | None = None) -> None:
    """`demo` exists for mock/shadow exercise runs only: any live path refuses it."""
    if profile not in PROFILES:
        raise ProfileError(f"unknown profile {profile!r} (allowed: {', '.join(PROFILES)})")
    if profile == "demo" and (mode not in DEMO_ALLOWED_MODES or config_mode == "live"):
        raise ProfileError(f"the demo profile is an exercise profile for {'/'.join(DEMO_ALLOWED_MODES)} only; "
                           f"refused for mode {mode!r} (config mode {config_mode!r})")


def momentum_params(profile: str, mode: str, config_mode: str | None = None) -> MomentumParams:
    assert_profile_allowed(profile, mode, config_mode)
    return DEMO_PARAMS if profile == "demo" else MomentumParams()
