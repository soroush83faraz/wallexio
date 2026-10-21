"""Market-data recorder: depth top-N + latest trades per symbol -> daily-rotating JSONL files.

Never crashes on a single failed request. Trades have no id on Wallex, so they are deduped by
(ts, price, qty, side); /v1/trades only returns the latest 50, so a quiet-interval overlap with the
previous poll is expected, and a non-overlapping full page is counted as a possible gap.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
SEEN_KEEP = 2000  # per-symbol dedupe memory


class Recorder:
    def __init__(self, client, symbols: list[str], out_dir: Path | str, interval_s: float = 15,
                 depth_levels: int = 10, clock=time.time):
        self._client = client
        self._symbols = list(symbols)
        self._dir = Path(out_dir)
        self._interval = interval_s
        self._levels = depth_levels
        self._clock = clock
        self._stop = threading.Event()
        self._seen: dict[str, deque] = {}
        self._seen_set: dict[str, set] = {}
        self._loaded_days: set[tuple[str, str]] = set()
        self.stats = {"depth_ok": 0, "depth_err": 0, "trades_ok": 0, "trades_err": 0,
                      "trades_new": 0, "possible_trade_gaps": 0}

    # ---- files -------------------------------------------------------------
    def _path(self, kind: str, day: str) -> Path:
        return self._dir / f"{kind}_{day}.jsonl"

    def _append(self, path: Path, rows: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%d")

    def _remember(self, symbol: str, key: tuple) -> None:
        dq = self._seen.setdefault(symbol, deque())
        st = self._seen_set.setdefault(symbol, set())
        dq.append(key)
        st.add(key)
        while len(dq) > SEEN_KEEP:
            st.discard(dq.popleft())

    def _preload_seen(self, symbol: str, day: str) -> None:
        """After a restart, reload today's keys so already-written trades are not duplicated."""
        if (symbol, day) in self._loaded_days:
            return
        self._loaded_days.add((symbol, day))
        p = self._path("trades", day)
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue  # torn last line from a crash
            if r.get("symbol") == symbol:
                self._remember(symbol, (r["ts"], r["price"], r["qty"], r["is_buy"]))

    # ---- one cycle -----------------------------------------------------------
    def _record_depth(self, symbol: str, day: str) -> None:
        d = self._client.depth(symbol)
        row = {"ts": d.ts, "symbol": symbol,
               "bids": [[str(p), str(q)] for p, q in d.bids[: self._levels]],
               "asks": [[str(p), str(q)] for p, q in d.asks[: self._levels]]}
        self._append(self._path("depth", day), [row])

    def _record_trades(self, symbol: str, day: str) -> None:
        self._preload_seen(symbol, day)
        trades = self._client.trades(symbol)
        seen = self._seen_set.setdefault(symbol, set())
        new = []
        for t in trades:
            key = (t.ts, str(t.price), str(t.quantity), t.is_buy)
            if key in seen:
                continue
            self._remember(symbol, key)
            new.append({"ts": t.ts, "symbol": symbol, "price": str(t.price), "qty": str(t.quantity),
                        "is_buy": t.is_buy})
        if trades and len(new) == len(trades) and len(trades) >= 50 and self.stats["trades_ok"] > 0:
            self.stats["possible_trade_gaps"] += 1  # full page, nothing overlapped the previous poll
        if new:
            self._append(self._path("trades", day), new)
        self.stats["trades_new"] += len(new)

    def run_once(self) -> dict:
        """Snapshot every symbol once; a failure on one request is logged and skipped."""
        day = self._day()
        for sym in self._symbols:
            for kind, fn in (("depth", self._record_depth), ("trades", self._record_trades)):
                try:
                    fn(sym, day)
                    self.stats[f"{kind}_ok"] += 1
                except Exception:  # noqa: BLE001 - deliberate: one bad request must not stop the loop
                    self.stats[f"{kind}_err"] += 1
                    log.exception("recorder: %s failed for %s", kind, sym)
        return dict(self.stats)

    # ---- loop ---------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, handle_signals: bool = False, max_cycles: int | None = None) -> None:
        if handle_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        cycles = 0
        while not self._stop.is_set():
            self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._stop.wait(self._interval)
