"""Deployment files: static checks (Dockerfile, compose, systemd, env templates, docs) and the healthcheck/recorder scripts."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

from test_no_secrets_in_repo import scan_text_for_secrets
from wallexbot.store import Store

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
UNITS = sorted((DEPLOY / "systemd").glob("wallexbot-*.service"))
NEW_FILES = [ROOT / "Dockerfile", ROOT / ".dockerignore", ROOT / "docker-compose.yml", ROOT / "Makefile",
             ROOT / "docs" / "DEPLOY.md", DEPLOY / "healthcheck.py", DEPLOY / "run_recorder.py",
             DEPLOY / "env.example", *UNITS]
KEYLIKE = re.compile(r"(?i)key|secret|token|passw|arm_live")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"deploy_{name}", DEPLOY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


healthcheck = _load("healthcheck")
run_recorder = _load("run_recorder")


# ---------------------------------------------------------------- Dockerfile
def _docker_lines() -> list[str]:
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_dockerfile_pins_base_image():
    froms = [ln.split()[1] for ln in _docker_lines() if ln.upper().startswith("FROM ")]
    assert froms and all(re.fullmatch(r"python:3\.12\.\d+-slim[\w.-]*", f) for f in froms), froms
    assert ":latest" not in (ROOT / "Dockerfile").read_text()


def test_dockerfile_non_root_workdir_volume_healthcheck_cmd():
    lines = _docker_lines()
    users = [ln.split(None, 1)[1].split(":")[0] for ln in lines if ln.upper().startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
    assert "WORKDIR /app" in lines
    assert any(ln.startswith("VOLUME") and "/data" in ln for ln in lines)
    assert any(ln.startswith("HEALTHCHECK") and "deploy/healthcheck.py" in ln for ln in lines)
    cmd = next(ln for ln in lines if ln.startswith("CMD "))
    argv = json.loads(cmd.removeprefix("CMD "))
    assert argv[:4] == ["python", "-m", "wallexbot", "bot"]
    assert argv[argv.index("--mode") + 1] == "shadow" and "live" not in argv
    assert argv[argv.index("--db") + 1] == "/data/bot.db"


def test_dockerfile_env_and_arg_hold_no_secrets():
    for ln in _docker_lines():
        if not re.match(r"(?i)(ENV|ARG)\s", ln):
            continue
        pairs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", ln.split(None, 1)[1]) or ln.split()[1:2]
        for name in pairs:
            assert not KEYLIKE.search(name), f"key-like name in Dockerfile: {name}"
    assert "COPY . " not in (ROOT / "Dockerfile").read_text()


def test_dockerignore_keeps_secrets_and_state_out():
    ignored = {ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env.*", "*.db", ".git", ".venv"} <= ignored


# ---------------------------------------------------------------- compose
@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_services_and_shadow_commands(compose):
    services = compose["services"]
    assert {"bot", "guardian", "recorder"} <= set(services)
    bot_cmd, guardian_cmd = services["bot"]["command"], services["guardian"]["command"]
    assert bot_cmd[bot_cmd.index("--mode") + 1] == "shadow" and guardian_cmd[guardian_cmd.index("--mode") + 1] == "shadow"
    assert guardian_cmd[3] == "guardian"
    for name, svc in services.items():
        assert "live" not in svc["command"], name
    assert "recorder" in services["recorder"]["profiles"]
    assert "guardian" in services["guardian"]["profiles"]      # opt-in: see docs/DEPLOY.md


def test_compose_has_no_ports_and_is_hardened(compose):
    code_lines = [ln for ln in (ROOT / "docker-compose.yml").read_text().splitlines() if not ln.strip().startswith("#")]
    assert not any(re.match(r"\s*(ports|expose):", ln) for ln in code_lines)
    for name, svc in compose["services"].items():
        assert "ports" not in svc and "expose" not in svc, name
        assert svc["restart"] == "unless-stopped" and svc["read_only"] is True, name
        assert "/tmp" in " ".join(svc["tmpfs"]), name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"] and svc["mem_limit"] and svc["cpus"] and svc["pids_limit"], name
        assert svc["volumes"] == ["wallex-data:/data"], name
        assert svc["env_file"] == [{"path": ".env", "required": False}], name
        assert "environment" not in svc, name          # no inline values at all
        assert not svc.get("privileged") and "network_mode" not in svc and "devices" not in svc, name
    assert "wallex-data" in compose["volumes"]


# ---------------------------------------------------------------- systemd
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_systemd_units_are_hardened(unit):
    assert len(UNITS) == 3
    text = unit.read_text()
    directives = {ln.split("=", 1)[0] for ln in text.splitlines() if "=" in ln and not ln.startswith("#")}
    for must in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "EnvironmentFile=/etc/wallexbot/env",
                 "Restart=on-failure", "User=wallexbot", "ReadWritePaths=/var/lib/wallexbot", "ProtectHome=yes"):
        assert must in text, f"{unit.name}: missing {must}"
    for name in ("TimeoutStopSec", "TimeoutStartSec", "CapabilityBoundingSet", "SystemCallFilter", "MemoryMax",
                 "RestrictAddressFamilies", "PrivateDevices", "StateDirectory"):
        assert name in directives, f"{unit.name}: missing {name}"
    assert "--mode shadow" in text or "run_recorder.py" in text
    assert "ARM_LIVE" not in text and "API_KEY" not in text


def test_guardian_unit_orders_after_bot():
    text = (DEPLOY / "systemd" / "wallexbot-guardian.service").read_text()
    assert "After=wallexbot-bot.service" in text and "PartOf=wallexbot-bot.service" in text
    assert "guardian" in text.split("ExecStart=", 1)[1].splitlines()[0]


# ---------------------------------------------------------------- env templates
@pytest.mark.parametrize("path", [DEPLOY / "env.example", ROOT / ".env.example"], ids=lambda p: p.name)
def test_env_templates_hold_names_only(path):
    names = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        name, sep, value = s.partition("=")
        assert sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", name), f"{path.name}:{n}"
        assert value == "", f"{path.name}:{n} {name} must be empty"
        names.append(name)
    if path.parent == DEPLOY:
        assert not {"WALLEX_API_KEY_TRADE", "WALLEX_ARM_LIVE"} & set(names)


def test_new_files_pass_the_repo_secret_scan():
    for path in NEW_FILES:
        assert path.is_file(), path
        assert scan_text_for_secrets(path.read_text()) == [], path.name


def test_deploy_doc_is_short_and_has_required_sections():
    text = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert len(text.splitlines()) < 150
    for needle in ("Prerequisites", "## Docker", "## systemd", "What to monitor", "Stop safely", ".backup", "Upgrade",
                   "Do not", "Going live is a separate, explicit, user-approved step", "WALLEX_ARM_LIVE",
                   "--arm-live", "WALLEX_API_KEY_TRADE", "doctor", "BACKTEST.md"):
        assert needle in text, needle


def test_makefile_has_required_targets():
    text = (ROOT / "Makefile").read_text()
    for target in ("venv", "test", "demo", "backtest", "shadow", "guardian", "docker-build", "docker-up",
                   "docker-down", "healthcheck", "clean"):
        assert re.search(rf"^{target}:", text, re.M), target
    assert "--mode live" not in text and "arm-live" not in text


# ---------------------------------------------------------------- healthcheck
NOW = 1_800_000_000.0


def _args(db, **kw):
    ns = healthcheck.argparse.Namespace(db=str(db), max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                        fail_on_killswitch=False, recorder_dir=None, max_recorder_age_s=300.0)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bot.db")
    yield s
    s.close()


def _status(store, **kw):
    code, st = healthcheck.run(_args(store.path, **kw), now=NOW)
    return code, st


def test_healthy_with_fresh_heartbeat(store):
    store.heartbeat("strategy", ts=NOW - 5)
    code, st = _status(store)
    assert code == 0 and st["ok"] and st["heartbeat_age_s"] == 5.0
    assert st["killswitch_engaged"] is False and st["problems"] == [] and st["warnings"] == []


def test_stale_or_missing_heartbeat_is_unhealthy(store):
    code, st = _status(store)
    assert code == 1 and "no strategy heartbeat" in st["problems"][0]
    store.heartbeat("strategy", ts=NOW - 500)
    assert _status(store)[0] == 1
    assert _status(store, max_heartbeat_age_s=600.0)[0] == 0
    store.heartbeat("strategy", ts=NOW - 1)
    assert _status(store)[0] == 0


def test_other_components_heartbeat_does_not_count(store):
    store.heartbeat("guardian", ts=NOW - 1)
    assert _status(store)[0] == 1


def test_killswitch_engaged_warns_unless_flagged(store):
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_killswitch_event(True, "guardian: heartbeat_lost", "guardian", ts=NOW - 30)
    code, st = _status(store)
    assert code == 0 and st["killswitch_engaged"] and st["killswitch_actor"] == "guardian"
    assert st["warnings"] and not st["problems"]
    code, st = _status(store, fail_on_killswitch=True)
    assert code == 1 and st["problems"]
    store.append_killswitch_event(False, "operator ok", "operator", ts=NOW - 10)    # latest row wins
    code, st = _status(store, fail_on_killswitch=True)
    assert code == 0 and st["killswitch_engaged"] is False


def test_recent_alerts_fail_old_ones_do_not(store):
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_error("guardian.needs_review", "operator review needed: balance_mismatch", ts=NOW - 3600)
    store.append_audit_event("guardian", "needs_review", {"what": "x"}, ts=NOW - 3600)
    assert _status(store)[0] == 0                               # an hour old, window is 15 min
    store.append_error("guardian.monitor", "reconcile failed", ts=NOW - 60)
    code, st = _status(store)
    assert code == 1 and st["recent_alerts"]["guardian_errors"] == 1 and "guardian.monitor" in st["problems"][0]
    assert _status(store, alert_window_min=0.5)[0] == 0         # window shorter than the row's age


def test_recent_needs_review_audit_event_fails(store):
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_audit_event("guardian", "needs_review", {"what": "balance_mismatch"}, ts=NOW - 20)
    code, st = _status(store)
    assert code == 1 and st["recent_alerts"]["needs_review_events"] == 1


def test_non_guardian_errors_do_not_fail(store):
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_error("bot.feed", "one slow request", ts=NOW - 5)
    assert _status(store)[0] == 0


def test_needs_review_intent_fails(store):
    from decimal import Decimal
    from wallexbot.types import IntentState, OrderIntent, OrderType, Side
    store.heartbeat("strategy", ts=NOW - 1)
    intent = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, Decimal("0.010"), Decimal("60010.00"), "sig1")
    store.append_intent(intent, ts=NOW - 100)
    for st_ in (IntentState.RISK_APPROVED, IntentState.SUBMITTED, IntentState.NEEDS_REVIEW):
        store.set_intent_state(intent.client_id, st_, ts=NOW - 50, reason="test")
    code, st = _status(store)
    assert code == 1 and st["recent_alerts"]["needs_review_intents"] == 1


def test_database_is_opened_read_only(store, tmp_path):
    store.heartbeat("strategy", ts=NOW - 1)
    before = [(p.name, p.stat().st_size) for p in sorted(tmp_path.iterdir()) if p.name == "bot.db"]
    conn = healthcheck._open_ro(store.path)
    with pytest.raises(Exception, match="readonly|read-only|query_only"):
        conn.execute("INSERT INTO errors(ts, source, message, payload) VALUES (1, 's', 'm', '{}')")
    conn.close()
    assert _status(store)[0] == 0
    assert before == [(p.name, p.stat().st_size) for p in sorted(tmp_path.iterdir()) if p.name == "bot.db"]


def test_missing_database_is_unhealthy_and_not_created(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert healthcheck.main(["--db", str(missing)]) == 1
    out = capsys.readouterr().out.strip()
    st = json.loads(out)
    assert st["ok"] is False and "cannot read database" in st["problems"][0]
    assert not missing.exists()


def test_cli_prints_one_json_line_and_exit_code(store, capsys):
    store.heartbeat("strategy")                                  # real clock, real now
    assert healthcheck.main(["--db", store.path]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out)["ok"] is True
    with pytest.raises(SystemExit) as e:
        healthcheck.main([])
    assert e.value.code == 2


def test_recorder_dir_check(tmp_path):
    rec = tmp_path / "recorder"
    code, st = healthcheck.run(healthcheck.argparse.Namespace(db=None, recorder_dir=str(rec), max_recorder_age_s=300.0,
                                                              max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                                              fail_on_killswitch=False), now=time.time())
    assert code == 1 and "no *.jsonl" in st["problems"][0]
    rec.mkdir()
    (rec / "depth_20260101.jsonl").write_text("{}\n")
    code, st = healthcheck.run(healthcheck.argparse.Namespace(db=None, recorder_dir=str(rec), max_recorder_age_s=300.0,
                                                              max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                                              fail_on_killswitch=False), now=time.time())
    assert code == 0 and st["recorder_age_s"] < 5
    code, st = healthcheck.run(healthcheck.argparse.Namespace(db=None, recorder_dir=str(rec), max_recorder_age_s=300.0,
                                                              max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                                              fail_on_killswitch=False), now=time.time() + 1000)
    assert code == 1


# ---------------------------------------------------------------- recorder entry point
def test_recorder_symbols_resolution():
    assert run_recorder.resolve_symbols(None, None) == list(run_recorder.DEFAULT_SYMBOLS)
    assert run_recorder.resolve_symbols(None, "BTCUSDT, ETHUSDT ETHUSDT") == ["BTCUSDT", "ETHUSDT"]
    assert run_recorder.resolve_symbols(["SOLUSDT"], "BTCUSDT") == ["SOLUSDT"]
    with pytest.raises(ValueError):
        run_recorder.resolve_symbols(["btc/usdt; rm"], None)


def test_recorder_runs_one_cycle_with_a_fake_public_client(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    class FakeClient:
        def depth(self, symbol):
            return SimpleNamespace(ts=1_700_000_000.0, bids=[(Decimal("99"), Decimal("1"))], asks=[(Decimal("101"), Decimal("1"))])

        def trades(self, symbol):
            return [SimpleNamespace(ts=1_700_000_000.0, price=Decimal("100"), quantity=Decimal("1"), is_buy=True)]

    out = tmp_path / "rec"
    assert run_recorder.main(["--out-dir", str(out), "--symbols", "BTCUSDT", "--max-cycles", "1"], client=FakeClient()) == 0
    files = sorted(p.name for p in out.glob("*.jsonl"))
    assert len(files) == 2 and files[0].startswith("depth_") and files[1].startswith("trades_")
    assert run_recorder.main(["--out-dir", str(out), "--symbols", "bad symbol"], client=FakeClient()) == 2
