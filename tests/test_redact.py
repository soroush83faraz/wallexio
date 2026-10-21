"""Tests for wallexbot.redact. All key material here is fake (every key contains FAKE)."""
from __future__ import annotations

import copy
import dataclasses
import io
import json
import logging
import pickle
import random
import string
import time
from collections import OrderedDict, namedtuple
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum

import pytest
import yaml

from wallexbot import redact as R
from wallexbot.redact import (FAILED, REDACTED, RedactingFilter, Secret, is_sensitive_key,
                              load_secret, redact, register_secret)
from wallexbot.types import Side

FAKE_READ_KEY = "FAKE_read_key_zQxWvB_NotReal_kYjHgF"
FAKE_TRADE_KEY = "FAKE_trade_key_pLmNoQ_NotReal_rStUvW"


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = set(R._SECRETS)
    R._reset_registry()
    yield
    R._reset_registry(saved)


def assert_clean(obj, *secrets):
    text = repr(obj)
    for secret in secrets:
        assert secret not in text


# ---------------------------------------------------------------- key-based redaction
SENSITIVE = ["api_key", "X-API-Key", "x_api_key", "apiKey", "WALLEX_API_KEY_READ", "Authorization",
             "Proxy-Authorization", "token", "access_token", "refreshToken", "secret",
             "client_secret", "password", "passwd", "Cookie", "Set-Cookie",
             "sub-account-client-id", "WALLEX_SUB_ACCOUNT_CLIENT_ID", "private_key",
             "credentials", "auth", b"api_key"]
NOT_SENSITIVE = ["symbol", "price", "max_tokens", "token_bucket", "tokens", "client_id", "key",
                 "monkey", "author", "side", None, 42, ("api_key",)]


@pytest.mark.parametrize("name", SENSITIVE)
def test_sensitive_names(name):
    assert is_sensitive_key(name)


@pytest.mark.parametrize("name", NOT_SENSITIVE)
def test_non_sensitive_names(name):
    assert not is_sensitive_key(name)


def test_values_under_sensitive_keys_are_redacted():
    data = {name: f"v-{i}" for i, name in enumerate(SENSITIVE)}
    data.update(symbol="BTCUSDT", max_tokens=1024, client_id="wb_abc", qty=Decimal("0.5"),
                unset_token=None, empty_password="", credentials={"user": "u", "pass": "p"})
    out = redact(data)
    assert all(out[name] == REDACTED for name in SENSITIVE)
    assert out["credentials"] == REDACTED
    assert out["symbol"] == "BTCUSDT" and out["max_tokens"] == 1024 and out["client_id"] == "wb_abc"
    assert out["qty"] == Decimal("0.5") and out["unset_token"] is None and out["empty_password"] == ""


def test_header_pairs_are_redacted():
    pairs = [("X-API-Key", "FAKEpairOne"), ("Accept", "application/json"),
             ["Authorization", "Bearer FAKEpairTwo"]]
    assert redact(pairs) == [("X-API-Key", REDACTED), ("Accept", "application/json"),
                             ["Authorization", REDACTED]]


# ---------------------------------------------------------------- text redaction
@pytest.mark.parametrize("text, leaked", [
    ("X-API-Key: FAKEkvOne", "FAKEkvOne"),
    ("{'api_key': 'FAKE kv two'}", "FAKE kv two"),
    ('{"password": "FAKEkvThree", "max_tokens": 1024}', "FAKEkvThree"),
    ("GET https://api.wallex.ir/v1/x?token=FAKEkvFour&symbol=BTCUSDT", "FAKEkvFour"),
    ("sub-account-client-id=FAKEkvFive", "FAKEkvFive"),
    ("Authorization: Bearer FAKEkvSix.part-two_x=", "FAKEkvSix"),
    ("send: b'GET / HTTP/1.1\\r\\nX-Api-Key: FAKEkvSeven\\r\\n'", "FAKEkvSeven"),
    ("Creds(client_secret='FAKEkvEight', x=1)", "FAKEkvEight"),
    ("url=https://h/?a=1&access_token=FAKEkvNine", "FAKEkvNine"),
])
def test_secret_pairs_in_text(text, leaked):
    out = redact(text)
    assert leaked not in out and REDACTED in out


@pytest.mark.parametrize("text", [
    "symbol=BTCUSDT side=BUY qty=0.5 client_id=wb_abc max_tokens=1024",
    "Content-Type: application/json",
    "token bucket refilled: 3 tokens",
    "price: 65000.1",
    "api_key=[REDACTED] and creds=Secret(****jHgF)",
])
def test_ordinary_text_is_unchanged(text):
    assert redact(text) == text


def test_bearer_tokens_in_free_text():
    out = redact("curl -H 'Authorization: Bearer FAKEbearerAbc.def-ghi_jkl=' ; "
                 "then Bearer FAKEother/tok+en== done")
    assert "FAKEbearer" not in out and "FAKEother" not in out and out.endswith("done")


def test_registered_secret_and_its_fragments():
    register_secret(FAKE_READ_KEY)
    assert redact(f"<{FAKE_READ_KEY}>") == f"<{REDACTED}>"
    assert redact(f"x{FAKE_READ_KEY[5:15]}y") == f"x{REDACTED}y"            # 10-char run
    assert redact(f"[{FAKE_READ_KEY[14:20]}]") == f"[{REDACTED}]"           # exactly 6 chars
    assert redact(f"[{FAKE_READ_KEY[14:19]}]") == f"[{FAKE_READ_KEY[14:19]}]"  # 5: below threshold
    two = f"{FAKE_READ_KEY[:8]} and {FAKE_READ_KEY[-8:]}"
    assert redact(two) == f"{REDACTED} and {REDACTED}"
    register_secret("FAKE5")                                                # short: matched whole
    assert redact("xFAKE5y") == f"x{REDACTED}y"


def test_register_secret_inputs():
    register_secret(None)
    register_secret("")
    register_secret(Secret(FAKE_TRADE_KEY))
    assert redact(FAKE_TRADE_KEY) == REDACTED
    with pytest.raises(TypeError):
        register_secret(12345678)


def test_exception_text_is_scrubbed():
    register_secret(FAKE_TRADE_KEY)
    exc = RuntimeError(f"POST failed: X-API-Key: {FAKE_READ_KEY} / key {FAKE_TRADE_KEY}")
    out = redact(exc)
    assert isinstance(out, str) and out.startswith("RuntimeError: POST failed")
    assert_clean(out, FAKE_READ_KEY, FAKE_TRADE_KEY)
    assert redact({"error": exc}) == {"error": out}

    class Exploding(Exception):
        def __str__(self):
            raise RuntimeError(FAKE_TRADE_KEY)

    assert redact(Exploding()) == f"Exploding: {FAILED}"


def test_nested_structures_keep_shape_and_lose_secrets():
    register_secret(FAKE_TRADE_KEY)
    Pair = namedtuple("Pair", "name value")
    data = {"orders": [{"id": 1, "headers": {"X-API-Key": FAKE_READ_KEY}},
                       ("X-API-Key", FAKE_READ_KEY),
                       {FAKE_TRADE_KEY: "key-as-dict-key"},
                       {f"tag-{FAKE_TRADE_KEY}"},
                       frozenset({FAKE_TRADE_KEY[2:12]}),
                       Pair("note", f"leaked {FAKE_TRADE_KEY}"),
                       b"raw " + FAKE_TRADE_KEY.encode(),
                       bytearray(FAKE_TRADE_KEY.encode())],
            "ok": OrderedDict(symbol="BTCUSDT")}
    out = redact(data)
    assert_clean(out, FAKE_READ_KEY, FAKE_TRADE_KEY, FAKE_TRADE_KEY[2:12])
    orders = out["orders"]
    assert orders[0] == {"id": 1, "headers": {"X-API-Key": REDACTED}}
    assert orders[1] == ("X-API-Key", REDACTED)
    assert orders[2] == {REDACTED: "key-as-dict-key"}
    assert orders[3] == {f"tag-{REDACTED}"} and orders[4] == frozenset({REDACTED})
    assert orders[5] == ("note", f"leaked {REDACTED}") and type(orders[5]) is tuple
    assert orders[6] == b"raw " + REDACTED.encode() and orders[7] == REDACTED.encode()
    assert type(out["ok"]) is dict and out["ok"] == {"symbol": "BTCUSDT"}


def test_input_is_never_mutated():
    register_secret(FAKE_TRADE_KEY)
    inner = {"Authorization": f"Bearer {FAKE_TRADE_KEY}", "list": [FAKE_TRADE_KEY, {"password": "x"}]}
    data = {"inner": inner, "tuple": (inner,), "buf": bytearray(FAKE_TRADE_KEY.encode()),
            "set": {FAKE_TRADE_KEY}, "secret": Secret(FAKE_READ_KEY)}
    before = copy.deepcopy(data)
    out = redact(data)
    assert data == before
    assert out is not data and out["inner"] is not inner and out["inner"]["list"] is not inner["list"]
    assert out["buf"] is not data["buf"] and out["set"] is not data["set"]


def test_scalars_and_str_enums_pass_through():
    assert redact(Side.BUY) is Side.BUY and redact({"side": Side.SELL})["side"] is Side.SELL
    for value in (None, True, 7, 1.5, Decimal("0.123"), 2j):
        assert redact(value) == value
    np = pytest.importorskip("numpy")
    for value in (np.int64(5), np.float64(1.5)):
        assert redact({"qty": value})["qty"] is value       # not turned into a repr string


# ---------------------------------------------------------------- Secret
def test_secret_never_prints_its_value(caplog):
    s = Secret(FAKE_READ_KEY)
    masked = "Secret(****jHgF)"
    for text in (repr(s), str(s), f"{s}", f"{s!r}", f"{s!s}", "%s" % s, "%r" % (s,), "{}".format(s)):
        assert text == masked
    assert f"{s:>20}" == masked.rjust(20) and f"{s:x}" == masked      # bad spec: still masked
    assert repr(Secret("FAKEshort")) == "Secret(****)"                 # short: no tail shown
    assert_clean([s, {"k": (s,)}], FAKE_READ_KEY)
    with caplog.at_level(logging.INFO):
        logging.getLogger("wallexbot.test").info("key=%s %r", s, s)
    assert FAKE_READ_KEY not in caplog.text and masked in caplog.text
    assert s.reveal() == FAKE_READ_KEY
    assert redact(s) == masked and redact({"nested": [s]}) == {"nested": [masked]}


def test_secret_is_not_serialisable_by_accident():
    s = Secret(FAKE_READ_KEY)
    for attempt in (lambda: json.dumps(s), lambda: json.dumps({"k": s}), lambda: "x" + s,
                    lambda: pickle.dumps({"nested": [s]}), lambda: s.__getstate__(),
                    lambda: vars(s), lambda: hash(s), lambda: list(s)):
        with pytest.raises(TypeError):
            attempt()
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(s, protocol=protocol)
    for dump in (yaml.dump, yaml.safe_dump):
        with pytest.raises((TypeError, yaml.YAMLError)):
            dump({"k": s})
    assert json.dumps({"k": s}, default=str) == '{"k": "Secret(****jHgF)"}'
    assert copy.copy(s) is s and copy.deepcopy({"k": s})["k"] is s

    @dataclasses.dataclass
    class Creds:
        key: Secret

    as_dict = dataclasses.asdict(Creds(s))
    assert as_dict["key"] is s
    with pytest.raises(TypeError):
        json.dumps(as_dict)


def test_secret_is_immutable_and_validated():
    s = Secret(FAKE_READ_KEY)
    with pytest.raises(AttributeError):
        s._value = "x"
    with pytest.raises(AttributeError):
        del s._value
    with pytest.raises(TypeError):
        Secret(b"FAKE-bytes")
    with pytest.raises(ValueError):
        Secret("")


def test_secret_equality_is_constant_time(monkeypatch):
    calls = []
    real = R.hmac.compare_digest
    monkeypatch.setattr(R.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    a, b = Secret(FAKE_READ_KEY), Secret(FAKE_TRADE_KEY)
    assert a == Secret(FAKE_READ_KEY) and a != b
    assert a == FAKE_READ_KEY and FAKE_READ_KEY == a and a != "FAKE"
    assert a != 123 and a is not None
    assert len(calls) == 5


def test_load_secret_reads_env_only_and_registers(monkeypatch):
    env = {"WALLEX_API_KEY_READ": f"  {FAKE_READ_KEY}\n", "BLANK": "   ", "EMPTY": ""}
    s = load_secret("WALLEX_API_KEY_READ", env)
    assert isinstance(s, Secret) and s == FAKE_READ_KEY
    for name in ("BLANK", "EMPTY", "MISSING"):
        assert load_secret(name, env) is None
    assert redact(f"oops {FAKE_READ_KEY}") == f"oops {REDACTED}"
    monkeypatch.setenv("WALLEXBOT_TEST_FAKE_SECRET", FAKE_TRADE_KEY)
    assert load_secret("WALLEXBOT_TEST_FAKE_SECRET") == FAKE_TRADE_KEY
    monkeypatch.delenv("WALLEXBOT_TEST_FAKE_SECRET")
    assert load_secret("WALLEXBOT_TEST_FAKE_SECRET") is None


# ---------------------------------------------------------------- logging
def test_redacting_filter_scrubs_message_args_and_traceback():
    register_secret(FAKE_TRADE_KEY)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("wallexbot.test_redacting_filter")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("submit key=%s headers=%r", FAKE_TRADE_KEY, {"X-API-Key": "FAKEhdrValue"})
        try:
            raise ValueError(f"exchange said {FAKE_TRADE_KEY}")
        except ValueError:
            logger.exception("request failed")
        logger.info("bad format %d", "not-a-number")       # must not crash the filter
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert FAKE_TRADE_KEY not in out and "FAKEhdrValue" not in out
    assert "Traceback" in out and "ValueError: exchange said [REDACTED]" in out and FAILED in out


def test_redacting_filter_defeats_formatters_that_rerender_exc_info():
    register_secret(FAKE_TRADE_KEY)

    class Rerender(logging.Formatter):   # ignores the cached exc_text, formats exc_info itself
        def format(self, record):
            extra = self.formatException(record.exc_info) if record.exc_info else ""
            return record.getMessage() + extra

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Rerender())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("wallexbot.test_rerender")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError(f"exchange said {FAKE_TRADE_KEY}")
        except ValueError:
            logger.error("request failed", exc_info=True)
    finally:
        logger.removeHandler(handler)
    assert "request failed" in stream.getvalue() and FAKE_TRADE_KEY not in stream.getvalue()


# ---------------------------------------------------------------- robustness
class BadRepr:
    def __repr__(self):
        raise RuntimeError(FAKE_TRADE_KEY)


class BadMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError(FAKE_TRADE_KEY)

    def __iter__(self):
        raise RuntimeError(FAKE_TRADE_KEY)

    def __len__(self):
        return 1


def _odd_inputs():
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    deep = cur = []
    for _ in range(10_000):
        cur.append([])
        cur = cur[0]
    cur.append(FAKE_TRADE_KEY)
    return [None, True, -1, 2 ** 200, float("nan"), float("inf"), 3j, Decimal("NaN"), "", "\x00",
            "\ud800" + FAKE_TRADE_KEY, b"\xff\xfe" + FAKE_TRADE_KEY.encode(), bytearray(),
            BadRepr(), BadMapping(), cyclic_list, cyclic_dict, deep, object(), type, len,
            Enum("Color", "RED GREEN").RED, memoryview(b"x"), Ellipsis,
            {None: FAKE_TRADE_KEY, (1, "api_key"): FAKE_TRADE_KEY, 3.5: [BadRepr()]},
            [BadRepr(), {"x": BadMapping()}], RuntimeError(BadRepr())]


@pytest.mark.parametrize("obj", _odd_inputs(), ids=lambda o: type(o).__name__)
def test_never_raises_and_never_leaks(obj):
    register_secret(FAKE_TRADE_KEY)
    out = redact(obj)
    assert FAKE_TRADE_KEY not in repr(out)


def test_deep_nesting_keeps_shape_and_is_cut_at_max_depth():
    deep = cur = []
    for _ in range(5_000):
        cur.append([])
        cur = cur[0]
    out, depth = redact(deep), 0
    while isinstance(out, list):
        out, depth = out[0], depth + 1
    assert out == "[MAX_DEPTH]" and depth == R._MAX_DEPTH


def test_generators_are_not_consumed():
    gen = (x for x in [FAKE_TRADE_KEY, "b"])
    assert isinstance(redact(gen), str)
    assert list(gen) == [FAKE_TRADE_KEY, "b"]


def test_internal_failure_returns_placeholder_not_input(monkeypatch):
    def boom(text):
        raise RuntimeError("internal")

    monkeypatch.setattr(R, "_scrub_text", boom)
    assert redact(f"key {FAKE_TRADE_KEY}") == FAILED
    assert redact({"a": ["b"]}) == FAILED


def test_random_fuzz_never_raises_or_leaks():
    register_secret(FAKE_TRADE_KEY)
    rng = random.Random(20261021)
    alphabet = string.printable + "\x00​\ud800é\U0001f600=:'\"&"

    def text():
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        if rng.random() < 0.5:
            cut = rng.randint(0, len(s))
            s = s[:cut] + FAKE_TRADE_KEY + s[cut:]
        return s

    def obj(depth=0):
        r = rng.random()
        if depth > 4 or r < 0.35:
            return rng.choice([text, lambda: text().encode("utf-8", "surrogatepass"),
                               lambda: rng.randint(-5, 5), lambda: None])()
        if r < 0.55:
            return {text(): obj(depth + 1) for _ in range(rng.randint(0, 4))}
        if r < 0.75:
            return [obj(depth + 1) for _ in range(rng.randint(0, 4))]
        if r < 0.85:
            return tuple(obj(depth + 1) for _ in range(rng.randint(0, 3)))
        if r < 0.95:
            return RuntimeError(text())
        return Secret(FAKE_TRADE_KEY)

    for _ in range(500):
        assert FAKE_TRADE_KEY not in repr(redact(obj()))


def test_redaction_is_fast_and_linear():
    register_secret(FAKE_READ_KEY)
    register_secret(FAKE_TRADE_KEY)
    records = [{"symbol": "BTCUSDT", "side": "BUY", "qty": "0.0123", "price": "65000.1",
                "client_id": f"wb_{i:032x}"} for i in range(10_000)]
    pathological = ["lorem ipsum " * 50_000, "keykey" * 50_000, "api_key" + " " * 100_000,
                    '"' * 100_000, "a=" * 100_000, 'token: "' * 30_000, "api_key=x " * 30_000,
                    "A" * 500_000, "a=b=" * 50_000, "url=" + "x/?y=" * 50_000]
    start = time.perf_counter()
    redact(records)
    for text in pathological:
        redact(text)
    assert time.perf_counter() - start < 5.0
