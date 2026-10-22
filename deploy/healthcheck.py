#!/usr/bin/env python3
"""Health probe for a wallexbot shadow deployment. Read-only: it never writes to the database.

Opens the SQLite audit DB with ``file:...?mode=ro`` and prints ONE line of JSON. Exit 0 = healthy.

Checks (table and column names come from wallexbot/store.py):
  * strategy heartbeat: newest audit_events row (actor='strategy', kind='heartbeat') must be
    younger than --max-heartbeat-age-s (default 120). No heartbeat at all is unhealthy.
  * run: the bot's latest 'startup' event (actor='bot') gives the run's mode, strategy profile
    (strict | demo, reported as "profile"; "exercise_profile" is true for demo) and whether the
    in-process guardian is enabled. If it is, the guardian heartbeat (actor='guardian',
    kind='heartbeat') must be younger than --max-heartbeat-age-s too.
  * kill switch: latest killswitch_events row. Engaged is UNHEALTHY (exit 1) by default: an engaged
    switch means no new entries, so an unattended run that looks healthy while it cannot trade is
    exactly what this probe must catch. --allow-killswitch downgrades it to a warning (e.g. while an
    operator investigates). "killswitch_auto_release" is true when the latest row is the guardian's
    own transient ('auto:') engagement, which the guardian releases itself once the condition has
    been clear for 3 ticks and 60 s. (--fail-on-killswitch is accepted for compatibility; it is the
    default now.)
  * alerts in the last --alert-window-min minutes (default 15), any of: errors rows whose source
    starts with 'guardian.' (guardian.needs_review, guardian.monitor, guardian.loop, ...),
    audit_events kind 'needs_review', order_intents in state 'needs_review'. Any => unhealthy.
  * optional --recorder-dir DIR: newest *.jsonl in DIR must be younger than --max-recorder-age-s.
    With --recorder-dir and no --db only the recorder is checked.

Exit codes: 0 healthy, 1 unhealthy, 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

HEARTBEAT_ACTOR = "strategy"
HEARTBEAT_KIND = "heartbeat"


def _open_ro(db: str) -> sqlite3.Connection:
    uri = "file:" + quote(str(Path(db).absolute())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def check_db(db: str, *, now: float, max_heartbeat_age_s: float, alert_window_min: float,
             allow_killswitch: bool = False) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    out: dict = {}
    conn = _open_ro(db)
    try:
        row = conn.execute("SELECT ts FROM audit_events WHERE actor=? AND kind=? ORDER BY id DESC LIMIT 1",
                           (HEARTBEAT_ACTOR, HEARTBEAT_KIND)).fetchone()
        if row is None:
            out["heartbeat_age_s"] = None
            problems.append("no strategy heartbeat yet")
        else:
            age = now - float(row[0])
            out["heartbeat_age_s"] = round(age, 1)
            if age > max_heartbeat_age_s:
                problems.append(f"strategy heartbeat is {age:.0f}s old (limit {max_heartbeat_age_s:.0f}s)")

        run = _latest_run(conn)
        out["profile"] = run.get("profile")
        out["exercise_profile"] = run.get("profile") == "demo"
        out["mode"] = run.get("mode")
        g = run.get("guardian") if isinstance(run.get("guardian"), dict) else {}
        out["guardian_enabled"] = g.get("enabled")
        if out["exercise_profile"]:
            warnings.append("EXERCISE PROFILE (demo) - no edge claimed - shadow/paper only")
        if g.get("enabled"):
            grow = conn.execute("SELECT ts FROM audit_events WHERE actor='guardian' AND kind=? ORDER BY id DESC "
                                "LIMIT 1", (HEARTBEAT_KIND,)).fetchone()
            gage = None if grow is None else now - float(grow[0])
            out["guardian_heartbeat_age_s"] = None if gage is None else round(gage, 1)
            if gage is None:
                problems.append("guardian enabled but no guardian heartbeat yet")
            elif gage > max_heartbeat_age_s:
                problems.append(f"guardian heartbeat is {gage:.0f}s old (limit {max_heartbeat_age_s:.0f}s)")

        ks = conn.execute("SELECT engaged, reason, actor FROM killswitch_events ORDER BY id DESC LIMIT 1").fetchone()
        engaged = ks is not None and ks[0] != 0          # same rule as KillSwitch: anything but 0 is engaged
        out["killswitch_engaged"] = engaged
        if engaged:
            out["killswitch_reason"] = ks[1]
            out["killswitch_actor"] = ks[2]
            out["killswitch_auto_release"] = ks[2] == "guardian" and str(ks[1]).startswith("auto:")
            msg = f"kill switch engaged by {ks[2]}: {ks[1]}"
            if out["killswitch_auto_release"]:
                msg += " (transient: the guardian releases it once clear)"
            (warnings if allow_killswitch else problems).append(msg)

        since = now - alert_window_min * 60.0
        n_err = conn.execute("SELECT COUNT(*) FROM errors WHERE ts>=? AND source LIKE 'guardian.%'",
                             (since,)).fetchone()[0]
        n_audit = conn.execute("SELECT COUNT(*) FROM audit_events WHERE ts>=? AND kind='needs_review'",
                               (since,)).fetchone()[0]
        n_intent = conn.execute("SELECT COUNT(*) FROM order_intents WHERE state='needs_review' AND updated_at>=?",
                                (since,)).fetchone()[0]
        out["recent_alerts"] = {"guardian_errors": n_err, "needs_review_events": n_audit,
                                "needs_review_intents": n_intent, "window_min": alert_window_min}
        if n_err or n_audit or n_intent:
            last = conn.execute("SELECT source, message FROM errors WHERE ts>=? AND source LIKE 'guardian.%' "
                                "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
            problems.append(f"{n_err + n_audit + n_intent} alert row(s) in the last {alert_window_min:g} min"
                            + (f" (latest: {last[0]}: {last[1][:80]})" if last else ""))
    finally:
        conn.close()
    out["problems"], out["warnings"] = problems, warnings
    return out


def _latest_run(conn: sqlite3.Connection) -> dict:
    """The run record of the bot's latest startup event ({} if none or unreadable)."""
    row = conn.execute("SELECT payload FROM audit_events WHERE actor='bot' AND kind='startup' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    try:
        p = json.loads(row[0]) if row is not None else {}
    except (TypeError, ValueError):
        return {}
    run = p.get("run") if isinstance(p, dict) else None
    out = dict(run) if isinstance(run, dict) else {}
    if "profile" not in out and isinstance(p, dict) and p.get("profile"):
        out["profile"] = p["profile"]
    return out


def check_recorder(directory: str, *, now: float, max_age_s: float) -> dict:
    files = sorted(Path(directory).glob("*.jsonl")) if Path(directory).is_dir() else []
    if not files:
        return {"recorder_age_s": None, "problems": ["recorder: no *.jsonl files yet"], "warnings": []}
    age = now - max(f.stat().st_mtime for f in files)
    probs = [f"recorder output is {age:.0f}s old (limit {max_age_s:.0f}s)"] if age > max_age_s else []
    return {"recorder_age_s": round(age, 1), "problems": probs, "warnings": []}


def run(args: argparse.Namespace, now: float | None = None) -> tuple[int, dict]:
    now = time.time() if now is None else now
    status: dict = {"ts": round(now, 3), "problems": [], "warnings": []}
    parts = []
    try:
        if args.db:
            parts.append(check_db(args.db, now=now, max_heartbeat_age_s=args.max_heartbeat_age_s,
                                  alert_window_min=args.alert_window_min,
                                  allow_killswitch=bool(getattr(args, "allow_killswitch", False))))
        if args.recorder_dir:
            parts.append(check_recorder(args.recorder_dir, now=now, max_age_s=args.max_recorder_age_s))
    except (sqlite3.Error, OSError) as e:
        status["problems"].append(f"cannot read database: {type(e).__name__}: {e}")
    for p in parts:
        status["problems"] += p.pop("problems")
        status["warnings"] += p.pop("warnings")
        status.update(p)
    status["ok"] = not status["problems"]
    return (0 if status["ok"] else 1), status


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="wallexbot health probe (read-only, one JSON line)")
    p.add_argument("--db", help="path of the audit SQLite database (opened read-only)")
    p.add_argument("--max-heartbeat-age-s", type=float, default=120.0)
    p.add_argument("--alert-window-min", type=float, default=15.0,
                   help="alert rows newer than this many minutes make the probe fail")
    p.add_argument("--allow-killswitch", action="store_true",
                   help="report an engaged kill switch as a warning instead of failing (default: exit 1)")
    p.add_argument("--fail-on-killswitch", action="store_true",
                   help="compatibility no-op: an engaged kill switch fails by default")
    p.add_argument("--recorder-dir", help="also require fresh *.jsonl output in this directory")
    p.add_argument("--max-recorder-age-s", type=float, default=300.0)
    args = p.parse_args(argv)
    if not args.db and not args.recorder_dir:
        p.error("give --db and/or --recorder-dir")
    code, status = run(args)
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
