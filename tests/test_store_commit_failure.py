"""store.tx when COMMIT itself fails (verifier v6/commit.py).

SQLite does not always roll back when COMMIT raises (e.g. an I/O error). store.tx used to leave the
connection inside the write transaction (in_transaction True, depth unchanged), after which every
later BEGIN failed with 'cannot start a transaction within a transaction' and every submit failed
until a restart; the open write lock also blocked every other connection. Now any COMMIT exception
rolls back (secondary errors ignored), resets the nesting depth and re-raises; a retry works.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from decimal import Decimal as D

import pytest

from wallexbot.types import IntentState, OrderIntent, OrderType, Side

S = IntentState
SYM = "BTCUSDT"


class FailingConn:
    """Wraps the store's sqlite3 connection; raises once on the given statement WITHOUT running it,
    so the transaction stays open exactly like a COMMIT that SQLite did not roll back."""

    def __init__(self, conn, fail_on: str):
        self._c, self.fail_on, self.armed = conn, fail_on, False

    def execute(self, sql, *args):
        if self.armed and sql == self.fail_on:
            self.armed = False
            raise sqlite3.OperationalError(f"disk I/O error (injected on {sql})")
        return self._c.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def failing(rig):
    def install(fail_on="COMMIT"):
        proxy = FailingConn(rig.store._conn, fail_on)
        rig.store._conn = proxy
        return proxy
    yield install
    if isinstance(rig.store._conn, FailingConn):
        rig.store._conn = rig.store._conn._c


def _other_writer_ok(path) -> None:
    c = sqlite3.connect(path, timeout=1)
    try:
        c.execute("INSERT INTO errors(ts, source, message, payload) VALUES (1, 'x', 'y', '{}')")
        c.commit()
    finally:
        c.close()


def test_commit_failure_rolls_back_and_resets_depth(rig, failing):
    p = failing()
    p.armed = True
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        with rig.store.tx():
            rig.store.append_error("test.src", "lost with the failed commit")
    assert not p._c.in_transaction and rig.store._depth == 0
    assert rig.store.rows("errors") == []                       # rolled back, not half-committed
    _other_writer_ok(rig.store.path)                            # the write lock was released
    with rig.store.tx():                                        # a new transaction starts cleanly
        rig.store.append_error("test.src", "kept")
    assert [r["message"] for r in rig.store.rows("errors")][-1] == "kept"


def test_release_failure_of_a_savepoint_rolls_back_to_it(rig, failing):
    p = failing("RELEASE sp1")
    with rig.store.tx():
        rig.store.append_error("test.src", "outer")
        p.armed = True
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            with rig.store.tx():
                rig.store.append_error("test.src", "inner")
        assert rig.store._depth == 1
    assert not p._c.in_transaction and rig.store._depth == 0
    assert [r["message"] for r in rig.store.rows("errors")] == ["outer"]


def test_submit_retry_after_a_commit_failure_works_and_sends_once(rig, failing):
    sent = Counter()
    orig = rig.broker.submit_order

    def counting(**kw):
        sent[kw["client_id"]] += 1
        return orig(**kw)
    rig.broker.submit_order = counting
    intent = OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, "C1")
    p = failing()
    p.armed = True
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        rig.submit(intent)
    assert not p._c.in_transaction and rig.store._depth == 0
    _other_writer_ok(rig.store.path)
    res = rig.submit(intent)                                    # the same intent again
    assert res.state is S.FILLED and sent[intent.client_id] == 1
