import json
from dataclasses import replace
from decimal import Decimal

import pytest

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import run_backtest
from wallexbot.backtest.metrics import compute_metrics
from wallexbot.backtest.report import build_report, render_markdown, write_report
from wallexbot.config import RiskConfig
from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import Signal
from wallexbot.strategy.exits import ExitParams, FixedRR
from wallexbot.types import SymbolRules

D = Decimal
A = "AAAUSDT"


class OneSignal:
    def evaluate(self, symbol, bars, market):
        last = bars[-1]
        if last.ts == 900:
            return Signal(symbol, last.ts, last.close, last.close, D(1), D(90), D("0.9"))
        return None


def _result():
    bars = [Candle(i * 900, D(p), D(p) + 1, D(p) - 1, D(p), D(1000)) for i, p in enumerate([100, 100, 101, 104, 130, 131])]
    rk = RiskConfig(D(1), D(25), D(60), 3, D(1000), D(4), D(6), D(8), D(12), 3, D(5))
    return run_backtest({A: bars}, OneSignal(), FixedRR(), CostModel(), rk,
                        {A: SymbolRules(A, "AAA", "USDT", 4, 4)}, D(10000))


def test_report_json_and_markdown(tmp_path):
    res = _result()
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "take_profit"
    rep = build_report(res, title="E1 smoke", parameters=ExitParams(), split="train")
    assert rep["inputs_hash"] == res.inputs_hash and rep["result_hash"] == res.result_hash
    assert rep["metrics"] == compute_metrics(res)
    assert rep["parameters"]["tp_r"] == "1.5"
    assert rep["git_sha"] is None or len(rep["git_sha"]) >= 40
    jp, mp = write_report(rep, tmp_path, "e1")
    assert json.loads(jp.read_text())["result_hash"] == res.result_hash
    md = mp.read_text()
    assert md == render_markdown(rep)
    assert res.inputs_hash in md and "## Limitations" in md and "placeholders" in md
    assert "| trade_count | 1 |" in md


def test_report_refuses_tampered_result_and_missing_limitations():
    res = _result()
    with pytest.raises(ValueError, match="result_hash"):
        build_report(replace(res, final_equity=res.final_equity * 2), title="x")
    with pytest.raises(ValueError, match="limitations"):
        build_report(res, title="x", limitations="  ")
