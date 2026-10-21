"""Config loader. Fail-closed: live mode is rejected unless fully armed."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from wallexbot.exchange.auth import AuthError, assert_live_armed
from wallexbot.redact import is_sensitive_key

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
ALLOWED_HOSTS = frozenset({"api.wallex.ir"})


class ConfigError(ValueError):
    pass


def _secret_like_path(node: object, path: str = "") -> str | None:
    """Dotted path of the first secret-like key holding text/structure (config is committed;
    secrets must come from the environment only), else None."""
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for k, v in items:
        p = f"{path}.{k}" if path else str(k)
        if is_sensitive_key(k) and v not in (None, "") and not isinstance(v, (bool, int, float)):
            return p
        found = _secret_like_path(v, p)
        if found:
            return found
    return None


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: Decimal
    max_position_pct: Decimal
    max_exposure_pct: Decimal
    max_open_positions: int
    max_order_value: Decimal
    daily_loss_soft_pct: Decimal
    daily_loss_hard_pct: Decimal
    drawdown_soft_pct: Decimal
    drawdown_hard_pct: Decimal
    consecutive_loss_cooldown: int
    min_notional: Decimal


@dataclass(frozen=True)
class Config:
    mode: str
    quote_currency: str
    host: str
    risk: RiskConfig
    taker_rate: Decimal
    maker_rate: Decimal
    top_n: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    min_depth_multiple: Decimal
    max_data_age_s: int


def load_config(path: Path | str = DEFAULT_PATH, env: dict | None = None, *,
                cli_armed: bool = False) -> Config:
    """``cli_armed`` is the CLI's --arm-live flag; live mode needs it (see assert_live_armed)."""
    env = os.environ if env is None else env
    raw = yaml.safe_load(Path(path).read_text())
    leaked = _secret_like_path(raw)
    if leaked:
        raise ConfigError(f"config key {leaked!r} looks like a secret; "
                          "secrets come only from environment variables")
    mode = raw["mode"]
    if mode not in ("mock", "shadow", "live"):
        raise ConfigError(f"invalid mode {mode!r}")
    if mode == "live" and env.get("WALLEX_ARM_LIVE") != "YES_I_UNDERSTAND":
        raise ConfigError("live mode requires WALLEX_ARM_LIVE=YES_I_UNDERSTAND")
    if mode == "live":
        try:
            assert_live_armed(env, cli_armed)
        except AuthError as exc:
            raise ConfigError(str(exc)) from exc
    host = raw["host"]
    if host not in ALLOWED_HOSTS:
        raise ConfigError(f"host {host!r} not in allow-list")
    r = {k: (Decimal(str(v)) if k != "max_open_positions" and k != "consecutive_loss_cooldown" else int(v))
         for k, v in raw["risk"].items()}
    u = raw["universe"]
    return Config(
        mode=mode, quote_currency=raw["quote_currency"], host=host, risk=RiskConfig(**r),
        taker_rate=Decimal(str(raw["fees"]["taker_rate"])), maker_rate=Decimal(str(raw["fees"]["maker_rate"])),
        top_n=int(u["top_n"]), max_spread_bps=Decimal(str(u["max_spread_bps"])),
        max_slippage_bps=Decimal(str(u["max_slippage_bps"])),
        min_depth_multiple=Decimal(str(u["min_depth_multiple"])), max_data_age_s=int(u["max_data_age_s"]),
    )
