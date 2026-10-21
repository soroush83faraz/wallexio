"""Static guard for bot/: orders leave only through pipeline.route_to_oms (-> OMS.submit).

Complements tests/test_single_order_path_ast.py (which scans all of src/wallexbot, bot/ included):
- nothing in bot/ references submit_order, calls a `.submit(...)` method or any submit-like name;
- bot/ never imports the broker modules (exchange.base / mock_wallex / wallex_private) nor ai/;
- route_to_oms is called from exactly two reviewed scopes of loop.py (entry and exit placement);
- pipeline.route_to_oms itself does nothing but return oms.submit(...);
- risk/, oms.py and exchange/ never import the advisor package (wallexbot.ai) or bot/.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "wallexbot"
BOT = SRC / "bot"
SEND_SCOPES = {("Bot", "_enter"), ("Bot", "_place_exit")}
FORBIDDEN_IMPORTS = ("wallexbot.exchange.base", "wallexbot.exchange.mock_wallex", "wallexbot.exchange.wallex_private",
                     "wallexbot.ai")


def _walk(tree):
    """(node, enclosing scope) pairs."""
    out = []

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = scope + (node.name,)
        out.append((node, scope))
        for c in ast.iter_child_nodes(node):
            visit(c, scope)

    visit(tree, ())
    return out


def _imports(tree):
    mods = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.append(n.module)
            mods += [f"{n.module}.{a.name}" for a in n.names]
    return mods


def test_bot_has_no_order_entry_of_its_own():
    route_calls = []
    for path in sorted(BOT.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        src = path.read_text()
        tree = ast.parse(src)
        for node, scope in _walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "submit_order", f"{rel}: submit_order referenced"
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                assert "submit" not in name.lower(), f"{rel}:{node.lineno} calls {name}()"
                assert name not in ("place_order", "send_order", "create_order", "new_order", "post_order")
                if name == "route_to_oms":
                    route_calls.append((rel, scope))
        for mod in _imports(tree):
            assert not mod.startswith(FORBIDDEN_IMPORTS), f"{rel} imports {mod}"
            assert mod.split(".")[-1] not in ("MockWallex", "WallexBroker"), f"{rel} imports {mod}"
    assert sorted(route_calls) == sorted(("bot/loop.py", s) for s in SEND_SCOPES)


def test_route_to_oms_is_a_bare_pass_through_to_oms_submit():
    tree = ast.parse((SRC / "pipeline.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "route_to_oms")
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Return)
    call = body[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "submit"
    assert isinstance(call.func.value, ast.Name) and call.func.value.id == "oms"


def test_order_path_never_imports_the_advisor_or_the_bot():
    paths = [SRC / "oms.py", SRC / "pipeline.py", *sorted((SRC / "risk").rglob("*.py")),
             *sorted((SRC / "exchange").rglob("*.py"))]
    for path in paths:
        for mod in _imports(ast.parse(path.read_text())):
            assert not mod.startswith(("wallexbot.ai", "wallexbot.bot")), f"{path.name} imports {mod}"


def test_bot_uses_no_dynamic_attribute_access():
    banned = {"getattr", "setattr", "delattr", "eval", "exec", "compile", "__import__", "vars", "globals"}
    for path in sorted(BOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name):
                assert node.id not in banned, f"{path.name}: {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("__dict__", "__getattribute__", "__getattr__"), f"{path.name}: {node.attr}"
