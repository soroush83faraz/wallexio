"""AST isolation of the AI advisor, in both directions, plus a no-dynamic-access check.

(1) Every module under ai/ imports (absolute, relative, `import x.y`, `from x import y`, at any
nesting level) nothing from wallexbot except `wallexbot.redact` (stdlib-only secret scrubbing) and
its own `wallexbot.ai.*` modules. In particular nothing from risk, oms, exchange, store, guardian,
bot, pipeline, backtest, strategy, config or types: the advisor sees plain numbers and returns plain
data, so it has no object through which it could reach an order path.
(2) Nothing in the order/risk path (risk, oms, exchange, store, guardian, config, types) imports
wallexbot.ai, directly or through a transitive wallexbot import chain from those modules.
(3) ai/ uses no network library other than urllib and no subprocess/os/socket/importlib.
Static, best-effort: tests/test_single_order_path_ast.py covers broker/submit-name bypasses."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wallexbot"
AI = sorted((SRC / "ai").glob("*.py"))
ALLOWED_WALLEXBOT = ("wallexbot.redact",)
FORBIDDEN_WALLEXBOT = ("risk", "oms", "exchange", "store", "guardian", "bot", "pipeline", "backtest",
                       "strategy", "config", "types", "data", "doctor")
ORDER_PATH = ["risk", "oms.py", "exchange", "store.py", "guardian", "config.py", "types.py"]
FORBIDDEN_STDLIB = {"subprocess", "os", "socket", "importlib", "ctypes", "shutil", "pickle", "marshal",
                    "sqlite3", "requests", "httpx", "aiohttp", "http", "ssl", "builtins", "inspect", "anthropic"}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def package(path: Path) -> list[str]:
    name = module_name(path).split(".")
    return name if path.name == "__init__.py" else name[:-1]


def imports_of(source: str, path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = package(path)
                mod = ".".join(pkg[: len(pkg) - (node.level - 1)] + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def is_under(mod: str, prefix: str) -> bool:
    return mod == prefix or mod.startswith(prefix + ".")


def wallexbot_imports(source: str, path: Path) -> set[str]:
    return {m for m in imports_of(source, path) if is_under(m, "wallexbot")}


def test_ai_package_is_nonempty():
    assert {p.name for p in AI} >= {"__init__.py", "schema.py", "advisor.py", "evaluate.py"}


@pytest.mark.parametrize("path", AI, ids=lambda p: p.name)
def test_ai_imports_only_redact_and_itself(path):
    bad = []
    for mod in wallexbot_imports(path.read_text(), path):
        if mod == "wallexbot":
            continue
        # `from wallexbot import x` is reported as wallexbot.x: only the allowed ones pass
        if any(is_under(mod, a) for a in ALLOWED_WALLEXBOT) or is_under(mod, "wallexbot.ai"):
            continue
        bad.append(mod)
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", AI, ids=lambda p: p.name)
def test_ai_imports_nothing_dangerous_from_the_stdlib(path):
    bad = {m for m in imports_of(path.read_text(), path) if m.split(".")[0] in FORBIDDEN_STDLIB}
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", AI, ids=lambda p: p.name)
def test_ai_forbidden_names_are_not_imported(path):
    for mod in wallexbot_imports(path.read_text(), path):
        for name in FORBIDDEN_WALLEXBOT:
            assert not is_under(mod, f"wallexbot.{name}"), (path.name, mod)


def order_path_files() -> list[Path]:
    out: list[Path] = []
    for entry in ORDER_PATH:
        p = SRC / entry
        out += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    return out


def test_order_path_files_exist_so_the_check_is_not_vacuous():
    files = order_path_files()
    assert all(f.exists() for f in files) and len(files) >= 8


@pytest.mark.parametrize("path", order_path_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_nothing_in_the_order_path_imports_ai(path):
    bad = {m for m in imports_of(path.read_text(), path) if is_under(m, "wallexbot.ai")}
    assert not bad, f"{path.relative_to(SRC)} imports {sorted(bad)}"


def reachable_from(start: Path) -> set[str]:
    """Transitive closure of wallexbot modules imported by `start` (modules, packages, submodules)."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        path = todo.pop()
        for mod in wallexbot_imports(path.read_text(), path):
            if mod in seen:
                continue
            seen.add(mod)
            rel = Path(*mod.split(".")[1:])
            for cand in (SRC / rel.with_suffix(".py"), SRC / rel / "__init__.py"):
                if cand.exists():
                    todo.append(cand)
    return seen


@pytest.mark.parametrize("path", order_path_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_ai_is_not_transitively_reachable_from_the_order_path(path):
    assert not {m for m in reachable_from(path) if is_under(m, "wallexbot.ai")}


def test_ai_reaches_nothing_beyond_redact_transitively():
    for path in AI:
        reach = {m for m in reachable_from(path) if not is_under(m, "wallexbot.ai")}
        extra = {m for m in reach if m != "wallexbot" and not is_under(m, "wallexbot.redact")}
        assert not extra, (path.name, sorted(extra))


# ------------------------------------------------------------------ the checker itself
def test_checker_detects_rogue_imports():
    p = SRC / "ai" / "rogue.py"
    for src, hit in [("from wallexbot.oms import OMS", "wallexbot.oms"), ("import wallexbot.risk.engine", "wallexbot.risk.engine"),
                     ("from wallexbot import exchange", "wallexbot.exchange"), ("from .. import store", "wallexbot.store"),
                     ("from ..exchange.base import Broker", "wallexbot.exchange.base"),
                     ("def f():\n    from wallexbot.guardian import monitor", "wallexbot.guardian.monitor")]:
        assert hit in wallexbot_imports(src, p), src
