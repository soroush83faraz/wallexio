"""Repo-wide secret hygiene, plus a scanner other tests reuse on DB files and logs:

    from test_no_secrets_in_repo import scan_text_for_secrets
    text = db_path.read_bytes().decode("latin-1")
    assert not scan_text_for_secrets(text, [FAKE_KEY], heuristics=False)

Key-like literals in tests/fixtures must contain FAKE/TEST/EXAMPLE/DUMMY (or sit on a line
with ``pragma: allowlist secret``) for the heuristics to accept them. Findings never contain
the matched value.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from wallexbot.redact import MIN_FRAGMENT

ROOT = Path(__file__).resolve().parents[1]
SECRET_ENV_NAMES = ("WALLEX_API_KEY_READ", "WALLEX_API_KEY_TRADE", "WALLEX_SUB_ACCOUNT_CLIENT_ID",
                    "ANTHROPIC_API_KEY")
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".claude", "data", "logs",
              "runs", "node_modules"}
_SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log", ".pyc")

# NAME = value / "NAME": "value" on one line (any quoting); start anchored, name bounded => linear
_ASSIGNMENT = re.compile(
    r"""(?<![\w.-])(?P<name>[A-Za-z_][\w.-]{0,63})["']?[ \t]*[:=][ \t]*["']?(?P<value>[A-Za-z0-9+/=_\-|.~]{20,})""")
_SECRET_NAME = re.compile(r"(?i)key|secret|token|passw")
_BEARER = re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9\-._~+/]{20,}=*)")
_KNOWN_FORMAT = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_ALLOW_MARKERS = ("fake", "test", "example", "dummy", "placeholder", "changeme", "redacted", "xxxx",
                  "notreal", "not-real")
_PRAGMA = "pragma: allowlist secret"


def _allowlisted(value: str) -> bool:
    low = value.lower()
    return any(marker in low for marker in _ALLOW_MARKERS)


def _looks_real(value: str) -> bool:
    """Token-like: has a digit and a letter, is not an ENV_VAR_NAME, carries no fake marker."""
    return (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
            and not _ENV_NAME.fullmatch(value) and not _allowlisted(value))


def scan_text_for_secrets(text: str, extra_fragments: Iterable[str] = (), *, heuristics: bool = True,
                          min_fragment: int = MIN_FRAGMENT) -> list[str]:
    """Return findings (line numbers and kinds only, never the value) for:
      * any run of >= ``min_fragment`` chars of an ``extra_fragments`` string (shorter strings
        are matched whole; no allowlist applies, so fake keys are found too);
      * with ``heuristics``: token-like values assigned to *KEY*/*SECRET*/*TOKEN*/*PASSWORD*
        names, long Bearer tokens, sk-ant- keys and private-key blocks.
    """
    lines = text.split("\n")
    findings: list[str] = []

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    for idx, secret in enumerate(s for s in extra_fragments if s):
        n = min(min_fragment, len(secret))
        hits = [p for p in (text.find(secret[i:i + n]) for i in range(len(secret) - n + 1)) if p >= 0]
        if hits:
            findings.append(f"line {line_of(min(hits))}: run of extra secret #{idx}")
    if not heuristics:
        return findings

    def report(match: re.Match, what: str) -> None:
        line = line_of(match.start())
        if _PRAGMA not in lines[line - 1]:
            findings.append(f"line {line}: {what}")

    for m in _ASSIGNMENT.finditer(text):
        if _SECRET_NAME.search(m["name"]) and _looks_real(m["value"]):
            report(m, f"token-like value ({len(m['value'])} chars) assigned to {m['name']}")
    for m in _BEARER.finditer(text):
        if _looks_real(m["value"]):
            report(m, f"Bearer token ({len(m['value'])} chars)")
    for m in _KNOWN_FORMAT.finditer(text):
        if not _allowlisted(m[0]):
            report(m, "known secret format (sk-ant- key or private key block)")
    return findings


# ---------------------------------------------------------------- repo helpers
def _git(*args: str) -> subprocess.CompletedProcess | None:
    if shutil.which("git") is None:
        return None
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def _in_git_checkout() -> bool:
    res = _git("rev-parse", "--is-inside-work-tree")
    return res is not None and res.returncode == 0 and res.stdout.strip() == "true"


def _repo_files() -> list[Path]:
    """Files a commit could include: tracked + untracked-not-ignored (git), else a pruned walk."""
    res = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if res is not None and res.returncode == 0:
        paths = [ROOT / p for p in res.stdout.split("\0") if p]
    else:
        paths = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            paths += [Path(dirpath, f) for f in filenames
                      if not f.endswith(_SKIP_SUFFIXES)
                      and not (f == ".env" or (f.startswith(".env.") and f != ".env.example"))]
    return [p for p in paths if p.is_file()]


def _scan_repo() -> list[str]:
    """In a helper so raw env values never sit in the failing test's frame (pytest -l)."""
    # Real keys present in this environment (never printed) are searched for verbatim too;
    # 12-char runs keep chance collisions with unrelated hex text negligible.
    env_secrets = [v.strip() for name in SECRET_ENV_NAMES
                   if len((v := os.environ.get(name, "")).strip()) >= 16]
    findings = []
    for path in _repo_files():
        text = path.read_bytes().decode("utf-8", "replace")
        rel = path.relative_to(ROOT)
        findings += [f"{rel}: {f}" for f in scan_text_for_secrets(text)]
        findings += [f"{rel}: {f} (a key from the current environment)" for f in
                     scan_text_for_secrets(text, env_secrets, heuristics=False, min_fragment=12)]
    return findings


# ---------------------------------------------------------------- repo tests
def test_repo_has_no_committed_secrets():
    files = _repo_files()
    assert any(p.name == "redact.py" for p in files), "repo file listing looks wrong"
    findings = _scan_repo()
    assert not findings, "possible secrets in the repo:\n" + "\n".join(findings)


def test_dotenv_is_git_ignored_and_untracked():
    patterns = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    assert ".env" in patterns
    if not _in_git_checkout():
        return  # e.g. an sdist: the .gitignore line above is all that can be checked
    for name in (".env", ".env.local", ".env.production"):
        assert _git("check-ignore", "-q", "--no-index", name).returncode == 0, f"{name} not ignored"
    assert _git("check-ignore", "-q", "--no-index", ".env.example").returncode == 1, \
        ".env.example must stay tracked"
    tracked = set(_git("ls-files", "--", ".env", ".env.*").stdout.split())
    assert tracked <= {".env.example"}, f"env files are tracked: {sorted(tracked)}"


def test_env_example_has_names_only():
    names = []
    for n, line in enumerate((ROOT / ".env.example").read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.removeprefix("export ").partition("=")
        assert sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", name.strip()), f"line {n} is not NAME="
        assert value.strip() == "", f".env.example line {n} ({name.strip()}) must be empty"
        names.append(name.strip())
    assert set(SECRET_ENV_NAMES) | {"WALLEX_ARM_LIVE"} <= set(names)


# ---------------------------------------------------------------- scanner self-tests
def _real_looking_token() -> str:
    return "a1" + secrets.token_hex(19)          # generated at runtime, never a literal


def test_scanner_flags_planted_secrets():
    tok = _real_looking_token()
    planted = [f'WALLEX_API_KEY_TRADE="{tok}"', f"api_key: {tok}", f'{{"X-API-Key": "{tok}"}}',
               f"export ANTHROPIC_API_KEY={tok}", f"client_secret = '{tok}'",
               f"curl -H 'Authorization: Bearer {tok}'", "sk-ant-" + tok,
               "-----BEGIN " + "RSA PRIVATE KEY-----"]
    for text in planted:
        findings = scan_text_for_secrets("x = 1\n" + text)
        assert findings and all(f.startswith("line 2:") for f in findings), "missed a planted secret"
        assert tok not in "".join(findings)
    # an empty NAME= line must not swallow the next line (wrong line/name attribution)
    findings = scan_text_for_secrets(f"WALLEX_API_KEY_READ=\nWALLEX_API_KEY_TRADE={tok}\n")
    assert findings == ["line 2: token-like value (40 chars) assigned to WALLEX_API_KEY_TRADE"]


def test_scanner_accepts_placeholders_names_and_fakes():
    benign = ["WALLEX_API_KEY_READ=", 'api_key = "FAKE_read_key_zQxWvB_NotReal_kYjHgF"',
              'TRADE_KEY_ENV = "WALLEX_API_KEY_TRADE"', "max_tokens: 1024", "api_key=[REDACTED]",
              "Authorization: Bearer <token>", "token: '" + _real_looking_token() + "'  # " + _PRAGMA]
    for text in benign:
        assert scan_text_for_secrets(text) == [], text


def test_scanner_extra_fragments_for_db_and_log_files(tmp_path):
    secret = "FAKE_db_scan_key_hJkLmN_qWeRtY"
    db = tmp_path / "audit.db"
    db.write_bytes(b"SQLite format 3\x00row1|ok|" + secret[4:14].encode() + b"|end")
    text = db.read_bytes().decode("latin-1")
    findings = scan_text_for_secrets(text, [secret], heuristics=False)
    assert findings and secret[4:14] not in "".join(findings)   # fake marker is no excuse here
    assert scan_text_for_secrets(f"only {secret[4:9]} here", [secret]) == []   # 5 < MIN_FRAGMENT
    assert scan_text_for_secrets(text.replace(secret[4:14], "[REDACTED]"), [secret]) == []
