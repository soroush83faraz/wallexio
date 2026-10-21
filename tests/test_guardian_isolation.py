"""AST proof that the guardian cannot pull in strategy, backtest or AI code.

Checked statically over src/wallexbot: (1) no guardian module imports wallexbot.strategy,
wallexbot.backtest or wallexbot.ai (absolute, relative, `import x.y`, `from x import y`, at any
nesting level, including function-local imports); (2) the same holds TRANSITIVELY for every
wallexbot module reachable from the guardian through imports, so a helper cannot smuggle strategy
code in. Broker-module isolation is enforced by tests/test_single_order_path_ast.py."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wallexbot"
FORBIDDEN = ("wallexbot.strategy", "wallexbot.backtest", "wallexbot.ai")
GUARDIAN = sorted(p for p in (SRC / "guardian").glob("*.py"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package(path: Path) -> list[str]:
    name = _module_name(path).split(".")
    return name if path.name == "__init__.py" else name[:-1]


def imports_of(source: str, path: Path) -> set[str]:
    """Every module name an import statement in `source` can load (x, x.y, and x.y.name for
    `from x.y import name`, since name may itself be a submodule)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = _package(path)
                base = pkg[: len(pkg) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def _is_forbidden(mod: str) -> bool:
    return any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN)


def _resolve(mod: str) -> Path | None:
    if not mod.startswith("wallexbot"):
        return None
    rel = Path(*mod.split("."))
    for cand in (SRC.parent / rel.with_suffix(".py"), SRC.parent / rel / "__init__.py"):
        if cand.exists():
            return cand
    return None


def closure(roots: list[Path]) -> dict[str, list[str]]:
    """wallexbot module -> import chain from a guardian module, for everything reachable."""
    seen: dict[str, list[str]] = {}
    stack = [(p, [_module_name(p)]) for p in roots]
    while stack:
        path, chain = stack.pop()
        name = _module_name(path)
        if name in seen:
            continue
        seen[name] = chain
        parts = name.split(".")
        for i in range(1, len(parts)):  # importing a.b.c also runs a/__init__ and a/b/__init__
            pkg = _resolve(".".join(parts[:i]))
            if pkg is not None and _module_name(pkg) not in seen:
                stack.append((pkg, chain + [_module_name(pkg)]))
        for mod in imports_of(path.read_text(), path):
            target = _resolve(mod)
            if target is not None:
                stack.append((target, chain + [_module_name(target)]))
            elif _is_forbidden(mod):
                seen[mod] = chain + [mod]
    return seen


def test_guardian_modules_exist():
    assert {p.name for p in GUARDIAN} >= {"__init__.py", "monitor.py", "flatten.py", "main.py"}


@pytest.mark.parametrize("path", GUARDIAN, ids=lambda p: p.name)
def test_guardian_module_imports_no_strategy_backtest_or_ai(path):
    bad = sorted(m for m in imports_of(path.read_text(), path) if _is_forbidden(m))
    assert bad == [], f"{path.name} imports {bad}"


def test_nothing_reachable_from_the_guardian_imports_strategy_backtest_or_ai():
    reach = closure(GUARDIAN)
    bad = {m: " -> ".join(chain) for m, chain in reach.items() if _is_forbidden(m)}
    assert bad == {}, bad
    # sanity: the walk really followed imports (guardian -> oms -> store ...)
    assert {"wallexbot.oms", "wallexbot.store", "wallexbot.risk.engine"} <= set(reach)


@pytest.mark.parametrize("src", [
    "from wallexbot.strategy.contract import Signal\n",
    "import wallexbot.backtest\n",
    "from wallexbot import ai\n",
    "from ..strategy import contract\n",
    "from .. import backtest\n",
    "def f():\n    import wallexbot.ai.models as m\n",
])
def test_scanner_catches_forbidden_imports(src):
    fake = SRC / "guardian" / "rogue.py"
    assert any(_is_forbidden(m) for m in imports_of(src, fake)), src
