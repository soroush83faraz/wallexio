"""Tests for wallexbot.redact. All key material here is fake (every key contains FAKE)."""
from __future__ import annotations

import base64
import copy
import dataclasses
import datetime as dt
import io
import json
import logging
import numbers
import pickle
import random
import re
import string
import sys
import time
import urllib.parse
import uuid
from collections import OrderedDict, namedtuple
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum, IntEnum
from fractions import Fraction

import pytest
import yaml

from wallexbot import redact as R
from wallexbot.redact import (FAILED, MIN_FRAGMENT, REDACTED, RedactingFilter, Secret,
                              is_sensitive_key, load_secret, redact, register_secret)
from wallexbot.types import Side

FAKE_READ_KEY = "FAKE_read_key_zQxWvB_NotReal_kYjHgF"
FAKE_TRADE_KEY = "FAKE_trade_key_pLmNoQ_NotReal_rStUvW"
FAKE_URL_KEY = "FAKE+url/key=with~reserved chars_TEST"     # has chars that URL-encoding escapes
FAKE_FA_KEY = "FAKE_TEST_رمزعبور_EXAMPLE"                   # Persian letters: NUL-free UTF-16


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


PLAIN_SCALARS = (type(None), bool, int, float, complex, Decimal, Fraction, dt.date, dt.datetime,
                 dt.time, dt.timedelta, uuid.UUID)


def plain_content(obj):
    """Yield every string an output holds, asserting it is built only of plain builtins (whose
    repr/str cannot lie, unlike a subclass's)."""
    kind = type(obj)
    np = sys.modules.get("numpy")
    if kind is str:
        yield obj
    elif kind is bytes:
        yield obj.decode("latin-1")
    elif kind in (list, tuple, set, frozenset):
        for item in obj:
            yield from plain_content(item)
    elif kind is dict:
        for key, value in obj.items():
            yield from plain_content(key)
            yield from plain_content(value)
    else:
        numpy_own = np is not None and getattr(np, kind.__name__, None) is kind
        assert kind in PLAIN_SCALARS or numpy_own, f"non-plain output {kind}"
        yield repr(obj)


def assert_plain_and_clean(out, *secrets):
    text = "\n".join(plain_content(out))
    for secret in secrets:
        assert secret not in text and not leaks(text, secret)


def recoverable(text: str) -> list[bytes]:
    """Byte strings recoverable from ``text``: as-is, URL-decoded, every hex or base64 run decoded
    at every offset, and with UTF-16 NULs (raw or ``\\x00``-escaped) dropped."""
    raw = text.encode("utf-8", "surrogatepass")
    found = [raw, urllib.parse.unquote_to_bytes(raw), urllib.parse.unquote_to_bytes(raw.replace(b"+", b" ")),
             text.replace("\\x00", "").replace("\x00", "").encode("utf-8", "surrogatepass")]
    for run in re.findall(r"[0-9A-Fa-f]{2,}", text):
        for offset in (0, 1):
            chunk = run[offset:]
            found.append(bytes.fromhex(chunk[:len(chunk) // 2 * 2]))
    for run in re.findall(r"[A-Za-z0-9+/_-]{4,}", text):
        run = run.translate(str.maketrans("-_", "+/"))
        for offset in range(4):
            chunk = run[offset:]
            found.append(base64.b64decode(chunk[:len(chunk) // 4 * 4]))
    return found


def leaks(text: str, secret: str) -> bool:
    """True if a MIN_FRAGMENT-byte run of ``secret`` (UTF-8) can be recovered from ``text``."""
    data = secret.encode()
    runs = {data[i:i + MIN_FRAGMENT] for i in range(len(data) - MIN_FRAGMENT + 1)}
    return any(run in blob for blob in recoverable(text) for run in runs)


# ---------------------------------------------------------------- hostile subclasses
class AlwaysEqualStr(str):
    """Equal to everything: the old ``obj if out == obj`` check returned it, secret and all."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class LyingStr(str):
    """Every str method the scrubber could call lies about the stored text."""

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __getitem__(self, item):
        return ""

    def __iter__(self):
        return iter(())

    def __contains__(self, item):
        return False

    def find(self, *args):
        return -1

    def lower(self):
        return "symbol"

    def encode(self, *args, **kwargs):
        return b""

    def __str__(self):
        return "symbol"

    def __repr__(self):
        return "'symbol'"

    def __format__(self, spec):
        return "symbol"


class CleanFace(bytes):
    """Shows clean bytes to bytes()/decode()/== while storing the secret."""

    def __bytes__(self):
        return b"clean"

    def decode(self, *args, **kwargs):
        return "clean"

    def __eq__(self, other):
        return True

    __hash__ = bytes.__hash__


class CleanFaceArray(bytearray):
    def __bytes__(self):
        return b"clean"

    def decode(self, *args, **kwargs):
        return "clean"


class LoudInt(int):
    def __repr__(self):
        return FAKE_TRADE_KEY

    __str__ = __repr__

    def __format__(self, spec):
        return FAKE_TRADE_KEY


class LoudFloat(float):
    def __repr__(self):
        return FAKE_TRADE_KEY

    __str__ = __repr__

    def __format__(self, spec):
        return FAKE_TRADE_KEY


class Level(IntEnum):
    HIGH = 3


# ---------------------------------------------------------------- key-based redaction
SENSITIVE = ["api_key", "X-API-Key", "x_api_key", "apiKey", "WALLEX_API_KEY_READ", "Authorization",
             "Proxy-Authorization", "token", "access_token", "refreshToken", "secret",
             "client_secret", "password", "passwd", "Cookie", "Set-Cookie",
             "sub-account-client-id", "WALLEX_SUB_ACCOUNT_CLIENT_ID", "private_key",
             "credentials", "auth", b"api_key", bytearray(b"X-API-Key")]
NOT_SENSITIVE = ["symbol", "price", "max_tokens", "token_bucket", "tokens", "client_id", "key",
                 "monkey", "author", "side", None, 42, ("api_key",)]


@pytest.mark.parametrize("name", SENSITIVE)
def test_sensitive_names(name):
    assert is_sensitive_key(name)


@pytest.mark.parametrize("name", NOT_SENSITIVE)
def test_non_sensitive_names(name):
    assert not is_sensitive_key(name)


def test_values_under_sensitive_keys_are_redacted():
    names = [n for n in SENSITIVE if not isinstance(n, bytearray)]     # bytearray is unhashable
    data = {name: f"v-{i}" for i, name in enumerate(names)}
    data.update(symbol="BTCUSDT", max_tokens=1024, client_id="wb_abc", qty=Decimal("0.5"),
                unset_token=None, empty_password="", credentials={"user": "u", "pass": "p"})
    out = redact(data)
    assert all(out[name] == REDACTED for name in names)
    assert out["credentials"] == REDACTED
    assert out["symbol"] == "BTCUSDT" and out["max_tokens"] == 1024 and out["client_id"] == "wb_abc"
    assert out["qty"] == Decimal("0.5") and out["unset_token"] is None and out["empty_password"] == ""


def test_header_pairs_are_redacted():
    pairs = [("X-API-Key", "FAKEpairOne"), ("Accept", "application/json"),
             ["Authorization", "Bearer FAKEpairTwo"], (bytearray(b"Cookie"), "FAKEpairThree")]
    assert redact(pairs) == [("X-API-Key", REDACTED), ("Accept", "application/json"),
                             ["Authorization", REDACTED], (b"Cookie", REDACTED)]


def test_subclass_methods_cannot_unmark_sensitive_keys_pairs_or_values():
    value = "FAKEunregisteredSubclassValue1"     # not registered: only key-based redaction applies

    class LowerLies(str):
        def lower(self):
            return "symbol"

    class ClaimsBlank(str):
        def __len__(self):
            return 0

        def __bool__(self):
            return False

    class HidesPairLength(tuple):
        def __len__(self):
            return 3

    R._sensitive_name.cache_clear()          # a cached exact "api_key" would hide the bug
    try:
        assert is_sensitive_key(LowerLies("api_key"))
        assert is_sensitive_key("api_key")   # ...and the shared lookup cache was not poisoned
        assert redact({LowerLies("api_key"): value}) == {"api_key": REDACTED}
    finally:
        R._sensitive_name.cache_clear()
    assert redact({"api_key": ClaimsBlank(value)}) == {"api_key": REDACTED}
    assert redact([HidesPairLength(("X-API-Key", value))]) == [("X-API-Key", REDACTED)]
    assert redact({"api_key": ClaimsBlank("")}) == {"api_key": ""}      # really blank: kept


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


def test_ordinary_text_is_unchanged_with_secrets_registered():
    for secret in (FAKE_READ_KEY, FAKE_TRADE_KEY, FAKE_URL_KEY, FAKE_FA_KEY):
        register_secret(secret)
    text = ("symbol=BTCUSDT side=BUY qty=0.5 client_id=wb_0123456789abcdef0123456789abcdef "
            "sig=9f86d081884c7d659a2feaa0c55ad015 id=550e8400-e29b-41d4-a716-446655440000 "
            "GET /v1/depth?symbol=BTC%2FUSDT&limit=50 note=سلام دنیا b'\\x00\\x01OK'")
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
    register_secret(LyingStr(FAKE_READ_KEY))       # the stored text is registered, not its lies
    assert redact(f"<{FAKE_READ_KEY[2:12]}>") == f"<{REDACTED}>"
    assert all(type(s) is str for s in R._SECRETS)


@pytest.mark.parametrize("prefix", [b"", b"u", b"us", b"user:", b"user:x", b"user:xy"])
def test_base64_of_a_registered_secret_is_scrubbed_at_every_alignment(prefix):
    register_secret(FAKE_TRADE_KEY)
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        blob = encode(prefix + FAKE_TRADE_KEY.encode() + b"!tail").decode()
        assert leaks(blob, FAKE_TRADE_KEY)                    # the oracle does see it
        for text in (f"Basic {blob}", blob.rstrip("=")):
            for out in (redact(text), redact(text.encode()).decode()):
                assert REDACTED in out and not leaks(out, FAKE_TRADE_KEY), text


def test_hex_and_url_encoded_registered_secrets_are_scrubbed():
    register_secret(FAKE_TRADE_KEY)
    register_secret(FAKE_URL_KEY)
    data = FAKE_TRADE_KEY.encode()
    cases = [(f"sig={data.hex()}", FAKE_TRADE_KEY), (f"SIG {data.hex().upper()}", FAKE_TRADE_KEY),
             (f"frag={data[3:20].hex()}z", FAKE_TRADE_KEY)]
    quote, quote_plus = urllib.parse.quote, urllib.parse.quote_plus
    for q in (quote(FAKE_URL_KEY, safe=""), quote(FAKE_URL_KEY), quote_plus(FAKE_URL_KEY),
              re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), quote(FAKE_URL_KEY, safe=""))):
        cases.append((f"GET /v1/x?a=1&p={q}&b=2", FAKE_URL_KEY))
    for text, secret in cases:
        assert leaks(text, secret), text
        out = redact(text)
        assert REDACTED in out and not leaks(out, secret), text


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-16"])
def test_utf16_secret_in_str_views_of_bytes_is_scrubbed(codec):
    """store._jsonable() turns bytes into str(bytes) before redact(): the escaped form counts."""
    register_secret(FAKE_TRADE_KEY)
    for prefix in (b"", b"k", b"k="):
        raw = prefix + FAKE_TRADE_KEY.encode(codec)
        for text in (str(raw), repr({"body": raw}), raw.decode("latin-1"),
                     raw.decode("utf-8", "replace")):
            out = redact(text)
            assert REDACTED in out and not leaks(out, FAKE_TRADE_KEY), text


def test_non_ascii_secret_in_escaped_and_mojibake_text_is_scrubbed():
    register_secret(FAKE_FA_KEY)
    assert redact(f"x {FAKE_FA_KEY} y") == f"x {REDACTED} y"
    assert redact(str(FAKE_FA_KEY.encode())) == f"b'{REDACTED}'"           # \xd8\xb1... escapes
    assert redact(FAKE_FA_KEY.encode().decode("latin-1")) == REDACTED       # UTF-8 read as latin-1


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


def test_empty_values_round_trip():
    for value in ("", b"", (), frozenset(), [], {}, set()):    # () is a singleton: allowed back
        out = redact(value)
        assert out == value and type(out) is type(value)
    assert redact(bytearray()) == b"" and redact([(), [()]]) == [(), [()]]


def test_exact_scalars_pass_through_and_enums_become_plain_values():
    for value in (None, True, 7, 2 ** 70, 1.5, Decimal("0.123"), 2j, Fraction(1, 3),
                  dt.date(2026, 1, 2), dt.datetime(2026, 1, 2, 3, 4), dt.time(5, 6),
                  dt.timedelta(seconds=7), uuid.UUID(int=8)):
        assert redact(value) is value
    for member, plain in ((Side.BUY, "BUY"), (Level.HIGH, 3)):    # str / int subclasses
        out = redact(member)
        assert out == plain and type(out) is type(plain)
    out = redact({"side": Side.SELL})
    assert out == {"side": "SELL"} and type(out["side"]) is str
    np = pytest.importorskip("numpy")
    for value in (np.int64(5), np.float64(1.5), np.bool_(True), np.uint8(3)):
        assert redact({"qty": value})["qty"] is value       # numpy's own numbers, not a repr string


# ---------------------------------------------------------------- hostile inputs (subclasses)
@pytest.mark.parametrize("cls", [AlwaysEqualStr, LyingStr])
def test_str_subclasses_come_back_as_new_plain_scrubbed_str(cls):
    register_secret(FAKE_TRADE_KEY)
    obj = cls(f"key {FAKE_TRADE_KEY}")
    for out in (redact(obj), redact([obj])[0], redact({"k": obj})["k"], redact((obj,))[0]):
        assert type(out) is str and out is not obj and str.__eq__(out, f"key {REDACTED}")
    out = redact(cls("nothing secret"))
    assert type(out) is str and str.__eq__(out, "nothing secret")


def test_bytes_subclasses_are_read_from_their_storage():
    register_secret(FAKE_TRADE_KEY)
    for obj in (CleanFace(FAKE_TRADE_KEY.encode()), CleanFaceArray(FAKE_TRADE_KEY.encode())):
        out = redact(obj)
        assert type(out) is bytes and out is not obj and bytes.__eq__(out, REDACTED.encode())


def test_container_subclasses_are_read_from_their_storage():
    value = "FAKEunregisteredSubclassValue2"

    class HidesItems(dict):
        def items(self):
            return iter([("symbol", "BTCUSDT")])

        def __iter__(self):
            return iter(["symbol"])

        def __getitem__(self, key):
            return "BTCUSDT"

    class HidesElems(list):
        def __iter__(self):
            return iter(["ok"])

        def __len__(self):
            return 1

    class HidesMembers(frozenset):
        def __iter__(self):
            return iter(())

    out = redact(HidesItems(api_key=value, symbol="BTCUSDT"))
    assert type(out) is dict and out == {"api_key": REDACTED, "symbol": "BTCUSDT"}
    out = redact(HidesElems(["Authorization", value]))
    assert type(out) is list and out == ["Authorization", REDACTED]
    register_secret(value)
    out = redact(HidesMembers({value, "ok"}))
    assert type(out) is frozenset and out == frozenset({REDACTED, "ok"})


def test_scalar_subclasses_and_number_registrants_are_not_passed_through():
    for obj, plain in ((LoudInt(7), 7), (LoudFloat(1.5), 1.5)):
        out = redact(obj)
        assert out == plain and type(out) is type(plain) and FAKE_TRADE_KEY not in f"{out}{out!r}"

    class Registered:              # claims to be a number: the old code passed it through as-is
        def __repr__(self):
            return "Registered(api_key='FAKEregisteredValue1')"

    numbers.Number.register(Registered)
    assert redact(Registered()) == f"Registered(api_key='{REDACTED}')"

    class ClaimsToBeInt(type):     # a metaclass equal to (and hashing like) every type
        def __eq__(cls, other):
            return True

        def __hash__(cls):
            return hash(int)

    class Impostor(metaclass=ClaimsToBeInt):
        def __repr__(self):
            return "Impostor(api_key='FAKEimpostorValue1')"

    assert redact(Impostor()) == f"Impostor(api_key='{REDACTED}')"
    np = pytest.importorskip("numpy")

    class LoudFloat64(np.float64):
        def __repr__(self):
            return FAKE_TRADE_KEY

    out = redact(LoudFloat64(2.5))
    assert out == 2.5 and type(out) is float


def test_repr_and_str_returning_str_subclasses_are_normalised():
    register_secret(FAKE_TRADE_KEY)

    class Odd:
        def __repr__(self):
            return LyingStr(f"Odd({FAKE_TRADE_KEY})")

    class OddError(Exception):
        def __str__(self):
            return LyingStr(f"failed with {FAKE_TRADE_KEY}")

    out = redact(Odd())
    assert type(out) is str and str.__eq__(out, f"Odd({REDACTED})")
    out = redact(OddError())
    assert type(out) is str and str.__eq__(out, f"OddError: failed with {REDACTED}")


def test_control_flow_exceptions_from_untrusted_code_carry_no_payload():
    class Exits:
        def __init__(self, code):
            self.code = code

        def __repr__(self):
            raise SystemExit(self.code)

    class Interrupts:
        def __repr__(self):
            try:
                raise RuntimeError(FAKE_TRADE_KEY)
            except RuntimeError:
                raise KeyboardInterrupt(FAKE_TRADE_KEY)

    class Cancelled(BaseException):
        pass

    class RaisesCancelled:
        def __repr__(self):
            raise Cancelled(FAKE_TRADE_KEY)

    for code, expected in ((f"bye {FAKE_TRADE_KEY}", 1), (3, 3), (None, None)):
        with pytest.raises(SystemExit) as caught:
            redact({"x": [Exits(code)]})
        assert caught.value.code == expected and caught.value.__suppress_context__
        assert caught.value.__context__ is None and caught.value.__cause__ is None
    with pytest.raises(KeyboardInterrupt) as caught:
        redact([Interrupts()])
    assert caught.value.args == () and caught.value.__context__ is None
    assert redact(RaisesCancelled()) == FAILED


# ---------------------------------------------------------------- bytes policy
NOT_PROVABLY_CLEAN = {
    "utf-16 with BOM": FAKE_TRADE_KEY.encode("utf-16"),
    "utf-16-le": b"k=" + FAKE_TRADE_KEY.encode("utf-16-le"),
    "utf-16-be at odd offset": b"k" + FAKE_TRADE_KEY.encode("utf-16-be"),
    "utf-32": FAKE_TRADE_KEY.encode("utf-32-le"),
    "latin-1 text": ("café " + FAKE_TRADE_KEY).encode("latin-1"),
    "binary": b"\xff\xfe\x00\x01" + FAKE_TRADE_KEY.encode(),
    "unregistered bearer in utf-16": "Authorization: Bearer FAKEutf16Bearer1".encode("utf-16-le"),
    "no secret at all": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
}


@pytest.mark.parametrize("name", NOT_PROVABLY_CLEAN)
def test_bytes_that_cannot_be_proven_clean_are_replaced_whole(name):
    register_secret(FAKE_TRADE_KEY)
    payload = NOT_PROVABLY_CLEAN[name]
    for obj in (payload, bytearray(payload), CleanFace(payload)):
        out = redact(obj)
        assert type(out) is bytes and bytes.__eq__(out, b"[BYTES len=%d]" % len(payload))


def test_utf8_bytes_are_scrubbed_as_text_including_encoded_forms():
    register_secret(FAKE_TRADE_KEY)
    data = FAKE_TRADE_KEY.encode()
    body = json.dumps({"note": base64.b64encode(b"u:" + data).decode(), "sig": data.hex(),
                       "api_key": "FAKEjsonValue1", "fa": "سلام", "raw": f"x{FAKE_TRADE_KEY}y"},
                      ensure_ascii=False).encode()
    for obj in (body, bytearray(body), CleanFace(body)):
        out = redact(obj)
        assert type(out) is bytes and out is not obj
        text = out.decode()
        assert "سلام" in text and "FAKEjsonValue1" not in text and not leaks(text, FAKE_TRADE_KEY)
    assert redact(b"") == b"" and redact(bytearray(b"ok")) == b"ok"


def test_non_ascii_secret_hidden_in_valid_utf8_bytes_is_caught():
    register_secret(FAKE_FA_KEY)
    hidden = "رمزعبور".encode("utf-16-le")
    assert b"\0" not in hidden and hidden.decode("utf-8")      # looks like ordinary text
    payload = b"note=" + hidden + b";"
    assert redact(payload) == b"[BYTES len=%d]" % len(payload)
    assert redact(f"x {FAKE_FA_KEY} y".encode()) == f"x {REDACTED} y".encode()


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


def test_secret_is_immutable_validated_and_final():
    s = Secret(FAKE_READ_KEY)
    with pytest.raises(AttributeError):
        s._value = "x"
    with pytest.raises(AttributeError):
        del s._value
    with pytest.raises(TypeError):
        Secret(b"FAKE-bytes")
    with pytest.raises(ValueError):
        Secret("")
    with pytest.raises(TypeError):
        class Unmasked(Secret):        # an override could print the value
            def __repr__(self):
                return self.reveal()
    lying = Secret(LyingStr(FAKE_TRADE_KEY))           # stores the text, not the subclass
    assert type(lying.reveal()) is str and lying.reveal() == FAKE_TRADE_KEY and lying == FAKE_TRADE_KEY


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
    env = {"WALLEX_API_KEY_READ": f"  {FAKE_READ_KEY}\n", "BLANK": "   ", "EMPTY": "",
           "LYING": LyingStr(f" {FAKE_TRADE_KEY} ")}
    s = load_secret("WALLEX_API_KEY_READ", env)
    assert isinstance(s, Secret) and s == FAKE_READ_KEY
    for name in ("BLANK", "EMPTY", "MISSING"):
        assert load_secret(name, env) is None
    assert load_secret("LYING", env).reveal() == FAKE_TRADE_KEY
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


def test_redacting_filter_scrubs_extra_fields():
    register_secret(FAKE_TRADE_KEY)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s|%(api_key)s|%(note)s|%(attempt)d|%(creds)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("wallexbot.test_extra_fields")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("order sent", extra={"api_key": "FAKEextraValue1", "note": f"key {FAKE_TRADE_KEY}",
                                         "attempt": 2, "creds": Secret(FAKE_READ_KEY)})
    finally:
        logger.removeHandler(handler)
    assert stream.getvalue() == f"order sent|{REDACTED}|key {REDACTED}|2|Secret(****jHgF)\n"


def test_redacting_filter_scrubs_every_text_field_of_a_record():
    register_secret(FAKE_TRADE_KEY)
    try:
        raise ValueError(f"exchange said {FAKE_TRADE_KEY}")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("wallexbot.test", logging.ERROR, __file__, 1, "key=%s",
                               (FAKE_TRADE_KEY,), exc_info, sinfo=f"Stack:\n  {FAKE_TRADE_KEY}")
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == f"key={REDACTED}" and record.args is None and record.exc_info is None
    assert f"ValueError: exchange said {REDACTED}" in record.exc_text
    assert record.stack_info == f"Stack:\n  {REDACTED}"
    assert FAKE_TRADE_KEY not in logging.Formatter().format(record)


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
            [BadRepr(), {"x": BadMapping()}], RuntimeError(BadRepr()),
            AlwaysEqualStr(f"x {FAKE_TRADE_KEY}"), LyingStr(FAKE_TRADE_KEY), LoudInt(1),
            CleanFace(FAKE_TRADE_KEY.encode()), CleanFaceArray(FAKE_TRADE_KEY.encode()),
            {AlwaysEqualStr("api_key"): FAKE_TRADE_KEY}, FAKE_TRADE_KEY.encode("utf-16-le")]


@pytest.mark.parametrize("obj", _odd_inputs(), ids=lambda o: type(o).__name__)
def test_never_raises_and_never_leaks(obj):
    register_secret(FAKE_TRADE_KEY)
    out = redact(obj)
    assert_plain_and_clean(out, FAKE_TRADE_KEY)


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


def test_a_pass_through_regression_is_caught_by_the_final_check(monkeypatch):
    monkeypatch.setattr(R, "_redact", lambda obj, depth, active: obj)
    for obj in (AlwaysEqualStr("x"), CleanFace(b"x"), LoudInt(1), [1], {"a": 1}, ("x",), [], object()):
        assert redact(obj) is FAILED
    for obj in ("x", b"x", 1, None, Decimal("1"), ()):
        assert redact(obj) is obj


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

    def leaf():
        s = text()
        raw = s.encode("utf-8", "surrogatepass")
        return rng.choice([lambda: s, lambda: raw, lambda: AlwaysEqualStr(s), lambda: LyingStr(s),
                           lambda: CleanFace(raw), lambda: base64.b64encode(raw).decode(),
                           lambda: raw.hex(), lambda: str(s.encode("utf-16-le", "surrogatepass")),
                           lambda: rng.randint(-5, 5), lambda: None])()

    def obj(depth=0):
        r = rng.random()
        if depth > 4 or r < 0.35:
            return leaf()
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
        content = "\n".join(plain_content(redact(obj())))
        assert FAKE_TRADE_KEY not in content and not leaks(content, FAKE_TRADE_KEY)


def test_redaction_is_fast_and_linear():
    for secret in (FAKE_READ_KEY, FAKE_TRADE_KEY, FAKE_URL_KEY, FAKE_FA_KEY):
        register_secret(secret)
    records = [{"symbol": "BTCUSDT", "side": "BUY", "qty": "0.0123", "price": "65000.1",
                "client_id": f"wb_{i:032x}"} for i in range(10_000)]
    pathological = ["lorem ipsum " * 50_000, "keykey" * 50_000, "api_key" + " " * 100_000,
                    '"' * 100_000, "a=" * 100_000, 'token: "' * 30_000, "api_key=x " * 30_000,
                    "A" * 500_000, "a=b=" * 50_000, "url=" + "x/?y=" * 50_000,
                    "\x00F" * 200_000, "رم" * 200_000]
    start = time.perf_counter()
    redact(records)
    for text in pathological:
        redact(text)
        redact(text.encode("utf-8", "surrogatepass"))
    assert time.perf_counter() - start < 5.0
