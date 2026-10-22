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
             ROOT / "docker-compose.snap.yml", ROOT / "docs" / "DEPLOY.md", DEPLOY / "healthcheck.py",
             DEPLOY / "env.example", *UNITS]
KEYLIKE = re.compile(r"(?i)key|secret|token|passw|arm_live")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"deploy_{name}", DEPLOY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


healthcheck = _load("healthcheck")


# ---------------------------------------------------------------- Dockerfile
def _docker_lines() -> list[str]:
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


PINNED = r"python:3\.12\.\d+-slim[\w.-]*"


def test_dockerfile_pins_base_image():
    """The default base is a pinned Docker Hub tag; BASE_IMAGE lets a registry mirror replace it at build time."""
    lines = _docker_lines()
    froms = [ln.split()[1] for ln in lines if ln.upper().startswith("FROM ")]
    assert froms == ["${BASE_IMAGE}"]
    args = [ln.split(None, 1)[1] for ln in lines if ln.upper().startswith("ARG BASE_IMAGE=")]
    assert len(args) == 1 and re.fullmatch(PINNED, args[0].split("=", 1)[1]), args
    assert lines.index(f"ARG {args[0]}") < lines.index("FROM ${BASE_IMAGE}")    # ARG before FROM
    assert ":latest" not in (ROOT / "Dockerfile").read_text()


def test_dockerfile_installs_the_package_non_editable_and_ships_no_second_config():
    lines = _docker_lines()
    installs = [ln for ln in lines if "pip install" in ln]
    assert installs and all(" -e " not in f" {ln} " and "--editable" not in ln for ln in installs), installs
    assert not any(ln.startswith("COPY") and "config" in ln.split()[1:-1] for ln in lines)
    assert "run_recorder" not in "\n".join(lines)


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
    assert set(services) == {"bot", "recorder"}            # the guardian runs inside the bot process
    bot_cmd = services["bot"]["command"]
    assert bot_cmd[bot_cmd.index("--mode") + 1] == "shadow"
    assert "--no-guardian" not in bot_cmd and "demo" not in bot_cmd   # in-process guardian on, strict profile
    for name, svc in services.items():
        assert "live" not in svc["command"], name
        assert "guardian" not in svc["command"], name
    assert "recorder" in services["recorder"]["profiles"]


def test_compose_has_no_ports_and_is_hardened(compose):
    code_lines = [ln for ln in (ROOT / "docker-compose.yml").read_text().splitlines() if not ln.strip().startswith("#")]
    assert not any(re.match(r"\s*(ports|expose):", ln) for ln in code_lines)
    for name, svc in compose["services"].items():
        assert "ports" not in svc and "expose" not in svc, name
        assert svc["restart"] == "unless-stopped" and svc["read_only"] is True and svc["init"] is True, name
        assert "/tmp" in " ".join(svc["tmpfs"]), name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"] and svc["mem_limit"] and svc["cpus"] and svc["pids_limit"], name
        assert svc["volumes"] == ["wallex-data:/data"], name
        assert svc["env_file"] == [{"path": ".env", "required": False}], name
        assert "environment" not in svc, name          # no inline values at all
        assert not svc.get("privileged") and "network_mode" not in svc and "devices" not in svc, name
    assert "wallex-data" in compose["volumes"]


class _ComposeLoader(yaml.SafeLoader):
    """safe_load that understands Compose's `!reset` merge tag (it stays visible as a marker)."""


class _Reset(list):
    pass


_ComposeLoader.add_constructor("!reset", lambda loader, node: _Reset(loader.construct_sequence(node)))


def test_snap_override_only_resets_security_opt_on_the_base_services(compose):
    path = ROOT / "docker-compose.snap.yml"
    snap = yaml.load(path.read_text(), Loader=_ComposeLoader)
    assert set(snap) == {"services"}                                   # no volumes, networks, x- blocks, secrets
    assert set(snap["services"]) == set(compose["services"]) == {"bot", "recorder"}
    for name, svc in snap["services"].items():
        assert set(svc) == {"security_opt"}, name                      # nothing but security_opt
        assert isinstance(svc["security_opt"], _Reset) and svc["security_opt"] == [], name
    code = [ln for ln in path.read_text().splitlines() if not ln.strip().startswith("#")]
    assert not any(re.match(r"\s*(ports|expose|environment|env_file|command|privileged|volumes):", ln) for ln in code)
    assert not any(KEYLIKE.search(ln) for ln in code)
    assert "no-new-privileges" not in "\n".join(code)                 # the base file keeps the hardening
    assert scan_text_for_secrets(path.read_text()) == []


def test_snap_override_is_wired_into_make_and_documented():
    make = (ROOT / "Makefile").read_text()
    assert "docker-up-snap" in re.search(r"^\.PHONY:.*$", make, re.M).group(0)
    rule = make.split("docker-up-snap:", 1)[1].splitlines()[1]
    assert "-f docker-compose.yml -f docker-compose.snap.yml up -d --build" in rule
    doc = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert "docker-compose.snap.yml" in doc and "make docker-up-snap" in doc


def test_smoke_target_and_doc_section_exist():
    make = (ROOT / "Makefile").read_text()
    assert re.search(r"^smoke: ", make, re.M) and "deploy/smoke_shadow_fill.py" in make
    assert "smoke" in re.search(r"^\.PHONY:.*$", make, re.M).group(0).split()
    doc = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert "## Pre-deploy smoke test" in doc and "SMOKE TEST - forced test signal - SHADOW ONLY" in doc


# ---------------------------------------------------------------- systemd
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_systemd_units_are_hardened(unit):
    assert len(UNITS) == 2
    text = unit.read_text()
    directives = {ln.split("=", 1)[0] for ln in text.splitlines() if "=" in ln and not ln.startswith("#")}
    for must in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "EnvironmentFile=/etc/wallexbot/env",
                 "Restart=on-failure", "User=wallexbot", "ReadWritePaths=/var/lib/wallexbot", "ProtectHome=yes"):
        assert must in text, f"{unit.name}: missing {must}"
    for name in ("TimeoutStopSec", "TimeoutStartSec", "CapabilityBoundingSet", "SystemCallFilter", "MemoryMax",
                 "RestrictAddressFamilies", "PrivateDevices", "StateDirectory"):
        assert name in directives, f"{unit.name}: missing {name}"
    assert "--mode shadow" in text or "-m wallexbot record" in text
    assert "ARM_LIVE" not in text and "API_KEY" not in text


def test_no_separate_guardian_unit_bot_runs_it_in_process():
    assert not (DEPLOY / "systemd" / "wallexbot-guardian.service").exists()
    exec_line = (DEPLOY / "systemd" / "wallexbot-bot.service").read_text().split("ExecStart=", 1)[1].splitlines()[0]
    assert " bot " in exec_line and "--no-guardian" not in exec_line and "--profile demo" not in exec_line


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


def _interval(argv):
    return float(argv[argv.index("--interval-s") + 1])


def test_deployed_bot_interval_is_within_half_the_data_age_and_probe_fails_on_kill_switch():
    """The shipped bot commands pass the --interval-s bound (the bot refuses > max_data_age_s / 2,
    15 s), and no shipped health probe opts out of failing on an engaged kill switch."""
    from wallexbot.bot.main import max_interval_s
    from wallexbot.config import load_config
    limit = max_interval_s(load_config(env={}))
    lines = _docker_lines()
    assert _interval(json.loads(next(ln for ln in lines if ln.startswith("CMD ")).removeprefix("CMD "))) <= limit
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert _interval(compose["services"]["bot"]["command"]) <= limit
    unit = (DEPLOY / "systemd" / "wallexbot-bot.service").read_text()
    exec_start = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert _interval(exec_start.split()) <= limit
    make = (ROOT / "Makefile").read_text()
    assert float(re.search(r"^INTERVAL \?= (\S+)$", make, re.M).group(1)) <= limit
    hc_lines = [ln for ln in lines if ln.startswith(("HEALTHCHECK", "CMD [\"python\", \"/app/deploy/health"))]
    for text in ["\n".join(lines), make, unit, (ROOT / "docker-compose.yml").read_text()]:
        assert "--allow-killswitch" not in "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
                                                     and "HEALTH_ARGS" not in ln), text[:40]
    assert hc_lines and re.search(r"^HEALTH_ARGS \?=\s*$", make, re.M)


def test_makefile_has_required_targets():
    text = (ROOT / "Makefile").read_text()
    for target in ("venv", "test", "demo", "backtest", "shadow", "guardian", "docker-build", "docker-up",
                   "docker-down", "healthcheck", "clean"):
        assert re.search(rf"^{target}:", text, re.M), target
    assert "--mode live" not in text and "arm-live" not in text
    for line in text.splitlines():           # the standalone guardian is a demo on its own DB only
        if "wallexbot guardian" in line:
            assert "--demo-db" in line and "$(DB)" not in line, line


# ---------------------------------------------------------------- healthcheck
NOW = 1_800_000_000.0


def _args(db, **kw):
    ns = healthcheck.argparse.Namespace(db=str(db), max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                        allow_killswitch=False, recorder_dir=None, max_recorder_age_s=300.0)
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


def _startup(store, ts, profile="strict", guardian=True):
    store.append_audit_event("bot", "startup", {"run": {"mode": "shadow", "profile": profile,
                                                        "guardian": {"enabled": guardian, "interval_s": 15}},
                                                "profile": profile}, ts=ts)


def test_reports_profile_and_requires_guardian_heartbeat_when_enabled(store):
    store.heartbeat("strategy", ts=NOW - 5)
    _startup(store, NOW - 100)
    code, st = _status(store)
    assert code == 1 and st["guardian_enabled"] is True and "no guardian heartbeat" in st["problems"][0]
    assert st["profile"] == "strict" and st["exercise_profile"] is False and st["mode"] == "shadow"
    store.heartbeat("guardian", ts=NOW - 500)
    assert "guardian heartbeat is 500s old" in _status(store)[1]["problems"][0]
    store.heartbeat("guardian", ts=NOW - 3)
    code, st = _status(store)
    assert code == 0 and st["guardian_heartbeat_age_s"] == 3.0


def test_demo_profile_is_reported_and_guardian_off_needs_no_heartbeat(store):
    store.heartbeat("strategy", ts=NOW - 5)
    _startup(store, NOW - 100, profile="demo", guardian=False)
    code, st = _status(store)
    assert code == 0 and st["profile"] == "demo" and st["exercise_profile"] is True
    assert st["guardian_enabled"] is False and any("EXERCISE PROFILE" in w for w in st["warnings"])


def test_other_components_heartbeat_does_not_count(store):
    store.heartbeat("guardian", ts=NOW - 1)
    assert _status(store)[0] == 1


def test_killswitch_engaged_fails_unless_allowed(store):
    """An engaged kill switch means no new entries: unhealthy by default (an unattended run that
    can no longer trade must not look healthy). --allow-killswitch downgrades it to a warning."""
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_killswitch_event(True, "guardian: balance_mismatch:BTC", "guardian", ts=NOW - 30)
    code, st = _status(store)
    assert code == 1 and st["killswitch_engaged"] and st["killswitch_actor"] == "guardian"
    assert st["problems"] and st["killswitch_auto_release"] is False
    code, st = _status(store, allow_killswitch=True)
    assert code == 0 and st["warnings"] and not st["problems"]
    store.append_killswitch_event(False, "operator ok", "operator", ts=NOW - 10)    # latest row wins
    code, st = _status(store)
    assert code == 0 and st["killswitch_engaged"] is False


def test_transient_killswitch_is_flagged_auto_release_and_still_fails(store):
    store.heartbeat("strategy", ts=NOW - 1)
    store.append_killswitch_event(True, "auto:heartbeat_lost", "guardian", ts=NOW - 30)
    code, st = _status(store)
    assert code == 1 and st["killswitch_auto_release"] is True and "transient" in st["problems"][0]
    store.append_killswitch_event(True, "auto:heartbeat_lost", "operator", ts=NOW - 20)   # not the guardian's
    assert _status(store)[1]["killswitch_auto_release"] is False


def test_killswitch_cli_flags(store, capsys):
    store.heartbeat("strategy", ts=time.time())
    store.append_killswitch_event(True, "operator: maintenance", "operator", ts=time.time())
    assert healthcheck.main(["--db", store.path]) == 1
    assert healthcheck.main(["--db", store.path, "--fail-on-killswitch"]) == 1     # compatibility no-op
    assert healthcheck.main(["--db", store.path, "--allow-killswitch"]) == 0
    capsys.readouterr()


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
                                                              allow_killswitch=False), now=time.time())
    assert code == 1 and "no *.jsonl" in st["problems"][0]
    rec.mkdir()
    (rec / "depth_20260101.jsonl").write_text("{}\n")
    code, st = healthcheck.run(healthcheck.argparse.Namespace(db=None, recorder_dir=str(rec), max_recorder_age_s=300.0,
                                                              max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                                              allow_killswitch=False), now=time.time())
    assert code == 0 and st["recorder_age_s"] < 5
    code, st = healthcheck.run(healthcheck.argparse.Namespace(db=None, recorder_dir=str(rec), max_recorder_age_s=300.0,
                                                              max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                                              allow_killswitch=False), now=time.time() + 1000)
    assert code == 1


# ---------------------------------------------------------------- recorder entry point (now `wallexbot record`)
def test_recorder_is_started_through_the_cli_everywhere():
    assert not (DEPLOY / "run_recorder.py").exists()
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["recorder"]["command"][:4] == ["python", "-m", "wallexbot", "record"]
    unit = (DEPLOY / "systemd" / "wallexbot-recorder.service").read_text()
    assert "-m wallexbot record --out /var/lib/wallexbot/recorder" in unit and "run_recorder" not in unit


def test_systemd_syscall_filter_does_not_deny_resources():
    """`~@resources` kills numpy/pandas (OpenBLAS thread setup) with SIGSYS at import: seen under systemd-run --user."""
    for unit in UNITS:
        filters = [ln for ln in unit.read_text().splitlines() if ln.startswith("SystemCallFilter=")]
        assert filters and not any("@resources" in ln for ln in filters), unit.name

