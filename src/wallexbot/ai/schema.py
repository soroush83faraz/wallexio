"""Advisor data contract: what the advisor may be told, and the only thing it may answer.

Input (``AdvisorContext``) is numeric/enum-only. No free text from market data, news or users ever
reaches the model (that is the prompt-injection channel): every string field must match
``^[A-Za-z0-9_.:-]{1,64}$`` and every number must be finite.

Output (``AdvisorVerdict``) is the advisor's whole power: ``allow`` / ``veto`` / ``abstain`` plus a
``size_multiplier`` in (0, 1]. There is no field through which it could name a price, a quantity,
a side, an order, a stop or a limit, so it cannot initiate or enlarge anything:
``effective_multiplier`` is 1 for abstain, ``size_multiplier`` for allow, 0 for veto, never above 1.

``parse_verdict(raw)`` is the only way model text becomes a verdict. It never raises. Unknown keys,
order-like keys, wrong types, bools, NaN/Infinity, duplicate keys, multipliers outside (0, 1],
confidence outside [0, 1], oversized payloads, unknown risk flags and imperative rationales all give
``abstain`` (with an ``outcome`` code saying why). The rationale is sanitised, stored for humans
and never executed or parsed further.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["ABSTAIN", "ALLOW", "VETO", "MAX_PAYLOAD_BYTES", "MAX_RATIONALE", "NAME_RE", "RISK_FLAGS",
           "AdvisorContext", "AdvisorSchemaError", "AdvisorVerdict", "abstain", "parse_verdict",
           "shrink_only"]

ALLOW, VETO, ABSTAIN = "allow", "veto", "abstain"
DECISIONS = frozenset({ALLOW, VETO, ABSTAIN})
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_PAYLOAD_BYTES = 2048        # a verdict is a handful of fields; anything bigger is hostile or broken
MAX_RATIONALE = 300
MAX_RISK_FLAGS = 8
MAX_RULES = 16
MAX_RETURNS = 10_000
RISK_FLAGS = frozenset({"wide_spread", "low_liquidity", "high_volatility", "regime_mismatch", "weak_trend",
                        "cost_margin_thin", "stale_data", "drawdown_risk", "correlation_risk", "other"})
VERDICT_KEYS = frozenset({"decision", "size_multiplier", "confidence", "rationale", "risk_flags"})
_ONE = Decimal(1)
_ZERO = Decimal(0)
_BOUND = Decimal(10) ** 12       # numeric context values are bounded: a huge number is a bug or an attack

# Keys that look like an attempt to steer an order. Anything outside VERDICT_KEYS is rejected anyway;
# this only classifies the rejection. Compared on lower-cased alphanumeric tokens of the key.
_ORDER_LIKE = re.compile(
    r"(?:price|px|qty|quantity|size(?!multiplier)|volume|notional|side|direction|action|orders?|"
    r"amount|stop|tp|sl|take|profit|limit|leverage|margin|symbol|pair|market|cancel|"
    r"buy|sell|long|short|reduce|exposure|position|kill|switch|secret|key|token|password)")
_KEY_TOKENS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

# Rationale policy: descriptive text only. Reject (do not repair) anything that reads as a command or
# as an attempt to talk to a downstream model or system.
_RATIONALE_OK = re.compile(r"[^A-Za-z0-9 .,;:!?'()%/+=_-]")
_FORBIDDEN_WORDS = re.compile(
    r"\b(?:buy|sell|short|place|submit|cancel|execute|liquidate|flatten|withdraw|transfer|leverage|"
    r"override|ignore|disregard|bypass|disable|reveal|forget|orders?|api|apikey|secret|password|token|"
    r"prompt|instructions?|system|assistant|json|exec|eval|sudo|http|https|www)\b")
_FORBIDDEN_SQUASHED = re.compile(r"(?:ignoreprevious|killswitch|apikey|systemprompt|previousinstructions|"
                                 r"disregardall|forgetall|jailbreak)")
_LEADING_IMPERATIVE = re.compile(
    r"^(?:please|you|set|change|update|open|close|enable|increase|raise|double|reduce|lower|use|make|go|"
    r"take|enter|exit|hold|wait|avoid|skip|do|don|never|always|must|should|now|immediately|add|remove|"
    r"delete|drop|run|call|send|print|output|reply|respond|answer|say|write|return|treat|assume|pretend|"
    r"act|follow|obey)\b")
_SENTENCE_SPLIT = re.compile(r"[.!?;:]+\s*")
_OUTCOME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class AdvisorSchemaError(ValueError):
    """Raised by constructors and the strict parse; ``code`` is a short, safe outcome code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- helpers
def _is_plain_str(value: object) -> bool:
    return type(value) is str


def _finite_decimal(value: object, code: str) -> Decimal:
    """An exact int/float/Decimal/str number as a finite Decimal; bool, NaN, Infinity rejected."""
    if type(value) is bool:
        raise AdvisorSchemaError(code)
    try:
        if type(value) is float:
            d = Decimal(repr(value))
        elif type(value) in (int, Decimal):
            d = Decimal(value)
        else:
            raise AdvisorSchemaError(code)
    except (InvalidOperation, ValueError):
        raise AdvisorSchemaError(code) from None
    if not d.is_finite():
        raise AdvisorSchemaError(code)
    return d


def _bounded(value: object, code: str) -> Decimal:
    d = _finite_decimal(value, code)
    if abs(d) > _BOUND:
        raise AdvisorSchemaError(code)
    return d


def _name(value: object, code: str) -> str:
    if not _is_plain_str(value) or not NAME_RE.fullmatch(value):
        raise AdvisorSchemaError(code)
    return value


def _clean_rationale(text: object) -> str:
    """Sanitise (strip everything outside a small safe alphabet, collapse whitespace) and then reject
    command-like text. Raises ``AdvisorSchemaError`` rather than silently repairing a command."""
    if not _is_plain_str(text) or len(text) > MAX_RATIONALE:
        raise AdvisorSchemaError("rationale_invalid")
    text = unicodedata.normalize("NFKC", text)
    text = _RATIONALE_OK.sub(" ", text)
    text = " ".join(text.split())
    low = text.lower()
    spaced = " ".join(re.sub(r"[^a-z0-9]+", " ", low).split())
    squashed = re.sub(r"[^a-z0-9]+", "", low)
    if _FORBIDDEN_WORDS.search(spaced) or _FORBIDDEN_SQUASHED.search(squashed) or "kill switch" in spaced:
        raise AdvisorSchemaError("rationale_imperative")
    for sentence in _SENTENCE_SPLIT.split(low):
        if _LEADING_IMPERATIVE.match(" ".join(re.sub(r"[^a-z0-9]+", " ", sentence).split())):
            raise AdvisorSchemaError("rationale_imperative")
    return text


# --------------------------------------------------------------------------- context
@dataclass(frozen=True)
class AdvisorContext:
    """Everything the advisor sees. Numbers and ``NAME_RE`` tokens only."""

    symbol: str
    score: Decimal                     # deterministic signal score, 0..1
    spread_bps: Decimal
    cost_margin_bps: Decimal           # expected edge minus round-trip cost, in bps
    regime: str                        # label such as "favourable" / "tightened"
    rules: Mapping[str, str] = field(default_factory=dict)   # rule name -> short value token
    ret_mean_bps: Decimal = _ZERO      # summary of recent bar returns
    ret_std_bps: Decimal = _ZERO
    ret_n: int = 0

    def __post_init__(self) -> None:
        _name(self.symbol, "ctx_symbol")
        _name(self.regime, "ctx_regime")
        score = _bounded(self.score, "ctx_score")
        if not _ZERO <= score <= _ONE:
            raise AdvisorSchemaError("ctx_score")
        _bounded(self.spread_bps, "ctx_spread")
        _bounded(self.cost_margin_bps, "ctx_cost_margin")
        _bounded(self.ret_mean_bps, "ctx_ret_mean")
        if _bounded(self.ret_std_bps, "ctx_ret_std") < _ZERO:
            raise AdvisorSchemaError("ctx_ret_std")
        if type(self.ret_n) is not int or not 0 <= self.ret_n <= MAX_RETURNS:
            raise AdvisorSchemaError("ctx_ret_n")
        if not isinstance(self.rules, Mapping) or len(self.rules) > MAX_RULES:
            raise AdvisorSchemaError("ctx_rules")
        for k, v in self.rules.items():
            _name(k, "ctx_rule_name")
            _name(v, "ctx_rule_value")

    @classmethod
    def from_signal(cls, signal: Any, *, spread_bps: Any, cost_margin_bps: Any, regime: str,
                    returns_bps: Sequence[Any] = ()) -> AdvisorContext:
        """Build a context from a strategy ``Signal`` (read-only duck typing) plus numeric features.

        The signal's ``reasons`` are free-form strategy prose; they are NOT forwarded. Each rule is
        reduced to its first number (or first safe token), or ``na``. Rule names that are not safe
        tokens are dropped.
        """
        rules: dict[str, str] = {}
        for name, text in dict(signal.reasons).items():
            if len(rules) >= MAX_RULES or not _is_plain_str(name) or not NAME_RE.fullmatch(name):
                continue
            rules[name] = _reduce_reason(text)
        rets = [_bounded(r, "ctx_returns") for r in list(returns_bps)[-MAX_RETURNS:]]
        n = len(rets)
        mean = sum(rets, _ZERO) / n if n else _ZERO
        var = sum(((r - mean) ** 2 for r in rets), _ZERO) / (n - 1) if n > 1 else _ZERO
        return cls(symbol=signal.symbol, score=Decimal(signal.score), spread_bps=_bounded(spread_bps, "ctx_spread"),
                   cost_margin_bps=_bounded(cost_margin_bps, "ctx_cost_margin"), regime=regime, rules=rules,
                   ret_mean_bps=mean.quantize(Decimal("0.0001")), ret_std_bps=var.sqrt().quantize(Decimal("0.0001")),
                   ret_n=n)

    def to_prompt_dict(self) -> dict[str, Any]:
        """JSON-able, deterministic view of the context; decimals as strings."""
        return {"symbol": self.symbol, "score": str(self.score), "spread_bps": str(self.spread_bps),
                "cost_margin_bps": str(self.cost_margin_bps), "regime": self.regime,
                "rules": dict(sorted(self.rules.items())), "ret_mean_bps": str(self.ret_mean_bps),
                "ret_std_bps": str(self.ret_std_bps), "ret_n": self.ret_n}


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _reduce_reason(text: object) -> str:
    if not _is_plain_str(text):
        return "na"
    m = _NUMBER.search(text[:200])
    if m and NAME_RE.fullmatch(m[0]):
        return m[0]
    first = text.strip().split(" ", 1)[0].rstrip(":,;") if text.strip() else ""
    return first if NAME_RE.fullmatch(first) else "na"


# --------------------------------------------------------------------------- verdict
@dataclass(frozen=True)
class AdvisorVerdict:
    """The advisor's entire output. Build with ``create`` / ``abstain``; ``parse_verdict`` for model text.

    ``outcome`` is set by the advisor code (never by the model): ``ok`` or a short failure code.
    """

    decision: str
    size_multiplier: Decimal = _ONE
    confidence: Decimal = _ZERO
    rationale: str = ""
    risk_flags: tuple[str, ...] = ()
    outcome: str = "ok"

    def __post_init__(self) -> None:
        if not _is_plain_str(self.decision) or self.decision not in DECISIONS:
            raise AdvisorSchemaError("decision_invalid")
        if type(self.size_multiplier) is not Decimal or type(self.confidence) is not Decimal:
            raise AdvisorSchemaError("number_type_invalid")     # stored values are always Decimal
        m = _finite_decimal(self.size_multiplier, "multiplier_invalid")
        if not _ZERO < m <= _ONE:
            raise AdvisorSchemaError("multiplier_out_of_range")
        c = _finite_decimal(self.confidence, "confidence_invalid")
        if not _ZERO <= c <= _ONE:
            raise AdvisorSchemaError("confidence_out_of_range")
        if _clean_rationale(self.rationale) != self.rationale:
            raise AdvisorSchemaError("rationale_unclean")
        if type(self.risk_flags) is not tuple or len(self.risk_flags) > MAX_RISK_FLAGS or \
                any(not _is_plain_str(f) or f not in RISK_FLAGS for f in self.risk_flags):
            raise AdvisorSchemaError("risk_flags_invalid")
        _name(self.outcome, "outcome_invalid")

    @classmethod
    def create(cls, decision: str, *, size_multiplier: Any = _ONE, confidence: Any = _ZERO, rationale: str = "",
               risk_flags: Sequence[str] = (), outcome: str = "ok") -> AdvisorVerdict:
        flags = tuple(dict.fromkeys(risk_flags))
        return cls(decision, _finite_decimal(size_multiplier, "multiplier_invalid"),
                   _finite_decimal(confidence, "confidence_invalid"), _clean_rationale(rationale), flags, outcome)

    @property
    def effective_multiplier(self) -> Decimal:
        """Factor the caller may apply to an already-computed size: 1 abstain, m allow, 0 veto. Never > 1."""
        if self.decision == VETO:
            return _ZERO
        if self.decision == ALLOW:
            return self.size_multiplier
        return _ONE

    def to_record(self) -> dict[str, Any]:
        return {"decision": self.decision, "size_multiplier": str(self.size_multiplier),
                "confidence": str(self.confidence), "rationale": self.rationale,
                "risk_flags": list(self.risk_flags), "outcome": self.outcome}


def abstain(outcome: str = "abstain") -> AdvisorVerdict:
    """The neutral verdict: the deterministic path proceeds unchanged."""
    if not _is_plain_str(outcome) or not _OUTCOME_RE.fullmatch(outcome):
        outcome = "abstain"
    return AdvisorVerdict(ABSTAIN, _ONE, _ZERO, "", (), outcome)


def shrink_only(quantity: Decimal, verdict: AdvisorVerdict) -> Decimal:
    """Scale an already-approved size by the verdict; the result is never larger than ``quantity``."""
    if not isinstance(verdict, AdvisorVerdict):
        return quantity
    return min(quantity, quantity * min(_ONE, max(_ZERO, verdict.effective_multiplier)))


# --------------------------------------------------------------------------- strict parse
def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise AdvisorSchemaError("duplicate_key")
        out[k] = v
    return out


def _reject_constant(_name_: str) -> Any:
    raise AdvisorSchemaError("non_finite_number")


def _key_is_order_like(key: str) -> bool:
    return any(_ORDER_LIKE.fullmatch(t.lower()) for t in _KEY_TOKENS.findall(key))


def _parse_strict(raw: object) -> AdvisorVerdict:
    if type(raw) is bytes:
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise AdvisorSchemaError("payload_too_large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AdvisorSchemaError("not_utf8") from None
    if not _is_plain_str(raw):
        raise AdvisorSchemaError("not_text")
    if len(raw) > MAX_PAYLOAD_BYTES or len(raw.encode("utf-8", "replace")) > MAX_PAYLOAD_BYTES:
        raise AdvisorSchemaError("payload_too_large")
    try:
        obj = json.loads(raw.strip(), parse_float=Decimal, parse_constant=_reject_constant,
                         object_pairs_hook=_no_duplicates)
    except AdvisorSchemaError:
        raise
    except (ValueError, RecursionError):
        raise AdvisorSchemaError("bad_json") from None
    if type(obj) is not dict:
        raise AdvisorSchemaError("not_object")
    extra = [k for k in obj if k not in VERDICT_KEYS]
    if extra:
        raise AdvisorSchemaError("forbidden_key" if any(_key_is_order_like(k) for k in extra) else "unknown_key")
    if "decision" not in obj or "confidence" not in obj:
        raise AdvisorSchemaError("missing_key")
    decision = obj["decision"]
    if not _is_plain_str(decision) or decision not in DECISIONS:
        raise AdvisorSchemaError("decision_invalid")
    multiplier = _finite_decimal(obj.get("size_multiplier", 1), "multiplier_invalid")
    if not _ZERO < multiplier <= _ONE:
        raise AdvisorSchemaError("multiplier_out_of_range")
    confidence = _finite_decimal(obj["confidence"], "confidence_invalid")
    if not _ZERO <= confidence <= _ONE:
        raise AdvisorSchemaError("confidence_out_of_range")
    flags = obj.get("risk_flags", [])
    if type(flags) is not list or len(flags) > MAX_RISK_FLAGS or \
            any(not _is_plain_str(f) or f not in RISK_FLAGS for f in flags):
        raise AdvisorSchemaError("risk_flags_invalid")
    rationale = _clean_rationale(obj.get("rationale", ""))
    return AdvisorVerdict(decision, multiplier, confidence, rationale, tuple(dict.fromkeys(flags)), "ok")


def parse_verdict(raw: object) -> AdvisorVerdict:
    """Strictly parse model text into a verdict. Never raises; any problem yields ``abstain`` whose
    ``outcome`` is ``rejected:<code>``."""
    try:
        return _parse_strict(raw)
    except AdvisorSchemaError as exc:
        return abstain("rejected:" + exc.code)
    except Exception:
        return abstain("rejected:internal")
