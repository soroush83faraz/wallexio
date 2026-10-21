"""Secret handling: a self-masking ``Secret`` wrapper and a never-raising ``redact()``.

    from wallexbot.redact import Secret, load_secret, redact
    key = load_secret("WALLEX_API_KEY_READ")   # Secret | None; read from env only, registered
    log.info("state=%s", redact(state))        # scrub before logging, persisting or AI prompts

redact(obj) returns a NEW object (input is never mutated) with:
  * values under secret-like keys (api_key, x-api-key, authorization, *token, secret, password,
    cookie, sub-account-client-id, credential, private_key) replaced by REDACTED, in mappings,
    ``(name, value)`` pairs and ``name=value`` / ``name: value`` text;
  * ``Bearer <token>`` removed from text;
  * every registered secret, and any run of >= MIN_FRAGMENT chars of one, removed from text.
Containers keep their kind (any Mapping becomes a plain dict); immutable leaves may be returned
as-is. Exceptions become a redacted ``"Type: message"`` str, a Secret its masked repr, and any
object it does not know a redacted ``repr()`` str (fail closed). It never raises: on internal
failure it returns FAILED, never the input. ``RedactingFilter`` applies it to log records.
"""
from __future__ import annotations

import datetime as _dt
import functools
import hmac
import logging
import numbers
import os
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, final

__all__ = ["FAILED", "MIN_FRAGMENT", "REDACTED", "RedactingFilter", "Secret",
           "is_sensitive_key", "load_secret", "redact", "register_secret"]

REDACTED = "[REDACTED]"
FAILED = "[REDACTION_FAILED]"
MIN_FRAGMENT = 6     # a leaked run of >= 6 chars of a registered secret is scrubbed
_MAX_DEPTH = 32      # deeper content is replaced, which also bounds recursion
# passed through unchanged (numbers.Number covers int/float/Decimal/Fraction and numpy scalars)
_SAFE_SCALARS = (type(None), numbers.Number, _dt.date, _dt.time, _dt.timedelta, uuid.UUID)

# --------------------------------------------------------------------------- registry
_LOCK = threading.Lock()
_SECRETS: set[str] = set()
# (every MIN_FRAGMENT-char run of a registered secret, secrets shorter than that matched whole);
# swapped atomically on registration and read without the lock.
_MATCHERS: tuple[frozenset[str], tuple[str, ...]] = (frozenset(), ())


def register_secret(value: str | Secret | None) -> None:
    """Scrub ``value`` and all of its >= MIN_FRAGMENT-char runs from future redact() output.

    Values shorter than MIN_FRAGMENT are matched whole. None/"" are ignored.
    """
    if isinstance(value, Secret):
        value = value.reveal()
    if _is_blank(value):
        return
    if not isinstance(value, str):
        raise TypeError("register_secret expects a str or Secret")
    with _LOCK:
        if value not in _SECRETS:
            _SECRETS.add(value)
            _rebuild()


def _rebuild() -> None:
    global _MATCHERS
    n = MIN_FRAGMENT
    windows = frozenset(s[i:i + n] for s in _SECRETS if len(s) >= n for i in range(len(s) - n + 1))
    _MATCHERS = (windows, tuple(s for s in _SECRETS if len(s) < n))


def _reset_registry(values: tuple[str, ...] | set[str] = ()) -> None:
    """Test helper: replace the registry with exactly ``values``."""
    with _LOCK:
        _SECRETS.clear()
        _SECRETS.update(v for v in values if v)
        _rebuild()


# --------------------------------------------------------------------------- Secret
@final
class Secret:
    """A string that will not print itself; ``reveal()`` is the only way to read it.

    repr/str/format/f-strings/%-formatting/logging all give ``Secret(****abcd)`` (the last 4
    chars only when the value has >= 16 chars). Not a str subclass, so json.dumps and string
    concatenation raise; pickling raises; copy/deepcopy return the same immutable object;
    ``==`` (against a Secret or str) is constant-time; instances are unhashable. Creating a
    Secret registers its value with redact().
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret value must be a str")
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
        if isinstance(other, Secret):
            other = other._value
        if not isinstance(other, str):
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
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Secret(raw.strip())


# --------------------------------------------------------------------------- matching
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SENSITIVE_PARTS = ("apikey", "secret", "password", "passwd", "authorization", "cookie",
                    "credential", "privatekey", "subaccountclientid", "bearer")


def is_sensitive_key(key: object) -> bool:
    """True for names like api_key, X-API-Key, Authorization, access_token, client_secret,
    password, Set-Cookie, sub-account-client-id, WALLEX_API_KEY_READ.
    False for max_tokens, token_bucket, client_id, symbol."""
    if isinstance(key, bytes):
        key = key.decode("utf-8", "replace")
    if not isinstance(key, str):
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
        quote = value[0][0] if value[0][0] in "\"'" else ""
        parts += (text[pos:m.end()], quote, REDACTED, quote)
        pos = value.end()
    return "".join(parts) + text[pos:] if parts else text


def _find_all(text: str, needles: Iterable[str], spans: list[tuple[int, int]]) -> None:
    for needle in needles:
        i = text.find(needle)
        while i >= 0:
            spans.append((i, i + len(needle)))
            i = text.find(needle, i + 1)


def _scrub_registered(text: str) -> str:
    windows, whole = _MATCHERS
    w, spans = MIN_FRAGMENT, []
    n = len(text) - w + 1                  # number of w-char windows in text
    if 0 < n <= len(windows) // 2:         # short text: look its own windows up in the set
        spans = [(i, i + w) for i in range(n) if text[i:i + w] in windows]
    elif n > 0:                            # long text: C-speed search for each secret window
        _find_all(text, windows, spans)
    _find_all(text, whole, spans)
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


# --------------------------------------------------------------------------- redact
def redact(obj: Any) -> Any:
    """Return a scrubbed copy of ``obj`` (see module docstring). Never raises."""
    try:
        return _redact(obj, 0, set())
    except Exception:
        return FAILED


def _text_of(fn: Any, obj: object) -> str:
    try:
        text = fn(obj)
        return text if isinstance(text, str) else FAILED
    except Exception:
        return FAILED


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value)


def _redact(obj: Any, depth: int, active: set[int]) -> Any:
    if isinstance(obj, Secret):
        return repr(obj)
    if isinstance(obj, str):
        out = _scrub_text(obj)
        return obj if out == obj else out
    if isinstance(obj, (bytes, bytearray)):
        text = bytes(obj).decode("utf-8", "replace")
        out = _scrub_text(text)
        if out != text:
            return out.encode("utf-8")
        return obj if isinstance(obj, bytes) else bytes(obj)
    if isinstance(obj, _SAFE_SCALARS):
        return obj
    if isinstance(obj, BaseException):
        return _scrub_text(f"{type(obj).__name__}: {_text_of(str, obj)}")
    if not isinstance(obj, (Mapping, list, tuple, set, frozenset)):
        return _scrub_text(_text_of(repr, obj))     # unknown object: never passed through
    if depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if id(obj) in active:
        return "[CYCLE]"
    active.add(id(obj))
    try:
        if isinstance(obj, Mapping):
            out = {}
            for k, v in obj.items():
                hide = is_sensitive_key(k) and not _is_blank(v)
                out[_redact(k, depth + 1, active)] = REDACTED if hide else _redact(v, depth + 1, active)
            return out
        items = [_redact(x, depth + 1, active) for x in obj]
        if isinstance(obj, list | tuple) and len(obj) == 2 and is_sensitive_key(obj[0]) \
                and not _is_blank(obj[1]):
            items[1] = REDACTED                     # ("X-API-Key", value) header pairs
        if isinstance(obj, list):
            return items
        # builtin kind even for subclasses (a namedtuple comes back as a tuple)
        kind = tuple if isinstance(obj, tuple) else frozenset if isinstance(obj, frozenset) else set
        return kind(items)
    finally:
        active.discard(id(obj))


class RedactingFilter(logging.Filter):
    """Attach to every logging *handler*: scrubs the final message, exception and stack text."""

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
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
