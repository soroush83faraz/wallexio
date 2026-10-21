"""The optional advisor can only veto or shrink entries; any failure is an abstention."""
import time
from dataclasses import dataclass
from decimal import Decimal

import pytest
from test_bot_helpers import ENTRY_TS, SIGNAL_BAR, STEP, T0, WINNER, Rig, series

from wallexbot.bot.advice import AdvisorGate, parse_verdict
from wallexbot.bot.loop import BotParams
from wallexbot.types import Side

D = Decimal
FULL_QTY = D("9.7437")   # the un-advised entry size on this series (max_order_value cap)


class Advisor:
    def __init__(self, answer, delay=0.0):
        self.answer, self.delay, self.calls = answer, delay, []

    def review(self, context):
        self.calls.append(context)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@dataclass
class VerdictObj:
    decision: str
    size_multiplier: object = None
    reason: str = ""


def _entry(tmp_path, advisor, params=None):
    rig = Rig(tmp_path, series(WINNER), advisor=advisor, params=params)
    rig.run_until(ENTRY_TS + 60)
    ents = rig.intents(Side.BUY)
    verdicts = rig.events("advisor_verdict")
    rig.close()
    return ents, verdicts


def test_no_advisor_baseline(tmp_path):
    ents, verdicts = _entry(tmp_path, None)
    assert [r.intent.quantity for r in ents] == [FULL_QTY] and verdicts == []


def test_veto_blocks_the_entry_and_is_logged(tmp_path):
    adv = Advisor({"decision": "veto", "reason": "news risk"})
    ents, verdicts = _entry(tmp_path, adv)
    assert ents == []
    assert verdicts[-1]["payload"]["decision"] == "veto" and verdicts[-1]["payload"]["valid"] is True
    assert len(adv.calls) == 1
    ctx = adv.calls[0]
    assert ctx["symbol"] == "BTCUSDT" and ctx["signal_id"].startswith("sig_") and isinstance(ctx["quantity"], str)


@pytest.mark.parametrize("answer,qty", [
    ({"decision": "allow", "size_multiplier": 0.5}, D("4.8718")),
    ({"decision": "allow", "size_multiplier": "0.25"}, D("2.4359")),
    (VerdictObj("allow", D("0.5")), D("4.8718")),
    ({"decision": "allow"}, FULL_QTY),
    ({"decision": "abstain", "size_multiplier": 0.1}, FULL_QTY),     # abstain has no influence
])
def test_allow_can_only_shrink(tmp_path, answer, qty):
    ents, verdicts = _entry(tmp_path, Advisor(answer))
    assert [r.intent.quantity for r in ents] == [qty]
    assert verdicts[-1]["payload"]["valid"] is True


@pytest.mark.parametrize("answer", [
    {"decision": "allow", "size_multiplier": 2},       # would enlarge
    {"decision": "allow", "size_multiplier": 0},
    {"decision": "allow", "size_multiplier": -0.5},
    {"decision": "allow", "size_multiplier": "NaN"},
    {"decision": "allow", "size_multiplier": True},
    {"decision": "allow", "size_multiplier": [0.5]},
    {"decision": "maybe"},
    {"decision": None},
    "allow",
    None,
    42,
    object(),
    VerdictObj("VETO"),
    ValueError("model exploded"),
])
def test_garbage_or_failing_advisor_is_ignored(tmp_path, answer):
    ents, verdicts = _entry(tmp_path, Advisor(answer))
    assert [r.intent.quantity for r in ents] == [FULL_QTY]          # never blocks, never enlarges
    p = verdicts[-1]["payload"]
    assert p["decision"] == "abstain" and p["valid"] is False


def test_slow_advisor_times_out_and_is_ignored(tmp_path):
    adv = Advisor({"decision": "veto"}, delay=0.5)
    t0 = time.monotonic()
    ents, verdicts = _entry(tmp_path, adv, BotParams(advisor_timeout_s=0.05))
    assert time.monotonic() - t0 < 5
    assert [r.intent.quantity for r in ents] == [FULL_QTY]
    assert verdicts[-1]["payload"]["note"] == "advisor_timeout"


def test_busy_advisor_is_not_called_again():
    adv = Advisor({"decision": "veto"}, delay=0.4)
    gate = AdvisorGate(adv, timeout_s=0.02)
    assert gate.review({"a": 1}).note == "advisor_timeout"
    assert gate.review({"a": 2}).note == "advisor_busy"
    assert len(adv.calls) == 1
    time.sleep(0.5)
    adv.delay = 0                                              # recovered: answers in time again
    assert gate.review({"a": 3}).decision == "veto"


def test_advisor_never_consulted_for_exits(tmp_path):
    adv = Advisor({"decision": "allow", "size_multiplier": 1})
    rig = Rig(tmp_path, series(WINNER), advisor=adv)
    rig.run_until(T0 + (SIGNAL_BAR + 1 + len(WINNER)) * STEP + 60)
    assert len(adv.calls) == 1 and len(rig.intents(Side.SELL)) == 2
    rig.close()


def test_advisor_cannot_mutate_bot_state():
    seen = []

    class Mutator:
        def review(self, ctx):
            ctx["quantity"] = "1000000"
            seen.append(ctx)
            return {"decision": "allow"}

    gate = AdvisorGate(Mutator())
    ctx = {"quantity": D("1")}
    assert gate.review(ctx).decision == "allow"
    assert ctx == {"quantity": D("1")}


def test_parse_verdict_bounds():
    assert parse_verdict({"decision": "allow", "size_multiplier": "1"}).size_multiplier == 1
    assert parse_verdict({"decision": "allow", "size_multiplier": "1.0001"}).decision == "abstain"
    assert parse_verdict({"decision": "veto"}).vetoed
    assert not parse_verdict({"decision": "abstain"}).vetoed
