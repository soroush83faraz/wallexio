"""Offline check of whether vetoes helped: compare hypothetical net R of vetoed vs allowed signals.

    result = compare_outcomes([{"decision": "veto", "net_r": -0.8}, {"decision": "allow", "net_r": 1.2}, ...])
    result["conclusion"]   # "insufficient_data" | "no_evidence_of_benefit" | "evidence_of_benefit"

``net_r`` is what the deterministic trade WOULD have returned (net of costs, in R multiples),
counterfactual for vetoed signals. Pre-registered rule (plan section 6): no claim of any kind below
``MIN_VETOED = 30`` vetoed samples. Above it, "evidence_of_benefit" needs the vetoed trades to have
a lower mean than the allowed ones by a one-sided Welch z of at least ``Z_CRIT`` (1.645); anything
else is "no_evidence_of_benefit". Pure stdlib, no I/O; this is a research helper and is never called
on the trading path.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["MIN_VETOED", "Z_CRIT", "compare_outcomes"]

MIN_VETOED = 30
Z_CRIT = 1.645
_DECISIONS = ("veto", "allow", "abstain")


def _decision(rec: Mapping[str, Any]) -> str | None:
    v = rec.get("decision", rec.get("verdict"))
    if isinstance(v, Mapping):
        v = v.get("decision")
    return v if isinstance(v, str) and v in _DECISIONS else None


def _net_r(rec: Mapping[str, Any]) -> float | None:
    v = rec.get("net_r")
    if type(v) is bool or not isinstance(v, (int, float, Decimal, str)):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return float(d) if d.is_finite() and abs(d) < Decimal(10) ** 9 else None


def _mean_var(xs: list[float]) -> tuple[float | None, float | None]:
    n = len(xs)
    if n == 0:
        return None, None
    mean = math.fsum(xs) / n
    var = math.fsum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else None
    return mean, var


def compare_outcomes(records: Iterable[Mapping[str, Any]], *, min_vetoed: int = MIN_VETOED) -> dict[str, Any]:
    """Summarise records ``{decision|verdict, net_r}``; see the module docstring for the rule."""
    groups: dict[str, list[float]] = {d: [] for d in _DECISIONS}
    invalid = 0
    for rec in records:
        decision, r = (_decision(rec), _net_r(rec)) if isinstance(rec, Mapping) else (None, None)
        if decision is None or r is None:
            invalid += 1
            continue
        groups[decision].append(r)
    n_v, n_a = len(groups["veto"]), len(groups["allow"])
    mean_v, var_v = _mean_var(groups["veto"])
    mean_a, var_a = _mean_var(groups["allow"])
    mean_abs, _ = _mean_var(groups["abstain"])
    z: float | None = None
    if n_v >= 2 and n_a >= 2 and var_v is not None and var_a is not None:
        se = math.sqrt(var_a / n_a + var_v / n_v)
        if se > 0:
            z = (mean_a - mean_v) / se          # type: ignore[operator]
    if n_v < max(min_vetoed, MIN_VETOED) or n_a < 2:
        conclusion = "insufficient_data"
    elif z is not None and z >= Z_CRIT:
        conclusion = "evidence_of_benefit"
    else:
        conclusion = "no_evidence_of_benefit"
    return {"n_vetoed": n_v, "n_allowed": n_a, "n_abstain": len(groups["abstain"]), "n_invalid": invalid,
            "mean_net_r_vetoed": mean_v, "mean_net_r_allowed": mean_a, "mean_net_r_abstain": mean_abs,
            "mean_diff_allowed_minus_vetoed": None if mean_v is None or mean_a is None else mean_a - mean_v,
            "welch_z": z, "min_vetoed": max(min_vetoed, MIN_VETOED), "conclusion": conclusion}
