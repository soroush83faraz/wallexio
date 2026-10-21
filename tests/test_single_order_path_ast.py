"""Static proof that OMS.submit() is the only place in src/wallexbot that sends an order."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "wallexbot"
TARGET = "submit_order"


def _scan(root: Path = SRC):
    calls, strings = [], []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        stack: list[str] = []

        def visit(node):
            pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if pushed:
                stack.append(node.name)
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == TARGET:
                    calls.append((path.relative_to(root).as_posix(), tuple(stack), node.lineno))
            if isinstance(node, ast.Constant) and node.value == TARGET:
                strings.append((path.relative_to(root).as_posix(), node.lineno))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                stack.pop()

        visit(tree)
    return calls, strings


def test_submit_order_called_only_from_oms_submit():
    calls, _ = _scan()
    assert calls, "expected OMS.submit to call broker.submit_order"
    for rel, scope, line in calls:
        assert rel == "oms.py" and scope == ("OMS", "submit"), f"{rel}:{line} calls submit_order in {scope}"
    assert len(calls) == 1


def test_no_dynamic_access_by_name():
    _, strings = _scan()
    assert strings == [], f"'submit_order' referenced as a string (getattr bypass?): {strings}"


def test_scanner_detects_violations(tmp_path):
    (tmp_path / "rogue.py").write_text(
        "class X:\n    def run(self, b):\n        b.submit_order(client_id='x')\n"
        "        getattr(b, 'submit_order')()\n")
    calls, strings = _scan(tmp_path)
    assert calls == [("rogue.py", ("X", "run"), 3)]
    assert strings == [("rogue.py", 4)]
