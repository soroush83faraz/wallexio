"""Wallex authentication surface. Wallex auth is one ``X-API-Key`` header (no HMAC/secret).

Keys (environment only, never files/config/argv; see wallexbot.redact.load_secret):
  * WALLEX_API_KEY_READ   - Read-only key: market data, balances, order status.
  * WALLEX_API_KEY_TRADE  - Read+Trade key: obtained only through assert_live_armed().
  * WALLEX_SUB_ACCOUNT_CLIENT_ID - optional, sent as the ``sub-account-client-id`` header.

WITHDRAW IS NEVER USED. Create both keys WITHOUT the Withdraw permission (restrict them to the
bot's IP if Wallex offers it). As a code-level backstop, assert_host_allowed() refuses any URL
whose path mentions "withdraw", so this bot cannot call a withdrawal endpoint even by mistake.

Every request URL must pass assert_host_allowed(). Authenticated requests must go through
safe_opener(): verified TLS, and a redirect is followed only if its target passes the same
check, so the key header is never forwarded to another host (urllib re-sends it on redirect).

is_live_armed_token(obj) is True only for the very object an assert_live_armed() call returned,
so a consumer (WallexBroker) can demand proof of arming that an equal Secret does not forge.
"""
from __future__ import annotations

import os
import re
import ssl
import threading
import urllib.request
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from wallexbot.redact import Secret, load_secret, redact

API_HOST = "api.wallex.ir"
API_KEY_HEADER = "X-API-Key"
SUB_ACCOUNT_HEADER = "sub-account-client-id"
READ_KEY_ENV = "WALLEX_API_KEY_READ"
TRADE_KEY_ENV = "WALLEX_API_KEY_TRADE"
SUB_ACCOUNT_ENV = "WALLEX_SUB_ACCOUNT_CLIENT_ID"
ARM_ENV, ARM_VALUE = "WALLEX_ARM_LIVE", "YES_I_UNDERSTAND"

_ALLOWED_NETLOCS = (API_HOST, f"{API_HOST}:443")
_URL_CHARS = re.compile(r"[\x21-\x7e]+")         # printable ASCII: no space/control/IDN tricks
_HEADER_VALUE = re.compile(r"[\x21-\x7e]{1,512}")  # no CR/LF (header injection) or spaces
# Every object assert_live_armed() has returned in this process, kept for identity checks only
# (one entry per successful arming; the same key is already held by os.environ and redact).
_ARMED_TOKENS: list[Secret] = []
_ARMED_LOCK = threading.Lock()


class AuthError(Exception):
    """An auth, URL or arming check refused. Messages never contain secret values."""


class UrlNotAllowed(AuthError):
    pass


class LiveNotArmed(AuthError):
    pass


class AuthHeaders(dict):
    """The auth headers. Values are raw (urllib needs them) but repr/str are redacted."""

    def __repr__(self) -> str:
        return repr(redact(dict(self)))

    __str__ = __repr__


def _header_value(value: object, what: str) -> str:
    if not isinstance(value, str) or not _HEADER_VALUE.fullmatch(value):
        raise AuthError(f"{what} is not a valid header value (1-512 printable ASCII, no spaces)")
    return value


def build_auth_headers(secret: Secret, sub_account: str | Secret | None = None) -> AuthHeaders:
    """Headers for a private Wallex request: ``X-API-Key`` and, if given, the sub-account id."""
    if not isinstance(secret, Secret):
        raise TypeError("build_auth_headers needs a Secret, never a raw str")
    headers = AuthHeaders({API_KEY_HEADER: _header_value(secret.reveal(), "API key")})
    if sub_account is not None:       # "" is refused, not silently treated as "main account"
        raw = sub_account.reveal() if isinstance(sub_account, Secret) else sub_account
        headers[SUB_ACCOUNT_HEADER] = _header_value(raw, "sub-account client id")
    return headers


def assert_host_allowed(url: str) -> str:
    """Return ``url`` if it is ``https://api.wallex.ir[:443]/...`` with no userinfo and no
    withdraw path; raise UrlNotAllowed otherwise. Only printable ASCII without backslashes is
    accepted, so every URL parser agrees on the host (no lookalike/IDN/whitespace tricks)."""
    if not isinstance(url, str) or not _URL_CHARS.fullmatch(url) or "\\" in url:
        raise UrlNotAllowed("URL must be printable ASCII without spaces or backslashes")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise UrlNotAllowed("URL could not be parsed") from None
    if parts.scheme != "https":
        raise UrlNotAllowed("only https is allowed")
    if "@" in parts.netloc or parts.netloc.lower() not in _ALLOWED_NETLOCS or host != API_HOST:
        raise UrlNotAllowed(f"only {API_HOST} (port 443, no userinfo) is allowed")
    if "withdraw" in unquote(parts.path).lower():
        raise UrlNotAllowed("withdraw endpoints are never used")
    return url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when its target passes assert_host_allowed(); else raise."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        assert_host_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener() -> urllib.request.OpenerDirector:
    """A urllib opener for Wallex: default (verifying) TLS context and pinned redirects."""
    return urllib.request.build_opener(
        SafeRedirectHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def assert_live_armed(env: Mapping[str, str] | None, cli_flag: bool) -> Secret:
    """Gate for live trading; returns the trade key only when ALL of these hold:
    ``WALLEX_ARM_LIVE == "YES_I_UNDERSTAND"`` (exact), ``cli_flag is True`` (the --arm-live
    flag) and a well-formed WALLEX_API_KEY_TRADE. Otherwise raises LiveNotArmed naming what
    is missing (never a value). ``doctor`` green and the user's explicit go-ahead are operator
    steps outside this check. ``env=None`` reads ``os.environ``."""
    env = os.environ if env is None else env
    missing = []
    if env.get(ARM_ENV) != ARM_VALUE:
        missing.append(f"{ARM_ENV}={ARM_VALUE}")
    if cli_flag is not True:
        missing.append("--arm-live flag")
    key = load_secret(TRADE_KEY_ENV, env)
    if key is None or not _HEADER_VALUE.fullmatch(key.reveal()):
        missing.append(f"a valid {TRADE_KEY_ENV}")
    if missing:
        raise LiveNotArmed("live trading is not armed; missing: " + ", ".join(missing))
    with _ARMED_LOCK:
        _ARMED_TOKENS.append(key)
    return key


def is_live_armed_token(token: object) -> bool:
    """True only for the very object an assert_live_armed() call returned in this process.

    Identity, not equality: a Secret with the same value made any other way (load_secret,
    Secret(...)) is not evidence that the arming conditions held, so it cannot arm anything."""
    if type(token) is not Secret:
        return False
    with _ARMED_LOCK:
        return any(token is armed for armed in _ARMED_TOKENS)
