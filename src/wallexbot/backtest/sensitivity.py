"""One-at-a-time +/-pct sensitivity of net expectancy, on train or validation data ONLY.

For every numeric field of a frozen parameter dataclass (int, Decimal, float; bool excluded) two
neighbours are built: value*(1-pct) and value*(1+pct). Ints are floored/ceiled so that the
neighbour differs from the base (so an int step may exceed pct), clamped to >= 1 when the base is
>= 1; a neighbour equal to the base (e.g. a zero value) is skipped. `run_fn(params)` must return
the net expectancy per trade (quote or R) or None when there were no trades.

Reported: every neighbour's expectancy and the fraction of neighbours whose expectancy has the
same (non-zero) sign as the base. None counts as "not the same sign". If the base itself is None
or zero, the fraction is None: there is no edge whose robustness could be measured.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Sequence

ALLOWED_SPLITS = ("train", "validation")


@dataclass(frozen=True)
class Neighbour:
    param: str
    direction: str       # "-" | "+"
    value: Any
    expectancy: Any      # Decimal | float | None
    same_sign: bool


@dataclass(frozen=True)
class SensitivityResult:
    split: str
    pct: Decimal
    base_expectancy: Any
    neighbours: tuple[Neighbour, ...]
    fraction_same_sign: float | None


def _sign(x) -> int:
    if x is None:
        return 0
    return (x > 0) - (x < 0)


def _neighbours(value, pct: Decimal):
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        lo, hi = math.floor(value * (1 - pct)), math.ceil(value * (1 + pct))
        if value >= 1:
            lo = max(lo, 1)
        return [("-", lo), ("+", hi)]
    if isinstance(value, Decimal):
        return [("-", value * (1 - pct)), ("+", value * (1 + pct))]
    if isinstance(value, float):
        return [("-", value * (1 - float(pct))), ("+", value * (1 + float(pct)))]
    return []


def run_sensitivity(run_fn: Callable[[Any], Any], base_params: Any, *, split: str,
                    pct: Decimal = Decimal("0.2"), only: Sequence[str] | None = None) -> SensitivityResult:
    if split not in ALLOWED_SPLITS:
        raise ValueError(f"sensitivity runs on {ALLOWED_SPLITS} only, never on {split!r}")
    base = run_fn(base_params)
    base_sign = _sign(base)
    rows = []
    for name, v in asdict(base_params).items():   # field order; values are copies
        if only is not None and name not in only:
            continue
        for direction, nv in _neighbours(v, pct):
            if nv == v:
                continue
            e = run_fn(replace(base_params, **{name: nv}))
            rows.append(Neighbour(name, direction, nv, e, base_sign != 0 and _sign(e) == base_sign))
    frac = None
    if base_sign != 0 and rows:
        frac = sum(r.same_sign for r in rows) / len(rows)
    return SensitivityResult(split, pct, base, tuple(rows), frac)
