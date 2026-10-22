"""Read-only preflight ("doctor") for the Wallex account and this installation.

    report = run_doctor(load_secret(READ_KEY_ENV), None)   # None: the real HTTPS transport
    print(report.render())                                 # report.ok is False on any FAIL

run_doctor() sends nothing but GET. It reaches Wallex only through WallexAccountReader (GET-only
by construction) over a transport wrapper that refuses any other method, it uses only the READ
key, and it never arms anything (the config is loaded with cli_armed=False). Checks:

  host_pin          the URL allow-list is pinned to api.wallex.ir and refuses a lookalike host,
                    plain http and a fund-movement path (local; nothing is sent)
  read_key          a read key is present and usable as a header; shown only masked (Secret repr)
  balances, fee, open_orders
                    the private GETs succeed AND parse with the adapter's fail-closed parsers,
                    which is how the unverified response shapes get checked
  not_found_lookup  GET of a random client_id reads as "not found" (404 + JSON success:false), the
                    shape reconcile relies on to tell "never created" from "lookup failed"
  clock_skew        local clock minus the server's Date header (SKIP when there is none)
  config            the config loads through the fail-closed loader and its mode is not live
  dotenv_ignored    .env is git-ignored and untracked (SKIP outside a git checkout)
  repo_secret_scan  SKIP: the scanner lives in tests/ and the doctor never imports test modules

Key permissions that cannot be verified from code are listed in DoctorReport.unverifiable.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from wallexbot.config import ALLOWED_HOSTS, load_config
from wallexbot.exchange.auth import API_HOST, AuthError, assert_host_allowed
from wallexbot.exchange.wallex_private import (BALANCES_PATH, BASE_URL, AuthRejected, Transport,
                                               WallexAccountReader, urllib_transport)
from wallexbot.redact import FAILED, Secret, redact

__all__ = ["CheckResult", "DoctorReport", "FAIL", "PASS", "SKIP", "UNVERIFIABLE", "run_doctor"]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
REPO_ROOT = Path(__file__).resolve().parents[2]
NETWORK_CHECKS = ("balances", "fee", "open_orders", "not_found_lookup")
# Must be refused by assert_host_allowed (checked locally; never sent anywhere).
_MUST_REFUSE = ("https://api.wallex.ir.example.com/v1/account/balances",
                "http://api.wallex.ir/v1/account/balances",
                "https://api.wallex.ir/v1/account/crypto-withdrawal")
UNVERIFIABLE = (
    "Withdraw permission is OFF on both keys: key permissions are not visible over the API; "
    "confirm on the Wallex API-key page (the code also refuses any withdraw path).",
    "IP allow-list is set on both keys (if Wallex offers it): not visible over the API.",
    "The READ key has no Trade permission: proving it would take an order request, and the "
    "doctor sends only GETs.",
)

# git(args) -> (returncode, stdout), or None when git cannot be run
GitRunner = Callable[[Sequence[str]], "tuple[int, str] | None"]


def _m(text: str | BaseException) -> str:
    out = redact(text)
    return out if type(out) is str else FAILED


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str          # PASS | FAIL | SKIP
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]
    unverifiable: tuple[str, ...] = UNVERIFIABLE

    @property
    def ok(self) -> bool:
        """No check failed (SKIPs do not fail the report; read them)."""
        return all(c.status != FAIL for c in self.checks)

    def by_name(self, name: str) -> CheckResult:
        return next(c for c in self.checks if c.name == name)

    def render(self) -> str:
        lines = [f"wallexbot doctor: {'OK' if self.ok else 'NOT OK'} (read-only: GET requests only)"]
        lines += [f"  {c.status:<4}  {c.name:<16}  {c.detail}" for c in self.checks]
        lines.append("Could NOT be verified from code (confirm by hand):")
        lines += [f"  - {item}" for item in self.unverifiable]
        return _m("\n".join(lines))


def _get_only(transport: Transport) -> Transport:
    """Belt and braces around the GET-only reader: refuse any other method or any body."""
    def guarded(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float):
        if method != "GET" or body is not None:
            raise AuthError("the doctor is read-only: refusing a request that is not a plain GET")
        return transport(method, url, headers, body, timeout)
    return guarded


def run_doctor(read_key: Secret | None, transport: Transport | None = None, *,
               sub_account: str | Secret | None = None, clock: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep, config_path: Path | str | None = None,
               env: Mapping[str, str] | None = None, repo_root: Path = REPO_ROOT,
               git: GitRunner | None = None, max_skew_s: float = 5.0,
               timeout_s: float = 10.0) -> DoctorReport:
    """Run every read-only check; see the module docstring. Never raises for a failed check."""
    checks = [_check_host_pin()]
    reader, key_check = _reader(read_key, transport, sub_account, clock, sleep, timeout_s)
    checks.append(key_check)
    if reader is None:
        checks += [CheckResult(n, FAIL, "no usable read key; nothing was requested")
                   for n in NETWORK_CHECKS]
        checks.append(CheckResult("clock_skew", SKIP, "no server response to compare against"))
    else:
        checks.append(_network("balances", lambda: f"{len(reader.balances())} assets parsed"))
        checks.append(_network("fee", lambda: _fee_detail(reader)))
        checks.append(_network("open_orders",
                               lambda: f"{len(reader.open_orders())} open orders parsed"))
        checks.append(_not_found_lookup(reader))
        checks.append(_clock_skew(reader, max_skew_s))
    checks.append(_check_config(config_path, env))
    checks.append(_check_dotenv(git if git is not None else _git_runner(repo_root)))
    checks.append(CheckResult("repo_secret_scan", SKIP,
                              "the scanner lives in tests/test_no_secrets_in_repo.py and the doctor "
                              "imports no test module; run that test file instead"))
    return DoctorReport(tuple(checks))


# --------------------------------------------------------------------------- checks
def _check_host_pin() -> CheckResult:
    problems = []
    if ALLOWED_HOSTS != frozenset({API_HOST}):
        problems.append("config.ALLOWED_HOSTS is not exactly {api.wallex.ir}")
    try:
        assert_host_allowed(BASE_URL + BALANCES_PATH)
    except AuthError:
        problems.append("the pinned host itself is refused")
    for url in _MUST_REFUSE:
        try:
            assert_host_allowed(url)
        except AuthError:
            continue
        problems.append("a URL that must be refused was allowed")
    if problems:
        return CheckResult("host_pin", FAIL, "; ".join(problems))
    return CheckResult("host_pin", PASS, f"only https://{API_HOST} is allowed; lookalike host, "
                                         "plain http and a withdraw path are refused")


def _reader(read_key: object, transport: Transport | None, sub_account: str | Secret | None,
            clock: Callable[[], float], sleep: Callable[[float], None],
            timeout_s: float) -> tuple[WallexAccountReader | None, CheckResult]:
    if read_key is None:
        return None, CheckResult("read_key", FAIL, "WALLEX_API_KEY_READ is not set")
    if type(read_key) is not Secret:
        return None, CheckResult("read_key", FAIL, "the read key must be a Secret "
                                                   "(load it with redact.load_secret)")
    try:
        reader = WallexAccountReader(
            read_key, sub_account=sub_account, clock=clock, sleep=sleep, timeout_s=timeout_s,
            transport=_get_only(urllib_transport if transport is None else transport))
    except AuthError as e:
        return None, CheckResult("read_key", FAIL, _m(f"the read key is unusable: {e}"))
    return reader, CheckResult("read_key", PASS, _m(f"present, {read_key!r} (masked)"))


def _network(name: str, probe: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, PASS, _m(probe()))
    except AuthRejected as e:
        return CheckResult(name, FAIL, _m(f"key rejected: {e}"))
    except Exception as e:  # the doctor reports every failure instead of stopping
        return CheckResult(name, FAIL, _m(e))


def _fee_detail(reader: WallexAccountReader) -> str:
    table = reader.fee_table()
    maker = max(r.maker for r in table.values())
    taker = max(r.taker for r in table.values())
    return f"{len(table)} markets; highest maker {maker}, highest taker {taker} (as fractions)"


def _not_found_lookup(reader: WallexAccountReader) -> CheckResult:
    probe = "wb_" + secrets.token_hex(16)      # a well-formed client_id nobody has used
    try:
        found = reader.get_order(probe)
    except Exception as e:  # noqa: BLE001
        return CheckResult("not_found_lookup", FAIL, _m(
            f"looking up an unknown client_id failed ({_m(e)}); reconcile could not tell 'never "
            "created' from 'lookup failed', so ambiguous orders would stay pending"))
    if found is not None:
        return CheckResult("not_found_lookup", FAIL, "an order came back for a random client_id")
    return CheckResult("not_found_lookup", PASS, "an unknown client_id reads as not found "
                                                 "(404 + JSON success:false), as reconcile assumes")


def _clock_skew(reader: WallexAccountReader, max_skew_s: float) -> CheckResult:
    seen = reader.last_server_date
    if seen is None:
        return CheckResult("clock_skew", SKIP, "no Date header in any response")
    server, local = seen
    skew = local - server
    detail = f"local - server = {skew:+.1f}s (limit {max_skew_s:g}s; Date has 1 s resolution)"
    return CheckResult("clock_skew", PASS if abs(skew) <= max_skew_s else FAIL, detail)


def _check_config(path: Path | str | None, env: Mapping[str, str] | None) -> CheckResult:
    try:
        cfg = load_config(path, env=os.environ if env is None else env, cli_armed=False)
    except Exception as e:  # noqa: BLE001
        return CheckResult("config", FAIL, _m(
            f"did not load ({_m(e)}); live mode never loads here: the doctor does not arm"))
    if cfg.mode == "live":            # unreachable with cli_armed=False; kept as a backstop
        return CheckResult("config", FAIL, "mode is live")
    return CheckResult("config", PASS, f"loads fail-closed; mode={cfg.mode} (not live)")


def _git_runner(root: Path) -> GitRunner:
    def run(args: Sequence[str]) -> tuple[int, str] | None:
        exe = shutil.which("git")
        if exe is None:
            return None
        try:
            done = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True,
                                  timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.returncode, done.stdout
    return run


def _check_dotenv(git: GitRunner) -> CheckResult:
    inside = git(["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside[0] != 0 or inside[1].strip() != "true":
        return CheckResult("dotenv_ignored", SKIP, "not a git checkout, or git is unavailable")
    ignored = git(["check-ignore", "-q", "--no-index", ".env"])
    tracked = git(["ls-files", "--", ".env"])
    if ignored is None or tracked is None:
        return CheckResult("dotenv_ignored", SKIP, "git could not be run")
    if tracked[1].strip():
        return CheckResult("dotenv_ignored", FAIL, ".env is tracked by git")
    if ignored[0] == 0:
        return CheckResult("dotenv_ignored", PASS, ".env is git-ignored and untracked")
    if ignored[0] == 1:
        return CheckResult("dotenv_ignored", FAIL, ".env is NOT git-ignored")
    return CheckResult("dotenv_ignored", SKIP, f"git check-ignore exited {ignored[0]}")
