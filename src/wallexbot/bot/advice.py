"""Optional AI advisor gate for ENTRIES. The advisor can only veto an entry or shrink its size.

The advisor is any object with `review(context: dict) -> verdict`, where the verdict is a mapping
or an object carrying `decision` in {"allow", "veto", "abstain"} and `size_multiplier` in (0, 1]
(None means 1). Anything else (a raise, a malformed verdict, a multiplier outside (0, 1], an
answer slower than `timeout_s`, or a previous call still running) is treated as ABSTAIN: no
influence at all. It is never consulted for exits, never sees bot objects (only a plain-JSON copy
of the context) and is never imported by risk/, oms.py or exchange/.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from wallexbot.redact import redact

DECISIONS = frozenset({"allow", "veto", "abstain"})
_ONE = Decimal(1)


@dataclass(frozen=True)
class Verdict:
    decision: str                   # allow | veto | abstain (abstain also for every failure)
    size_multiplier: Decimal        # applied only when decision == "allow"; always in (0, 1]
    valid: bool                     # False: the advisor failed and its answer was ignored
    note: str = ""
    latency_s: float = 0.0
    raw: Any = None                 # the advisor's own answer (redacted), for the audit row

    @property
    def vetoed(self) -> bool:
        return self.decision == "veto"

    def audit(self) -> dict:
        return {"decision": self.decision, "size_multiplier": str(self.size_multiplier), "valid": self.valid,
                "note": self.note, "latency_s": round(self.latency_s, 4), "raw": self.raw}


def abstain(note: str, latency_s: float = 0.0, raw: Any = None) -> Verdict:
    return Verdict("abstain", _ONE, False, note, latency_s, raw)


def _field(v: Any, name: str) -> Any:
    """A verdict field from a mapping or an object (plain attribute access, no dynamic lookup)."""
    if isinstance(v, Mapping):
        return v.get(name)
    try:
        if name == "decision":
            return v.decision
        if name == "size_multiplier":
            return v.size_multiplier
        return v.reason
    except Exception:
        return None


def parse_verdict(v: Any, latency_s: float = 0.0) -> Verdict:
    raw = redact(v if isinstance(v, (Mapping, str, int, float, type(None))) else repr(v))
    decision = _field(v, "decision")
    if not isinstance(decision, str) or decision not in DECISIONS:
        return abstain("invalid_decision", latency_s, raw)
    note = _field(v, "reason")
    note = str(redact(note))[:200] if isinstance(note, str) else ""
    if decision != "allow":
        return Verdict(decision, _ONE, True, note, latency_s, raw)
    m = _field(v, "size_multiplier")
    if m is None:
        return Verdict("allow", _ONE, True, note, latency_s, raw)
    if isinstance(m, bool) or not isinstance(m, (int, float, str, Decimal)):
        return abstain("invalid_size_multiplier", latency_s, raw)
    try:
        dm = Decimal(str(m))
    except (InvalidOperation, ValueError):
        return abstain("invalid_size_multiplier", latency_s, raw)
    if not dm.is_finite() or not (0 < dm <= 1):
        return abstain("size_multiplier_out_of_range", latency_s, raw)
    return Verdict("allow", dm, True, note, latency_s, raw)


class AdvisorGate:
    """Calls the advisor in a daemon thread with a deadline. A hung advisor leaks at most one
    thread: while it is still running every later call abstains ("advisor_busy")."""

    def __init__(self, advisor: Any, timeout_s: float = 2.0) -> None:
        self.advisor = advisor
        self.timeout_s = float(timeout_s)
        self._busy: threading.Thread | None = None

    def review(self, context: Mapping[str, Any]) -> Verdict:
        if self.advisor is None:
            return abstain("no_advisor")
        if self._busy is not None and self._busy.is_alive():
            return abstain("advisor_busy")
        try:
            ctx = json.loads(json.dumps(redact(dict(context)), default=str))  # plain copy, no live objects
        except Exception as e:
            return abstain(f"context_error:{type(e).__name__}")
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["v"] = self.advisor.review(ctx)
            except Exception as e:  # noqa: BLE001 - any advisor failure is an abstention
                box["e"] = e

        t0 = time.monotonic()
        t = threading.Thread(target=run, name="wallexbot-advisor", daemon=True)
        t.start()
        t.join(self.timeout_s)
        latency = time.monotonic() - t0
        if t.is_alive():
            self._busy = t
            return abstain("advisor_timeout", latency)
        if "e" in box:
            return abstain(f"advisor_error:{type(box['e']).__name__}", latency)
        if latency > self.timeout_s:
            return abstain("advisor_timeout", latency)
        return parse_verdict(box.get("v"), latency)
