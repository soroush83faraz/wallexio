"""Doctor (read-only preflight): fake transport, FAKE keys and a fake git runner; no network.

The Wallex responses are HAND-WRITTEN to the shapes wallexbot.exchange.wallex_private assumes.
"""
from __future__ import annotations

import ast
import email.utils
import importlib.util
import inspect
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from wallexbot import doctor as DR
from wallexbot import redact as R
from wallexbot.config import DEFAULT_PATH
from wallexbot.doctor import FAIL, PASS, SKIP, UNVERIFIABLE, run_doctor
from wallexbot.exchange import auth as A
from wallexbot.exchange.auth import AuthError
from wallexbot.redact import Secret

FAKE_READ_KEY = "FAKEtestDr5Hk8Lm3Np6Qr9St2Uv"
FAKE_TRADE_KEY = "FAKEtestDt7Wx2Yz4Ab6Cd8Ef1Gh"
DATE = "Thu, 22 Oct 2026 08:00:00 GMT"
SERVER_TS = email.utils.parsedate_to_datetime(DATE).timestamp()
READ_PATHS = {"/v1/account/balances", "/v1/account/fee", "/v1/account/openOrders"}
ALL_CHECKS = ["host_pin", "read_key", "balances", "fee", "open_orders", "not_found_lookup",
              "clock_skew", "config", "dotenv_ignored", "repo_secret_scan"]


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "_wallexbot_secret_scanner_doctor", Path(__file__).with_name("test_no_secrets_in_repo.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan_text_for_secrets


scan_text_for_secrets = _load_scanner()


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = set(R._SECRETS)
    R._reset_registry()
    yield
    R._reset_registry(saved)


def envelope(result, success=True, message="The operation was successful"):
    return json.dumps({"result": result, "message": message, "success": success}).encode()


class FakeReadApi:
    """Answers the private READ endpoints the way the adapter assumes Wallex does."""

    def __init__(self, *, date=DATE, key=FAKE_READ_KEY, overrides=None, echo_order=False):
        self.date, self.key, self.echo_order = date, key, echo_order
        self.overrides = overrides or {}          # path prefix -> (status, body)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        path = urlsplit(url).path
        got = {"Date": self.date} if self.date else {}
        for prefix, (status, payload) in self.overrides.items():
            if path.startswith(prefix):
                return status, got, payload
        presented = headers.get("X-API-Key")
        if presented != self.key:                 # a hostile server echoes the key it was sent
            return 401, got, envelope({}, False, f"invalid api key {presented}")
        if path == "/v1/account/balances":
            return 200, got, envelope({"balances": {
                "USDT": {"asset": "USDT", "value": "100.5000000000000000", "locked": "0.5"},
                "BTC": {"asset": "BTC", "value": "0", "locked": "0"}}})
        if path == "/v1/account/fee":
            return 200, got, envelope({
                "BTCUSDT": {"makerFeeRate": "0.0020", "takerFeeRate": "0.0025", "recent_days_sum": 0},
                "ETHUSDT": {"makerFeeRate": "0.0030", "takerFeeRate": "0.0035", "recent_days_sum": 0}})
        if path == "/v1/account/openOrders":
            return 200, got, envelope({"orders": []})
        if path.startswith("/v1/account/orders/"):
            if self.echo_order:
                cid = path.rsplit("/", 1)[1]
                return 200, got, envelope({
                    "clientOrderId": cid, "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "origQty": "1", "status": "NEW", "fills": []})
            return 404, got, envelope({}, False, "Order not found.")
        return 404, got, b"<html>404 Not Found</html>"


def git_answers(**overrides):
    answers = {"rev-parse": (0, "true\n"), "check-ignore": (0, ""), "ls-files": (0, "")}
    answers.update(overrides)
    return lambda args: answers[args[0]]


def doctor(api, key=..., **kw):
    kw.setdefault("clock", lambda: SERVER_TS + 1.5)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("env", {})
    kw.setdefault("git", git_answers())
    return run_doctor(Secret(FAKE_READ_KEY) if key is ... else key, api, **kw)


# ----------------------------------------------------------------------------- happy path
def test_all_green_and_strictly_read_only():
    api = FakeReadApi()
    report = doctor(api)
    assert [c.name for c in report.checks] == ALL_CHECKS
    assert {c.name: c.status for c in report.checks} == {
        **{name: PASS for name in ALL_CHECKS}, "repo_secret_scan": SKIP}
    assert report.ok
    assert {method for method, *_ in api.calls} == {"GET"}
    assert all(body is None for *_, body in api.calls)
    paths = [urlsplit(url).path for _, url, _, _ in api.calls]
    probes = [p for p in paths if p.startswith("/v1/account/orders/wb_")]
    assert len(probes) == 1 and set(paths) - set(probes) == READ_PATHS and len(paths) == 4
    assert all(headers["X-API-Key"] == FAKE_READ_KEY for _, _, headers, _ in api.calls)
    assert report.by_name("balances").detail == "2 assets parsed"
    assert "highest maker 0.003, highest taker 0.0035" in report.by_name("fee").detail
    assert "+1.5s" in report.by_name("clock_skew").detail
    assert report.by_name("config").detail == "loads fail-closed; mode=mock (not live)"


def test_report_masks_the_key_and_lists_what_cannot_be_verified():
    report = doctor(FakeReadApi())
    text = report.render()
    assert f"Secret(****{FAKE_READ_KEY[-4:]})" in text and FAKE_READ_KEY not in text
    assert scan_text_for_secrets(text, [FAKE_READ_KEY], heuristics=False) == []
    assert report.unverifiable == UNVERIFIABLE
    joined = " ".join(report.unverifiable)
    assert "Withdraw" in joined and "IP allow-list" in joined and "Trade permission" in joined
    assert "Could NOT be verified from code" in text and text.startswith("wallexbot doctor: OK")


def test_the_doctor_has_no_trade_key_parameter():
    params = inspect.signature(run_doctor).parameters
    assert not any("trade" in name or "armed" in name for name in params)


# ----------------------------------------------------------------------------- key problems
@pytest.mark.parametrize("key", [None, FAKE_READ_KEY])       # a raw str is refused, never echoed
def test_no_usable_read_key_sends_nothing(key):
    api = FakeReadApi()
    report = doctor(api, key=key)
    assert api.calls == [] and not report.ok
    assert report.by_name("read_key").status == FAIL
    assert all(report.by_name(n).status == FAIL for n in DR.NETWORK_CHECKS)
    assert report.by_name("clock_skew").status == SKIP
    assert FAKE_READ_KEY not in report.render()


def test_malformed_read_key_is_refused_without_echo():
    api = FakeReadApi()
    report = doctor(api, key=Secret("FAKE test key with spaces"))
    assert report.by_name("read_key").status == FAIL and api.calls == []
    assert "with spaces" not in report.render()


def test_rejected_key_fails_every_network_check_without_leaking_it():
    api = FakeReadApi(key="FAKEtestSomeOtherKey000000")
    report = doctor(api)
    assert all(report.by_name(n).status == FAIL for n in DR.NETWORK_CHECKS) and not report.ok
    assert "key rejected" in report.by_name("balances").detail
    assert "AuthRejected" in report.by_name("not_found_lookup").detail
    text = report.render() + repr(report)
    assert scan_text_for_secrets(text, [FAKE_READ_KEY], heuristics=False) == []
    assert {method for method, *_ in api.calls} == {"GET"}


def test_transport_failures_are_reported_not_raised():
    def broken(method, url, headers, body, timeout):
        raise RuntimeError(f"socket exploded while sending {headers['X-API-Key']}")
    report = doctor(broken)
    assert all(report.by_name(n).status == FAIL for n in DR.NETWORK_CHECKS)
    assert scan_text_for_secrets(report.render(), [FAKE_READ_KEY], heuristics=False) == []


# ----------------------------------------------------------------------------- response shapes
def test_unexpected_response_shapes_fail_closed():
    api = FakeReadApi(overrides={
        "/v1/account/balances": (200, envelope({"balances": [{"asset": "USDT", "free": "1"}]})),
        "/v1/account/fee": (200, envelope({"BTCUSDT": {"makerFeeRate": "0.25",
                                                       "takerFeeRate": "0.25"}})),
        "/v1/account/openOrders": (200, envelope([]))})
    report = doctor(api)
    for name in ("balances", "fee", "open_orders"):
        assert report.by_name(name).status == FAIL
        assert "BrokerDataError" in report.by_name(name).detail
    assert report.by_name("not_found_lookup").status == PASS


@pytest.mark.parametrize("status, body", [
    (404, b"<html>404 Not Found</html>"),                      # router page: not proof of absence
    (404, b'{"message": "Not Found"}'),
    (200, envelope({}, False, "Order not found.")),
    (400, envelope({}, False, "Order not found.")),
])
def test_not_found_lookup_fails_when_the_shape_differs(status, body):
    report = doctor(FakeReadApi(overrides={"/v1/account/orders/": (status, body)}))
    assert report.by_name("not_found_lookup").status == FAIL and not report.ok


def test_not_found_lookup_fails_if_a_random_id_returns_an_order():
    report = doctor(FakeReadApi(echo_order=True))
    assert report.by_name("not_found_lookup").status == FAIL


# ----------------------------------------------------------------------------- clock
@pytest.mark.parametrize("offset, status", [(1.5, PASS), (-4.0, PASS), (60.0, FAIL), (-6.0, FAIL)])
def test_clock_skew_against_the_server_date_header(offset, status):
    report = doctor(FakeReadApi(), clock=lambda: SERVER_TS + offset)
    assert report.by_name("clock_skew").status == status


def test_clock_skew_is_skipped_without_a_date_header():
    report = doctor(FakeReadApi(date=None))
    assert report.by_name("clock_skew").status == SKIP and report.ok


# ----------------------------------------------------------------------------- local checks
def test_config_in_live_mode_fails_and_nothing_gets_armed(tmp_path):
    path = tmp_path / "live.yaml"
    path.write_text(DEFAULT_PATH.read_text().replace("mode: mock", "mode: live"))
    env = {"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND", "WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}
    before = len(A._ARMED_TOKENS)
    report = doctor(FakeReadApi(), config_path=path, env=env)
    assert report.by_name("config").status == FAIL and not report.ok
    assert "--arm-live" in report.by_name("config").detail
    assert len(A._ARMED_TOKENS) == before                     # the doctor never arms
    assert FAKE_TRADE_KEY not in report.render()


def test_broken_config_fails(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(DEFAULT_PATH.read_text().replace("mode: mock", "mode: yolo"))
    assert doctor(FakeReadApi(), config_path=path).by_name("config").status == FAIL


@pytest.mark.parametrize("answers, status", [
    ({"check-ignore": (1, "")}, FAIL),                         # .env not ignored
    ({"ls-files": (0, ".env\n")}, FAIL),                       # .env tracked
    ({"rev-parse": (128, "")}, SKIP),                          # not a checkout
    ({"check-ignore": (128, "")}, SKIP),
])
def test_dotenv_checks(answers, status):
    report = doctor(FakeReadApi(), git=git_answers(**answers))
    assert report.by_name("dotenv_ignored").status == status


def test_git_unavailable_is_a_skip():
    assert doctor(FakeReadApi(), git=lambda args: None).by_name("dotenv_ignored").status == SKIP


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_dotenv_check_with_real_git_on_this_checkout():
    report = run_doctor(Secret(FAKE_READ_KEY), FakeReadApi(), env={}, sleep=lambda s: None,
                        clock=lambda: SERVER_TS)
    status = report.by_name("dotenv_ignored").status
    assert status == (PASS if (DR.REPO_ROOT / ".git").exists() else SKIP)


def test_repo_secret_scan_is_skipped_not_faked():
    check = doctor(FakeReadApi()).by_name("repo_secret_scan")
    assert check.status == SKIP and "test_no_secrets_in_repo" in check.detail


# ----------------------------------------------------------------------------- read-only by construction
def test_get_only_guard_refuses_any_write():
    api = FakeReadApi()
    guarded = DR._get_only(api)
    for method, body in (("POST", b"{}"), ("DELETE", None), ("PUT", b"{}"), ("GET", b"x")):
        with pytest.raises(AuthError):
            guarded(method, "https://api.wallex.ir/v1/account/orders", {}, body, 1.0)
    assert api.calls == []


def test_doctor_source_has_no_write_or_arming_path():
    tree = ast.parse(Path(DR.__file__).read_text())
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert not constants & {"POST", "DELETE", "PUT", "PATCH"}
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not used & {"submit_order", "cancel_order", "WallexBroker", "assert_live_armed",
                       "is_live_armed_token", "TRADE_KEY_ENV"}
