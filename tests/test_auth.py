"""Tests for wallexbot.exchange.auth and the config arming gate (all key material is fake)."""
from __future__ import annotations

import itertools
import ssl
import urllib.request

import pytest

from wallexbot import config as C
from wallexbot import redact as R
from wallexbot.exchange import auth as A
from wallexbot.exchange.auth import (AuthError, LiveNotArmed, SafeRedirectHandler, UrlNotAllowed,
                                     assert_host_allowed, assert_live_armed, build_auth_headers,
                                     safe_opener)
from wallexbot.redact import REDACTED, Secret, redact

FAKE_READ_KEY = "FAKE_read_key_zQxWvB_NotReal_kYjHgF"
FAKE_TRADE_KEY = "FAKE_trade_key_pLmNoQ_NotReal_rStUvW"


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = set(R._SECRETS)
    R._reset_registry()
    yield
    R._reset_registry(saved)


# ---------------------------------------------------------------- host pinning
ALLOWED = [
    "https://api.wallex.ir",
    "https://api.wallex.ir/v1/markets",
    "https://api.wallex.ir:443/v1/account/balances",
    "HTTPS://API.WALLEX.IR/v1/udf/history?symbol=BTCUSDT&resolution=60",
    "https://api.wallex.ir/v1/account/orders?symbol=BTCUSDT#frag",
]
REJECTED = [
    "http://api.wallex.ir/v1/markets",              # not https
    "https://api.wallex.ir.evil.com/v1/markets",    # lookalike suffix
    "https://evil.com/@api.wallex.ir",              # real host is evil.com
    "evil.com/@api.wallex.ir",                      # no scheme
    "https://evil.com/api.wallex.ir",
    "https://evil.com#@api.wallex.ir",
    "https://evil.com?@api.wallex.ir",
    "https://evil.com\\@api.wallex.ir",             # WHATWG reads "\" as "/" -> evil.com
    "https://api.wallex.ir\\@evil.com",
    "https://user:pass@api.wallex.ir/v1",           # userinfo
    "https://user@api.wallex.ir/v1",
    "https://@api.wallex.ir/v1",
    "https://api.wallex.ir@evil.com/v1",
    "https://api.wallex.ir:443@evil.com/v1",
    "https://api.wallex.ir:8443/v1",                # other ports
    "https://api.wallex.ir:0443/v1",
    "https://api.wallex.ir:abc/v1",
    "https://api.wallex.ir./v1",                    # trailing-dot FQDN
    "https://wallex.ir/v1",
    "https://xapi.wallex.ir/v1",
    "https://api-wallex.ir/v1",
    "https://api.wallex.ir%2eevil.com/v1",
    "https://api.wallex.ir\n.evil.com/v1",          # parsers strip \n -> api.wallex.ir.evil.com
    "https://api.wallex.ir\t/v1",
    "https://api.wallex.ir /v1",
    " https://api.wallex.ir/v1",
    "https://api.wallex.ır/v1",                # dotless-i homograph
    "https://api.wallex.ｉｒ/v1",           # fullwidth "ir" (IDNA maps it to ASCII)
    "https://[::1]/v1",
    "https://[api.wallex.ir/v1",
    "https://127.0.0.1/v1",
    "//api.wallex.ir/v1",
    "api.wallex.ir/v1",
    "https:api.wallex.ir/v1",
    "https:///api.wallex.ir/v1",
    "ftp://api.wallex.ir/v1",
    "wss://api.wallex.ir/v1",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "",
    "https://api.wallex.ir/v1/account/withdraw",    # withdraw is never used
    "https://api.wallex.ir/v1/account/crypto-withdrawal",
    "https://api.wallex.ir/v1/account/%77ithdraw",
    "https://api.wallex.ir/v1/WITHDRAW/request",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert assert_host_allowed(url) == url


@pytest.mark.parametrize("url", REJECTED + [None, b"https://api.wallex.ir/v1", 42])
def test_rejected_urls(url):
    with pytest.raises(UrlNotAllowed):
        assert_host_allowed(url)


def test_url_errors_do_not_echo_the_url():
    with pytest.raises(UrlNotAllowed) as err:
        assert_host_allowed("http://api.wallex.ir/v1?api_key=FAKEqueryValue")
    assert "FAKEqueryValue" not in str(err.value)


def test_config_and_auth_pin_the_same_host():
    assert C.ALLOWED_HOSTS == {A.API_HOST}


# ---------------------------------------------------------------- redirects
def _redirect(newurl: str) -> urllib.request.Request:
    headers = dict(build_auth_headers(Secret(FAKE_READ_KEY)))
    req = urllib.request.Request("https://api.wallex.ir/v1/account/balances", headers=headers)
    return SafeRedirectHandler().redirect_request(req, None, 302, "Found", {}, newurl)


@pytest.mark.parametrize("target", ["https://evil.com/steal", "http://api.wallex.ir/v1/x",
                                    "https://api.wallex.ir.evil.com/v1",
                                    "https://api.wallex.ir@evil.com/",
                                    "https://api.wallex.ir/v1/account/withdraw"])
def test_redirects_off_host_are_refused(target):
    with pytest.raises(UrlNotAllowed):
        _redirect(target)


def test_same_host_redirect_is_followed():
    new = _redirect("https://api.wallex.ir/v1/account/balances/")
    assert new.full_url == "https://api.wallex.ir/v1/account/balances/"
    assert new.get_header("X-api-key") == FAKE_READ_KEY


def test_safe_opener_pins_redirects_and_verifies_tls():
    handlers = safe_opener().handlers
    assert any(isinstance(h, SafeRedirectHandler) for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    ctx = https[0]._context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


# ---------------------------------------------------------------- auth headers
def test_build_auth_headers():
    assert dict(build_auth_headers(Secret(FAKE_READ_KEY))) == {"X-API-Key": FAKE_READ_KEY}
    h = build_auth_headers(Secret(FAKE_TRADE_KEY), "FAKE-sub-account-1")
    assert dict(h) == {"X-API-Key": FAKE_TRADE_KEY, "sub-account-client-id": "FAKE-sub-account-1"}
    sub = build_auth_headers(Secret(FAKE_TRADE_KEY), Secret("FAKE-sub-secret"))
    assert sub["sub-account-client-id"] == "FAKE-sub-secret"
    for text in (repr(h), str(h), f"{h}", "%s" % (h,), repr({"headers": h})):
        assert FAKE_TRADE_KEY not in text and "FAKE-sub-account-1" not in text
    assert redact(h) == {"X-API-Key": REDACTED, "sub-account-client-id": REDACTED}


@pytest.mark.parametrize("bad", ["FAKE key with space", "FAKEkey\r\nX-Injected: 1", "FAKEkey\n",
                                 "FAKEkeyé", "FAKE" + "k" * 600, "FAKE\x00key"])
def test_bad_key_values_are_refused_without_echo(bad):
    with pytest.raises(AuthError) as err:
        build_auth_headers(Secret(bad))
    assert bad.strip() not in str(err.value)


def test_build_auth_headers_refuses_raw_str_and_bad_sub_account():
    with pytest.raises(TypeError):
        build_auth_headers(FAKE_READ_KEY)
    for bad in ("", "FAKE sub\r\n", 12345):
        with pytest.raises(AuthError):
            build_auth_headers(Secret(FAKE_READ_KEY), bad)


def test_auth_errors_are_not_os_errors():
    # urllib errors are OSError; a network retry loop catching OSError must not swallow these
    assert not issubclass(AuthError, OSError)


# ---------------------------------------------------------------- live arming
ARM_VALUES = [None, "", "yes", "YES", "yes_i_understand", "YES_I_UNDERSTAND ", " YES_I_UNDERSTAND",
              "YES_I_UNDERSTAND"]
CLI_FLAGS = [False, None, 0, 1, "true", "--arm-live", True]
TRADE_KEYS = [None, "", "   ", "FAKE bad key", FAKE_TRADE_KEY]


@pytest.mark.parametrize("arm, flag, key", list(itertools.product(ARM_VALUES, CLI_FLAGS, TRADE_KEYS)))
def test_arming_matrix(arm, flag, key):
    env = {"WALLEX_API_KEY_READ": FAKE_READ_KEY}
    if arm is not None:
        env["WALLEX_ARM_LIVE"] = arm
    if key is not None:
        env["WALLEX_API_KEY_TRADE"] = key
    if arm == "YES_I_UNDERSTAND" and flag is True and key == FAKE_TRADE_KEY:
        assert assert_live_armed(env, flag) == FAKE_TRADE_KEY
    else:
        with pytest.raises(LiveNotArmed) as err:
            assert_live_armed(env, flag)
        assert FAKE_TRADE_KEY not in str(err.value) and FAKE_READ_KEY not in str(err.value)


def test_arming_error_names_every_missing_condition():
    with pytest.raises(LiveNotArmed) as err:
        assert_live_armed({}, False)
    message = str(err.value)
    assert "WALLEX_ARM_LIVE" in message and "--arm-live" in message
    assert "WALLEX_API_KEY_TRADE" in message


def test_arming_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("WALLEX_ARM_LIVE", "YES_I_UNDERSTAND")
    monkeypatch.setenv("WALLEX_API_KEY_TRADE", FAKE_TRADE_KEY)
    assert assert_live_armed(None, True) == FAKE_TRADE_KEY
    monkeypatch.delenv("WALLEX_API_KEY_TRADE")
    with pytest.raises(LiveNotArmed):
        assert_live_armed(None, True)


def test_armed_trade_key_is_registered_for_redaction():
    env = {"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND", "WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}
    assert_live_armed(env, True)
    assert redact(f"oops {FAKE_TRADE_KEY}") == f"oops {REDACTED}"


# ---------------------------------------------------------------- config integration
def _config_file(tmp_path, *, live=False, extra=""):
    text = C.DEFAULT_PATH.read_text()
    path = tmp_path / "config.yaml"
    path.write_text((text.replace("mode: mock", "mode: live") if live else text) + extra)
    return path


def test_config_live_mode_requires_full_arming(tmp_path):
    path = _config_file(tmp_path, live=True)
    armed_env = {"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND", "WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}
    with pytest.raises(C.ConfigError):
        C.load_config(path, env=armed_env)                                   # no --arm-live
    with pytest.raises(C.ConfigError):
        C.load_config(path, env={"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND"}, cli_armed=True)  # no key
    with pytest.raises(C.ConfigError):
        C.load_config(path, env={"WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}, cli_armed=True)
    assert C.load_config(path, env=armed_env, cli_armed=True).mode == "live"


def test_config_file_must_not_hold_secrets(tmp_path):
    path = _config_file(tmp_path, extra="wallex:\n  api_key: FAKEinlineKeyValue\n")
    with pytest.raises(C.ConfigError) as err:
        C.load_config(path, env={})
    assert "wallex.api_key" in str(err.value) and "FAKEinlineKeyValue" not in str(err.value)
    ok = _config_file(tmp_path, extra="wallex:\n  api_key: ''\n  max_tokens: 5\n  token_bucket: 10\n")
    assert C.load_config(ok, env={}).mode == "mock"
