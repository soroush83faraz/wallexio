"""Glue between the bot's advisor gate and the ai/ package (kept OUT of bot/, which must not import ai/).

The bot hands its advisor a plain JSON dict (bot/advice.py); ai.advisor wants an AdvisorContext (numbers and
safe tokens only). `BotAdvisorBridge` converts one into the other and returns the dict shape the bot parses.
`AdvisorRecorder` persists LoggedAdvisor.drain() records as append-only audit events (actor "advisor",
kind "verdict"). Nothing here touches orders: the advisor stays shadow-only (apply=False) and can never
reach risk/, oms.py or exchange/.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from wallexbot.ai.advisor import Advisor, LoggedAdvisor, NullAdvisor, make_advisor
from wallexbot.ai.schema import AdvisorContext
from wallexbot.redact import load_secret
from wallexbot.store import Store

log = logging.getLogger("wallexbot.advisor")
KEY_ENV = "ANTHROPIC_API_KEY"
_COST_GATE = re.compile(r"expected_move\s+(-?[\d.]+)bps\s*>=\s*[\d.]+\*round_trip_cost\s+(-?[\d.]+)bps")
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def _dec(v: Any, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else Decimal(default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def to_context(d: Mapping[str, Any]) -> AdvisorContext:
    """AdvisorContext from the bot's entry context. spread_bps comes from bid/ask; cost_margin_bps is
    (expected move - cost threshold) parsed from the strategy's cost_gate reason, 0 if unparsable."""
    bid, ask = _dec(d.get("bid")), _dec(d.get("ask"))
    mid = (bid + ask) / 2
    spread = ((ask - bid) / mid * 10_000).quantize(Decimal("0.01")) if mid > 0 and ask >= bid else Decimal(0)
    reasons = {str(k): str(v) for k, v in dict(d.get("reasons") or {}).items()}
    m = _COST_GATE.search(reasons.get("cost_gate", ""))
    margin = (_dec(m[1]) - _dec(m[2])).quantize(Decimal("0.1")) if m else Decimal(0)
    regime = reasons.get("regime", "").split(":", 1)[0].strip()
    sig = SimpleNamespace(symbol=str(d["symbol"]), score=_dec(d.get("score")), reasons=reasons)
    return AdvisorContext.from_signal(sig, spread_bps=spread, cost_margin_bps=margin,
                                      regime=regime if _TOKEN.match(regime) else "unknown")


class BotAdvisorBridge:
    def __init__(self, advisor: Advisor) -> None:
        self.advisor = advisor

    def review(self, ctx: Mapping[str, Any]) -> dict[str, Any]:
        try:
            actx = to_context(ctx)
        except Exception as e:  # noqa: BLE001 - an unusable context is an abstention
            return {"decision": "abstain", "reason": f"context_unavailable:{type(e).__name__}"}
        v = self.advisor.review(actx)
        return {"decision": v.decision, "size_multiplier": str(v.size_multiplier), "reason": v.rationale}

    def drain(self) -> list[dict[str, Any]]:
        return self.advisor.drain() if isinstance(self.advisor, LoggedAdvisor) else []


class AdvisorRecorder:
    """Persists drained verdict records into the audit database on its own connection (main thread only)."""

    def __init__(self, bridge: BotAdvisorBridge, db: str | Path) -> None:
        self.bridge, self.db, self.written = bridge, db, 0
        self._store: Store | None = None

    def __call__(self, _report: Any = None) -> None:
        recs = self.bridge.drain()
        if not recs:
            return
        if self._store is None:
            self._store = Store(self.db)
        for r in recs:
            self._store.append_audit_event("advisor", "verdict", r, ts=float(r.get("ts") or 0) or None)
            self.written += 1

    def close(self) -> None:
        self()
        if self._store is not None:
            self._store.close()
            self._store = None


def build_advisor(kind: str, db: str | Path, *, env: Mapping[str, str] | None = None,
                  warn: Callable[[str], Any] | None = None,
                  **advisor_kwargs: Any) -> tuple[BotAdvisorBridge | None, AdvisorRecorder | None]:
    """kind 'off': (None, None) - the bot runs without an advisor gate. 'shadow': ANTHROPIC_API_KEY is read
    (only now) via redact.load_secret; no key -> warn and use NullAdvisor. Always apply=False."""
    if kind == "off":
        return None, None
    if kind != "shadow":
        raise ValueError(f"unknown advisor mode {kind!r}")
    key = load_secret(KEY_ENV, env)
    if key is None:
        (warn or log.warning)(f"--advisor shadow: {KEY_ENV} is not set; running with NullAdvisor (no AI calls)")
        advisor: Advisor = NullAdvisor()
    else:
        advisor = make_advisor(enabled=True, apply=False, api_key=key, **advisor_kwargs)
    bridge = BotAdvisorBridge(advisor)
    return bridge, (AdvisorRecorder(bridge, db) if str(db) != ":memory:" else None)
