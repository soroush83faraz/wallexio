"""Secret handling: a self-masking ``Secret`` wrapper and a never-raising ``redact()``.

    from wallexbot.redact import Secret, load_secret, redact
    key = load_secret("WALLEX_API_KEY_READ")   # Secret | None; read from env only, registered
    log.info("state=%s", redact(state))        # scrub before logging, persisting or AI prompts

redact(obj) returns a NEW object (input is never mutated) with:
  * values under secret-like keys (api_key, x-api-key, authorization, *token, secret, password,
    cookie, sub-account-client-id, credential, private_key) replaced by REDACTED, in mappings,
    ``(name, value)`` pairs and ``name=value`` / ``name: value`` text;
  * ``Bearer <token>`` removed from text;
  * every registered secret, and any run of >= MIN_FRAGMENT chars of one, removed from text,
    also when hex-, base64- (standard or URL-safe, any byte alignment) or URL-encoded, or held
    as UTF-16 bytes (NUL-interleaved, or ``\\x00``-escaped as ``str(some_bytes)`` prints it).
Input is untrusted. Only its real type (``type(obj)``, never ``obj.__class__``) is consulted, and
str/bytes/container subclasses are read from their storage (``str.__str__``, ``bytes.__bytes__``,
``dict.items`` ...), so overridden ``__eq__``/``__len__``/``__iter__``/``items``/``lower`` never
run. Only plain builtins come out: str, bytes, dict (from any Mapping), list, tuple, set,
frozenset; exact None/bool/int/float/complex/Decimal/Fraction/date/datetime/time/timedelta/UUID
and numpy's own number types pass through as-is, int/float/complex subclasses (IntEnum) become
their plain value, and str subclasses (str enums) their plain str. bytes/bytearray become new
bytes: NUL-free strict UTF-8 is scrubbed as text; anything else (binary, UTF-16/32, latin-1, or
UTF-8 hiding a registered secret in another encoding) cannot be proven clean and is replaced by
``b"[BYTES len=N]"`` (fail closed). Exceptions become a redacted ``"Type: message"`` str, a Secret
its masked repr, and any other object a redacted ``repr()`` str. It never raises: on internal
failure, or an exception from the input's own methods, it returns FAILED, never the input (a
KeyboardInterrupt/SystemExit is re-raised bare, without its payload). ``RedactingFilter``
applies it to log records, including ``extra=`` fields.
"""
from __future__ import annotations

import base64
import datetime as _dt
import functools
import hmac
import logging
import os
import re
import sys
import threading
import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any, NamedTuple, final
from urllib.parse import quote, quote_plus

__all__ = ["FAILED", "MIN_FRAGMENT", "REDACTED", "RedactingFilter", "Secret",
           "is_sensitive_key", "load_secret", "redact", "register_secret"]

REDACTED = "[REDACTED]"
FAILED = "[REDACTION_FAILED]"
MIN_FRAGMENT = 6     # a leaked run of >= 6 chars of a registered secret is scrubbed
_MAX_DEPTH = 32      # deeper content is replaced, which also bounds recursion
# returned as-is: exact types only, as a subclass can override __str__/__repr__/__format__.
# Looked up by id(): ``kind in {types}`` would run a metaclass's own __eq__/__hash__.
_EXACT_SCALARS = (type(None), bool, int, float, complex, Decimal, Fraction, _dt.date,
                  _dt.datetime, _dt.time, _dt.timedelta, uuid.UUID)
_EXACT_SCALAR_IDS = frozenset(map(id, _EXACT_SCALARS))
# a subclass of these comes back as its plain value, read by the base type's own slot
_SCALAR_BASES = ((int, int.__int__), (float, float.__float__), (complex, complex.__complex__))
_SEQUENCES = (list, tuple, set, frozenset)


# --------------------------------------------------------------------------- registry
class _Matcher(NamedTuple):
    heads: dict[str, tuple[str, ...]]   # needle[:MIN_FRAGMENT] -> needles of >= MIN_FRAGMENT chars
    whole: tuple[str, ...]              # shorter needles (short secrets), searched for whole


_LOCK = threading.Lock()
_SECRETS: set[str] = set()
# (text forms of every registered secret, non-UTF-8 byte forms as latin-1 text); swapped
# atomically on registration and read without the lock.
_MATCHERS: tuple[_Matcher, _Matcher] = (_Matcher({}, ()), _Matcher({}, ()))
_PCT_ESCAPE = re.compile(r"%[0-9A-F]{2}")
_URLSAFE = str.maketrans("+/", "-_")


def register_secret(value: str | Secret | None) -> None:
    """Scrub ``value``, its >= MIN_FRAGMENT-char runs and their encoded forms (hex, base64,
    URL-encoding, UTF-16 bytes) from future redact() output.

    Values shorter than MIN_FRAGMENT are matched whole. None/"" are ignored.
    """
    if type(value) is Secret:              # Secret cannot be subclassed: this is Secret.reveal
        value = value.reveal()
    if _is_blank(value):
        return
    if not issubclass(type(value), str):
        raise TypeError("register_secret expects a str or Secret")
    value = _plain(value)
    with _LOCK:
        if value not in _SECRETS:
            _SECRETS.add(value)
            _rebuild()


def _rebuild() -> None:
    global _MATCHERS
    text: set[str] = set()
    raw: set[str] = set()
    for secret in _SECRETS:
        text |= _text_forms(secret)
        raw |= _byte_forms(secret)
    _MATCHERS = (_matcher(text), _matcher(raw))


def _matcher(needles: set[str]) -> _Matcher:
    heads: dict[str, list[str]] = {}
    for needle in needles:
        if len(needle) >= MIN_FRAGMENT:
            heads.setdefault(needle[:MIN_FRAGMENT], []).append(needle)
    return _Matcher({head: tuple(group) for head, group in heads.items()},
                    tuple(n for n in needles if 0 < len(n) < MIN_FRAGMENT))


def _runs(secret: str) -> list[str]:
    n = MIN_FRAGMENT
    return [secret[i:i + n] for i in range(len(secret) - n + 1)] or [secret]


def _text_forms(secret: str) -> set[str]:
    """Strings whose presence in text reveals a MIN_FRAGMENT-char run of ``secret``."""
    n, forms = MIN_FRAGMENT, set()
    for run in _runs(secret):
        for safe in ("", "/"):                     # URL-encoded, either hex-digit case
            for q in (quote(run, safe, errors="surrogatepass"),
                      quote_plus(run, safe, errors="surrogatepass")):
                forms.update((q, _PCT_ESCAPE.sub(lambda m: m[0].lower(), q)))
        for codec in ("utf-8", "utf-16-le", "utf-16-be"):
            raw = run.encode(codec, "surrogatepass")
            hexed = raw.hex()
            # the bytes read as latin-1 text, escaped the way str(bytes) prints them, as hex
            forms.update((raw.decode("latin-1"), repr(raw)[2:-1], hexed, hexed.upper()))
    data = secret.encode("utf-8", "surrogatepass")
    for k in range(3):                             # base64 with the secret at each byte alignment
        b64 = base64.b64encode(bytes(k) + data).decode("ascii")
        core = b64[(8 * k + 5) // 6:(8 * (k + len(data))) // 6]   # chars of secret bits only
        width = min(n + 2, len(core))
        for alphabet in (core, core.translate(_URLSAFE)):
            forms.update(alphabet[i:i + width] for i in range(len(alphabet) - width + 1))
    return {f for f in forms if len(f) >= n} | set(_runs(secret))


def _byte_forms(secret: str) -> set[str]:
    """latin-1 views of the non-UTF-8 encodings of ``secret``'s runs that NUL-free UTF-8 bytes
    could hide (an ASCII run has none: its UTF-16 holds NULs, its latin-1 bytes are its UTF-8)."""
    forms = set()
    for run in _runs(secret):
        if run.isascii():
            continue
        utf8 = run.encode("utf-8", "surrogatepass")
        for codec in ("utf-16-le", "utf-16-be", "latin-1", "cp1252"):
            try:
                raw = run.encode(codec)
            except UnicodeEncodeError:
                continue
            if raw != utf8 and b"\0" not in raw and len(raw) >= MIN_FRAGMENT:
                forms.add(raw.decode("latin-1"))
    return forms


def _reset_registry(values: tuple[str, ...] | set[str] = ()) -> None:
    """Test helper: replace the registry with exactly ``values``."""
    with _LOCK:
        _SECRETS.clear()
        _SECRETS.update(_plain(v) for v in values if v)
        _rebuild()


# --------------------------------------------------------------------------- Secret
@final
class Secret:
    """A string that will not print itself; ``reveal()`` is the only way to read it.

    repr/str/format/f-strings/%-formatting/logging all give ``Secret(****abcd)`` (the last 4
    chars only when the value has >= 16 chars). Not a str subclass, so json.dumps and string
    concatenation raise; pickling raises; copy/deepcopy return the same immutable object;
    ``==`` (against a Secret or str) is constant-time; instances are unhashable; subclassing
    raises (an override could unmask it). Creating a Secret registers its value with redact().
    """

    __slots__ = ("_value",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("Secret cannot be subclassed")

    def __init__(self, value: str) -> None:
        if not issubclass(type(value), str):
            raise TypeError("Secret value must be a str")
        value = _plain(value)
        if not value:
            raise ValueError("Secret value must be non-empty")
        object.__setattr__(self, "_value", value)
        register_secret(value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        tail = self._value[-4:]
        show = len(self._value) >= 16 and tail.isascii() and tail.isalnum()
        return f"Secret(****{tail if show else ''})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        try:
            return format(repr(self), spec)
        except (TypeError, ValueError):
            return repr(self)

    def __eq__(self, other: object) -> bool:
        if type(other) is Secret:
            other = other._value
        elif issubclass(type(other), str):
            other = _plain(other)
        else:
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8", "surrogatepass"),
                                   other.encode("utf-8", "surrogatepass"))

    __hash__ = None  # type: ignore[assignment]

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Secret is immutable")

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError("Secret must not be pickled or serialised")

    def __getstate__(self) -> Any:
        raise TypeError("Secret must not be pickled or serialised")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: dict) -> Secret:
        return self


def load_secret(env_name: str, env: Mapping[str, str] | None = None) -> Secret | None:
    """Read ``env_name`` from the environment only (``os.environ`` unless ``env`` is given).

    Surrounding whitespace is stripped; unset or blank gives None. The value is registered.
    """
    raw = (os.environ if env is None else env).get(env_name)
    if not issubclass(type(raw), str):
        return None
    raw = _plain(raw).strip()
    return Secret(raw) if raw else None


# --------------------------------------------------------------------------- matching
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SENSITIVE_PARTS = ("apikey", "secret", "password", "passwd", "authorization", "cookie",
                    "credential", "privatekey", "subaccountclientid", "bearer")


def is_sensitive_key(key: object) -> bool:
    """True for names like api_key, X-API-Key, Authorization, access_token, client_secret,
    password, Set-Cookie, sub-account-client-id, WALLEX_API_KEY_READ.
    False for max_tokens, token_bucket, client_id, symbol."""
    if issubclass(type(key), (bytes, bytearray)):
        key = _exact_bytes(key).decode("utf-8", "replace")
    elif issubclass(type(key), str):
        key = _plain(key)                  # a subclass's own lower()/__len__ never runs
    else:
        return False
    return _sensitive_name(key) if len(key) <= 128 else _sensitive_name.__wrapped__(key)


@functools.lru_cache(maxsize=4096)      # names repeat heavily (dict keys, log fields)
def _sensitive_name(key: str) -> bool:
    norm = _NON_ALNUM.sub("", key.lower())
    return norm == "auth" or norm.endswith("token") or any(p in norm for p in _SENSITIVE_PARTS)


_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
# "name=" / "name: " (also quoted, JSON/repr style); the start is anchored and the name bounded.
_KV_NAME = re.compile(r"""(?<![\w.-])(?P<name>[A-Za-z_][\w.-]{0,63})["']?\s*[:=]\s*""")
_KV_VALUE = re.compile(
    r"""\[REDACTED\]|Secret\(\*{4}\w*\)|"[^"]*"?|'[^']*'?
        |(?:(?:bearer|basic|token|digest)\s+)?(?:\[REDACTED\]|[^\s"',;&()<>\[\]{}]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_KV_HINTS = ("key", "secret", "pass", "auth", "cookie", "token", "credential", "client", "bearer")


def _scrub_kv(text: str) -> str:
    """Redact the value after every sensitive ``name=`` / ``name:``. Values are only read for
    sensitive names and each char is consumed at most once, so this stays linear; a
    non-sensitive pair never hides a sensitive one inside its value (``url=...?token=x``)."""
    parts, pos = [], 0
    for m in _KV_NAME.finditer(text):
        if m.start() < pos or not is_sensitive_key(m["name"]):
            continue
        value = _KV_VALUE.match(text, m.end())
        if value is None or value[0].startswith((REDACTED, "Secret(****")):
            continue
        quote_char = value[0][0] if value[0][0] in "\"'" else ""
        parts += (text[pos:m.end()], quote_char, REDACTED, quote_char)
        pos = value.end()
    return "".join(parts) + text[pos:] if parts else text


def _find_all(text: str, needles: Iterable[str], spans: list[tuple[int, int]]) -> None:
    for needle in needles:
        i = text.find(needle)
        while i >= 0:
            spans.append((i, i + len(needle)))
            i = text.find(needle, i + 1)


def _spans(text: str, m: _Matcher) -> list[tuple[int, int]]:
    """(start, end) of every occurrence of every needle of ``m`` in ``text``."""
    heads, w = m.heads, MIN_FRAGMENT
    spans: list[tuple[int, int]] = []
    count = len(text) - w + 1              # number of w-char windows in text
    if heads and count > 0:
        if count <= 2 * len(heads) or len(heads) > 128:     # look text's own windows up
            starts = [i for i in range(count) if text[i:i + w] in heads]
        else:                                               # long text, few heads: C-speed find
            starts = []
            for head in heads:
                i = text.find(head)
                while i >= 0:
                    starts.append(i)
                    i = text.find(head, i + 1)
        for i in starts:
            for needle in heads[text[i:i + w]]:
                if len(needle) == w or text.startswith(needle, i):
                    spans.append((i, i + len(needle)))
    _find_all(text, m.whole, spans)
    return spans


def _scrub_registered(text: str) -> str:
    spans = _spans(text, _MATCHERS[0])
    if not spans:
        return text
    spans.sort()
    parts, pos = [], 0
    start, end = spans[0]
    for s, e in spans[1:]:
        if s <= end:                       # overlapping/adjacent windows: one leaked run
            end = max(end, e)
            continue
        parts += (text[pos:start], REDACTED)
        pos, start, end = end, s, e
    parts += (text[pos:start], REDACTED, text[end:])
    return "".join(parts)


def _scrub_text(text: str) -> str:
    out = _scrub_registered(text)
    lower = out.lower()
    if any(h in lower for h in _KV_HINTS):
        out = _scrub_kv(out)
        if "bearer" in lower:
            out = _BEARER.sub(r"\1 " + REDACTED, out)
    return out


def _redact_bytes(data: bytes) -> bytes:
    """NUL-free strict UTF-8 is scrubbed as text and re-encoded. Anything else (binary, UTF-16/32,
    latin-1 ...) cannot be proven clean, nor can UTF-8 holding a registered secret in another
    encoding: those are replaced whole (fail closed)."""
    try:
        text = None if b"\0" in data else data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    other = _MATCHERS[1]
    if text is None or ((other.heads or other.whole) and _spans(data.decode("latin-1"), other)):
        return b"[BYTES len=%d]" % len(data)
    return _scrub_text(text).encode("utf-8")


# --------------------------------------------------------------------------- redact
def redact(obj: Any) -> Any:
    """Return a scrubbed copy of ``obj`` (see module docstring). Never raises."""
    try:
        out = _redact(obj, 0, set())
        return out if out is not obj or _may_be_returned_as_is(obj) else FAILED
    except Exception:
        return FAILED
    except BaseException as exc:           # from the input's own methods, or a signal handler
        bare = _bare(exc)
    if bare is None:
        return FAILED
    raise bare from None


def _bare(exc: BaseException) -> BaseException | None:
    """KeyboardInterrupt/SystemExit stay control flow, but without the args and context that
    untrusted code may have filled with a secret; any other BaseException is swallowed."""
    if issubclass(type(exc), KeyboardInterrupt):
        return KeyboardInterrupt()
    if issubclass(type(exc), SystemExit):
        code = SystemExit.code.__get__(exc)       # the C slot: a subclass property never runs
        return SystemExit(code if code is None or type(code) is int or type(code) is bool else 1)
    return None


def _plain(text: object) -> str:
    """An exact str with ``text``'s stored characters (no subclass method runs); TypeError
    unless ``text`` really is a str."""
    return str.__str__(text)


def _exact_bytes(obj: bytes | bytearray) -> bytes:
    """``obj``'s stored bytes as an exact bytes object; an overridden __bytes__ never runs."""
    return bytes.__bytes__(obj) if issubclass(type(obj), bytes) else bytes(bytearray.copy(obj))


def _text_of(fn: Any, obj: object) -> str:
    try:
        return _plain(fn(obj))             # repr()/str() may return a str subclass
    except Exception:
        return FAILED


def _is_blank(value: object) -> bool:
    return value is None or (issubclass(type(value), str) and str.__len__(value) == 0)


def _is_numpy_number(kind: type) -> bool:
    """numpy's own scalar number types (immutable, formatted in C); identity with numpy's export
    rules out a subclass or impostor, which would control its own repr."""
    np = sys.modules.get("numpy")
    return (np is not None and kind.__module__ == "numpy"
            and getattr(np, kind.__name__, None) is kind and issubclass(kind, (np.number, np.bool_)))


def _may_be_returned_as_is(obj: object) -> bool:
    """Final check: only an immutable exact builtin may come back as the very input object
    (an unchanged str, a cached bytes, a scalar, the empty-tuple singleton)."""
    kind = type(obj)
    return (kind is str or kind is bytes or id(kind) in _EXACT_SCALAR_IDS or _is_numpy_number(kind)
            or ((kind is tuple or kind is frozenset) and not obj))


def _redact(obj: Any, depth: int, active: set[int]) -> Any:
    kind = type(obj)                       # the real type: obj.__class__ can be faked
    if kind is Secret:
        return repr(obj)
    if issubclass(kind, str):
        return _scrub_text(_plain(obj))
    if issubclass(kind, (bytes, bytearray)):
        return _redact_bytes(_exact_bytes(obj))
    if id(kind) in _EXACT_SCALAR_IDS or _is_numpy_number(kind):
        return obj
    for base, value_of in _SCALAR_BASES:
        if issubclass(kind, base):
            return value_of(obj)
    if issubclass(kind, BaseException):
        return _scrub_text(f"{_plain(kind.__name__)}: {_text_of(str, obj)}")
    if not issubclass(kind, (dict, *_SEQUENCES)) and not issubclass(kind, Mapping):
        return _scrub_text(_text_of(repr, obj))     # unknown object: never passed through
    if depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if id(obj) in active:
        return "[CYCLE]"
    active.add(id(obj))
    try:
        if issubclass(kind, dict) or not issubclass(kind, _SEQUENCES):
            # a dict (subclass) is read from its storage; any other Mapping only has items()
            pairs = dict.items(obj) if issubclass(kind, dict) else obj.items()
            out = {}
            for k, v in pairs:
                hide = is_sensitive_key(k) and not _is_blank(v)
                out[_redact(k, depth + 1, active)] = REDACTED if hide else _redact(v, depth + 1, active)
            return out
        base = next(b for b in _SEQUENCES if issubclass(kind, b))
        elems = list(base.__iter__(obj))   # stored elements: an overridden __iter__ never runs
        items = [_redact(x, depth + 1, active) for x in elems]
        if base in (list, tuple) and len(elems) == 2 and is_sensitive_key(elems[0]) \
                and not _is_blank(elems[1]):
            items[1] = REDACTED                     # ("X-API-Key", value) header pairs
        # builtin kind even for subclasses (a namedtuple comes back as a tuple)
        return items if base is list else base(items)
    finally:
        active.discard(id(obj))


# standard LogRecord attributes; anything else on a record came from ``extra=`` (or a factory)
_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message",
                                                                                 "asctime"}


class RedactingFilter(logging.Filter):
    """Attach to every logging *handler*: scrubs the final message, exception and stack text,
    and every ``extra=`` field."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = FAILED
        record.msg, record.args = redact(message), None
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = _text_of(logging.Formatter().formatException, record.exc_info)
            record.exc_info = None                  # formatters print the scrubbed exc_text
        record.exc_text = redact(record.exc_text)
        record.stack_info = redact(record.stack_info)
        fields = vars(record)
        for name in [n for n in fields if not (type(n) is str and n in _RECORD_FIELDS)]:
            value = fields[name]
            fields[name] = REDACTED if is_sensitive_key(name) and not _is_blank(value) \
                else redact(value)
        return True
