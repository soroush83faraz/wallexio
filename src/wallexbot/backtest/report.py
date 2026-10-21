"""JSON + Markdown backtest report.

The report never carries numbers it did not compute: metrics are recomputed here from the
BacktestResult (callers cannot pass metric values in), and the result's hash is re-verified so
an edited result object is refused. A missing value is rendered as "n/a", never invented.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from wallexbot.backtest.engine import BacktestResult, Trade, to_canonical
from wallexbot.backtest.metrics import compute_metrics

DEFAULT_LIMITATIONS = (
    "Simulation, not evidence of future profit. Fees (0.35% taker), minimum notional and spreads are "
    "placeholders not verified against Wallex; fees are charged in quote although Wallex may charge "
    "the received asset. Fills are next-bar-open or level-touch taker fills with a half-spread and a "
    "linear impact proxy from the previous bar's base volume times close (volume semantics "
    "unverified); queue position, partial fills, rejections, latency and order-book depth are not "
    "modelled. Within a bar only OHLC is known: a bar touching both stop and target is booked as a "
    "stop. Equity is marked at closes without liquidation cost; loss limits are checked at closes "
    "only. Positions open at the end of the data are sold at the last close and flagged. The universe "
    "and the data period may carry selection bias; a short sample does not generalise."
)


def git_sha(repo_dir: Path | str | None = None) -> str | None:
    cwd = Path(repo_dir) if repo_dir else Path(__file__).resolve().parent
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True,
                               timeout=5).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def build_report(result: BacktestResult, *, title: str, parameters: Any = None,
                 limitations: str = DEFAULT_LIMITATIONS, regime_of: Callable[[Trade], str] | None = None,
                 split: str | None = None, repo_dir: Path | str | None = None) -> dict:
    if result.compute_result_hash() != result.result_hash:
        raise ValueError("result_hash does not match the result contents; refusing to report")
    if not limitations.strip():
        raise ValueError("a limitations paragraph is required")
    params = to_canonical(parameters) if (is_dataclass(parameters) or isinstance(parameters, Mapping)) else parameters
    return {
        "title": title,
        "split": split,
        "inputs_hash": result.inputs_hash,
        "result_hash": result.result_hash,
        "git_sha": git_sha(repo_dir),
        "stats_inputs": to_canonical(dict(result.stats_inputs)),
        "parameters": params,
        "metrics": compute_metrics(result, regime_of=regime_of),
        "limitations": limitations,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def render_markdown(report: Mapping[str, Any]) -> str:
    m = report["metrics"]
    lines = [f"# {report['title']}", "",
             f"- split: {_fmt(report.get('split'))}",
             f"- inputs hash: `{report['inputs_hash']}`",
             f"- result hash: `{report['result_hash']}`",
             f"- git SHA: {_fmt(report.get('git_sha'))}",
             f"- symbols: {', '.join(report['stats_inputs'].get('symbols', []))}",
             f"- bars: {_fmt(report['stats_inputs'].get('n_bars'))} x {_fmt(m.get('bar_interval_s'))}s", "",
             "## Metrics", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in m.items() if not isinstance(v, dict)]
    if "by_regime" in m:
        lines += ["", "## By regime", "", "| regime | trades | win rate | expectancy (quote) | expectancy (R) |",
                  "|---|---|---|---|---|"]
        lines += [f"| {k} | {v['trade_count']} | {_fmt(v['win_rate'])} | {_fmt(v['expectancy_quote'])} | "
                  f"{_fmt(v['expectancy_r'])} |" for k, v in m["by_regime"].items()]
    lines += ["", "## Parameters", "", "```json", json.dumps(report.get("parameters"), indent=2, sort_keys=True,
                                                               default=str), "```",
              "", "## Limitations", "", report["limitations"], ""]
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], out_dir: Path | str, stem: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / f"{stem}.json", out / f"{stem}.md"
    jp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    mp.write_text(render_markdown(report))
    return jp, mp
