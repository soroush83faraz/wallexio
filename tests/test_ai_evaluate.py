"""compare_outcomes: the pre-registered n_vetoed >= 30 rule and the three conclusions."""
from __future__ import annotations

import random
from decimal import Decimal

from wallexbot.ai.evaluate import MIN_VETOED, compare_outcomes


def recs(decision: str, values) -> list[dict]:
    return [{"decision": decision, "net_r": v} for v in values]


def noisy(rnd: random.Random, mean: float, n: int, sd: float = 1.0) -> list[float]:
    return [rnd.gauss(mean, sd) for _ in range(n)]


def test_means_and_counts():
    out = compare_outcomes(recs("veto", [-1, -2]) + recs("allow", [1, 3]) + recs("abstain", [0.5])
                           + [{"decision": "bogus", "net_r": 1}, {"decision": "allow", "net_r": float("nan")},
                              {"decision": "allow"}, "junk", {"decision": "veto", "net_r": True}])
    assert (out["n_vetoed"], out["n_allowed"], out["n_abstain"], out["n_invalid"]) == (2, 2, 1, 5)
    assert out["mean_net_r_vetoed"] == -1.5 and out["mean_net_r_allowed"] == 2.0
    assert out["mean_net_r_abstain"] == 0.5 and out["mean_diff_allowed_minus_vetoed"] == 3.5


def test_refuses_to_claim_below_30_vetoes_whatever_the_effect():
    rnd = random.Random(1)
    for n_veto in (0, 1, 10, 29):
        out = compare_outcomes(recs("veto", [-5.0] * n_veto) + recs("allow", noisy(rnd, 1.0, 500)))
        assert out["conclusion"] == "insufficient_data" and out["n_vetoed"] == n_veto
    # lowering min_vetoed cannot weaken the pre-registered floor
    assert compare_outcomes(recs("veto", [-5.0] * 10) + recs("allow", [1.0, 2.0, 3.0]),
                            min_vetoed=1)["conclusion"] == "insufficient_data"


def test_needs_allowed_samples_too():
    assert compare_outcomes(recs("veto", noisy(random.Random(2), -2, 40)))["conclusion"] == "insufficient_data"
    assert compare_outcomes(recs("veto", noisy(random.Random(2), -2, 40)) + recs("allow", [1.0]))[
        "conclusion"] == "insufficient_data"


def test_empty_and_garbage_input():
    out = compare_outcomes([])
    assert out["conclusion"] == "insufficient_data" and out["mean_net_r_vetoed"] is None and out["welch_z"] is None
    assert compare_outcomes([None, 5, "x"])["n_invalid"] == 3                      # type: ignore[list-item]


def test_evidence_of_benefit_when_vetoes_avoid_clearly_worse_trades():
    rnd = random.Random(3)
    out = compare_outcomes(recs("veto", noisy(rnd, -0.8, MIN_VETOED + 20)) + recs("allow", noisy(rnd, 0.3, 200)))
    assert out["conclusion"] == "evidence_of_benefit" and out["welch_z"] > 1.645


def test_no_evidence_when_vetoes_were_as_good_as_the_rest():
    rnd = random.Random(4)
    out = compare_outcomes(recs("veto", noisy(rnd, 0.3, 60)) + recs("allow", noisy(rnd, 0.3, 200)))
    assert out["conclusion"] == "no_evidence_of_benefit"


def test_no_evidence_when_vetoes_removed_winners():
    rnd = random.Random(5)
    out = compare_outcomes(recs("veto", noisy(rnd, 1.5, 60)) + recs("allow", noisy(rnd, 0.0, 200)))
    assert out["conclusion"] == "no_evidence_of_benefit" and out["welch_z"] < 0


def test_zero_variance_is_not_a_claim():
    out = compare_outcomes(recs("veto", [-1.0] * 40) + recs("allow", [1.0] * 40))
    assert out["welch_z"] is None and out["conclusion"] == "no_evidence_of_benefit"


def test_accepts_verdict_dicts_decimals_and_strings():
    rows = [{"verdict": {"decision": "veto", "size_multiplier": "1"}, "net_r": Decimal("-1.5")} for _ in range(30)]
    rows += [{"verdict": "allow", "net_r": "0.5"} for _ in range(30)]
    out = compare_outcomes(rows)
    assert out["n_vetoed"] == 30 and out["n_allowed"] == 30 and out["mean_net_r_allowed"] == 0.5
    assert out["conclusion"] in {"no_evidence_of_benefit", "evidence_of_benefit"}


def test_conclusion_is_always_one_of_three_and_result_is_plain():
    rnd = random.Random(6)
    for n in (0, 5, 30, 100):
        out = compare_outcomes(recs("veto", noisy(rnd, -0.2, n)) + recs("allow", noisy(rnd, 0.1, 80)))
        assert out["conclusion"] in {"insufficient_data", "no_evidence_of_benefit", "evidence_of_benefit"}
        assert all(type(v) in (int, float, str, type(None)) for v in out.values())
