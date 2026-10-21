"""Lint-style guard: OMS.submit() is the only place in src/wallexbot that sends an order.

This is a static, best-effort check over the AST of every module in src/wallexbot. It is NOT a
proof: Python is dynamic enough (code loaded from data files, C extensions, monkeypatching from
outside src/, obfuscation through constructs not listed below) that no AST lint can rule out every
path. What it does guarantee is that none of the following appear anywhere in src/wallexbot:

1. `submit_order` as an attribute anywhere except as the callee of the single call
   `self.broker.submit_order(...)` inside `OMS.submit` in oms.py (so no aliasing such as
   `send = b.submit_order`, no passing `f(b.submit_order)`, no second call site); and exactly one
   such call exists. `submit_order` as a bare name, import alias, keyword, parameter or function
   name is flagged too (method definitions are allowed only under exchange/).
2. String material that could build the name: any string constant containing "submit_order", any
   string constant that is itself a >= 4-char fragment of it ("submit", "_order", "order", ...),
   any concatenation / f-string / %-format / str.join|format whose literal pieces include a
   >= 3-char fragment, or whose literal pieces fold to a string containing it. Docstrings (bare
   string statements) are ignored.
3. Dynamic attribute / code access outside an explicit allow-list (ALLOWED_DYNAMIC, by file,
   scope and name): getattr, setattr, delattr, vars, globals, locals, eval, exec, compile,
   __import__, __builtins__, attrgetter, methodcaller, import_module, getmembers,
   __getattribute__, __getattr__, __setattr__, __dict__; and importing operator, importlib,
   inspect or builtins.
4. Outside oms.py, pipeline.py and exchange/: importing the broker modules
   (wallexbot.exchange.mock_wallex / .base, the wallexbot.exchange package itself) or referencing
   MockWallex / mock_wallex, and calling any submit-like method (`*submit*`, `place_order`,
   `send_order`, `create_order`, `new_order`, `post_order`).
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wallexbot"
TARGET = "submit_order"
SEND_SITE = ("oms.py", ("OMS", "submit"))

DYNAMIC_NAMES = frozenset({"getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec",
                           "compile", "__import__", "__builtins__", "attrgetter", "methodcaller",
                           "import_module", "getmembers"})
DYNAMIC_ATTRS = frozenset({"__getattribute__", "__getattr__", "__setattr__", "__dict__", "__import__",
                           "__builtins__", "attrgetter", "methodcaller", "import_module", "getmembers"})
DYNAMIC_MODULES = frozenset({"operator", "importlib", "inspect", "builtins"})
# (file, enclosing scope, name) -> number of uses allowed. Each entry was reviewed: none of these
# can reach a broker object.
ALLOWED_DYNAMIC: dict[tuple[str, tuple[str, ...], str], int] = {
    ("store.py", ("_jsonable",), "getattr"): 1,                       # dataclass field values
    ("store.py", ("Store", "append_risk_decision"), "getattr"): 1,    # optional decision.hard_breach
    ("redact.py", ("Secret", "__init__"), "__setattr__"): 1,          # frozen Secret's own slot
    ("redact.py", ("_is_numpy_number",), "getattr"): 1,               # numpy scalar type lookup
    ("redact.py", (), "vars"): 1,                         # logging.LogRecord field names
    ("redact.py", ("RedactingFilter", "filter"), "vars"): 1,          # scrub extra= LogRecord fields
}

# (file, enclosing scope) -> number of `self.oms.submit(...)` calls allowed outside the order-path
# modules. Reviewed: the guardian's flatten procedure must place reduce-only exits, and does so
# through OMS.submit (risk check, kill-switch gate, idempotent client_id, audit), never through
# the broker. The receiver must literally be `self.oms`; any other submit-like call is flagged.
ALLOWED_OMS_SUBMIT: dict[tuple[str, tuple[str, ...]], int] = {
    ("guardian/flatten.py", ("_Flattener", "_place_exit")): 1,
}

BROKER_MODULES = frozenset({"wallexbot.exchange.mock_wallex", "wallexbot.exchange.base", "wallexbot.exchange"})
BROKER_NAMES = frozenset({"MockWallex", "mock_wallex"})
SUBMIT_LIKE = re.compile(r"submit|^(place|send|create|new|post)_?orders?$", re.IGNORECASE)


def _broker_ok(rel: str) -> bool:
    return rel in ("oms.py", "pipeline.py") or rel.startswith("exchange/")


def _fragment(s: str, min_len: int) -> bool:
    return len(s) >= min_len and s.lower() in TARGET


@dataclass(frozen=True)
class Violation:
    rel: str
    line: int
    rule: str
    detail: str


_PLACEHOLDER = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[a-zA-Z%]|\{[^{}]*\}")


def _string_pieces(node: ast.AST) -> list[str] | None:
    """Literal pieces of a string-building expression, in order, with %- and {}-placeholders cut
    out ("sub%s" -> ["sub", ""]); None if the node does not build a string."""
    raw = _raw_pieces(node)
    return None if raw is None else [p for r in raw for p in _PLACEHOLDER.split(r)]


def _raw_pieces(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        pieces = []
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                pieces.append(side.value)
            else:
                sub = _raw_pieces(side)
                if sub is not None:
                    pieces.extend(sub)
        return pieces
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
            node.func.attr in ("join", "format", "replace", "__add__", "format_map"):
        pieces = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        return pieces or None
    return None


def _resolve_from(rel: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    pkg = ["wallexbot", *rel.split("/")[:-1]]
    base = pkg[: len(pkg) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def scan_source(source: str, rel: str) -> tuple[list[Violation], list[tuple[str, tuple[str, ...], int]]]:
    """Scan one module. Returns (violations, allowed send sites)."""
    tree = ast.parse(source, filename=rel)
    parents: dict[ast.AST, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    out: list[Violation] = []
    sends: list[tuple[str, tuple[str, ...], int]] = []
    dyn_used: dict[tuple[str, tuple[str, ...], str], list[int]] = {}
    oms_submits: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    seen_pieces: set[int] = set()

    def flag(node, rule, detail):
        out.append(Violation(rel, getattr(node, "lineno", 0), rule, detail))

    def dynamic(node, name, scope):
        dyn_used.setdefault((rel, scope, name), []).append(getattr(node, "lineno", 0))

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == TARGET and not (rel.startswith("exchange/") and isinstance(node, ast.FunctionDef)):
                flag(node, "name", f"defines {TARGET} outside exchange/")
            scope = scope + (node.name,)
        # 1. the attribute itself
        if isinstance(node, ast.Attribute) and node.attr == TARGET:
            parent = parents.get(node)
            is_callee = isinstance(parent, ast.Call) and parent.func is node
            on_self_broker = isinstance(node.value, ast.Attribute) and node.value.attr == "broker" and \
                isinstance(node.value.value, ast.Name) and node.value.value.id == "self"
            if is_callee and on_self_broker and (rel, scope) == SEND_SITE:
                sends.append((rel, scope, node.lineno))
            else:
                flag(node, "attribute", f"{TARGET} referenced in {scope or '<module>'}"
                     + ("" if is_callee else " without being called (alias / pass-through)"))
        if isinstance(node, ast.Name) and node.id == TARGET:
            flag(node, "name", f"bare name {TARGET}")
        if isinstance(node, ast.keyword) and node.arg == TARGET:
            flag(node.value, "name", f"keyword {TARGET}=")
        if isinstance(node, ast.arg) and node.arg == TARGET:
            flag(node, "name", f"parameter {TARGET}")
        # 2. string material
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            low = node.value.lower()
            if TARGET in low:
                flag(node, "string", f"string contains {TARGET!r}")
            elif _fragment(node.value, 4):
                flag(node, "string", f"string {node.value!r} is a fragment of {TARGET!r}")
        if id(node) not in seen_pieces:
            pieces = _string_pieces(node)
            if pieces is not None:
                for n in ast.walk(node):
                    seen_pieces.add(id(n))
                if TARGET in "".join(pieces).lower():
                    flag(node, "string", f"string pieces fold to {TARGET!r}")
                elif any(_fragment(p, 3) for p in pieces):
                    flag(node, "string", f"string building uses a fragment of {TARGET!r}: {pieces}")
        # 3. dynamic access
        if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            dynamic(node, node.id, scope)
        if isinstance(node, ast.Attribute) and node.attr in DYNAMIC_ATTRS:
            dynamic(node, node.attr, scope)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in DYNAMIC_MODULES:
                    flag(node, "dynamic", f"imports {a.name}")
        if isinstance(node, ast.ImportFrom):
            mod = _resolve_from(rel, node)
            if mod.split(".")[0] in DYNAMIC_MODULES:
                flag(node, "dynamic", f"imports from {mod}")
            for a in node.names:
                if a.name in DYNAMIC_NAMES or a.name == TARGET or a.asname == TARGET:
                    flag(node, "dynamic", f"imports {a.name}")
        # 4. broker access outside the order path
        if not _broker_ok(rel):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if any(a.name == m or a.name.startswith(m + ".") for m in BROKER_MODULES - {"wallexbot.exchange"}) \
                            or a.name == "wallexbot.exchange":
                        flag(node, "broker", f"imports {a.name}")
            if isinstance(node, ast.ImportFrom):
                mod = _resolve_from(rel, node)
                names = {a.name for a in node.names}
                if mod in BROKER_MODULES - {"wallexbot.exchange"} or names & BROKER_NAMES or \
                        (mod == "wallexbot.exchange" and names & {"mock_wallex", "base", "*"}) or \
                        (mod == "wallexbot" and "exchange" in names):
                    flag(node, "broker", f"imports {sorted(names)} from {mod}")
            if isinstance(node, ast.Name) and node.id in BROKER_NAMES:
                flag(node, "broker", f"references {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in BROKER_NAMES:
                flag(node, "broker", f"references .{node.attr}")
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                on_self_oms = isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and \
                    f.value.attr == "oms" and isinstance(f.value.value, ast.Name) and f.value.value.id == "self"
                if name == "submit" and on_self_oms and (rel, scope) in ALLOWED_OMS_SUBMIT:
                    oms_submits.setdefault((rel, scope), []).append(node.lineno)
                elif name and SUBMIT_LIKE.search(name):
                    flag(node, "broker", f"calls submit-like {name}()")
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    for key, lines in oms_submits.items():
        if len(lines) > ALLOWED_OMS_SUBMIT[key]:
            out.append(Violation(rel, lines[-1], "broker", f"oms.submit called {len(lines)}x in {key[1]} "
                                 f"(allowed {ALLOWED_OMS_SUBMIT[key]})"))
    for key, lines in dyn_used.items():
        if len(lines) > ALLOWED_DYNAMIC.get(key, 0):
            out.append(Violation(rel, lines[-1], "dynamic",
                                 f"{key[2]} used {len(lines)}x in {key[1] or '<module>'} (allowed "
                                 f"{ALLOWED_DYNAMIC.get(key, 0)})"))
    return out, sends


def scan_tree(root: Path = SRC):
    violations, sends = [], []
    for path in sorted(root.rglob("*.py")):
        v, s = scan_source(path.read_text(), path.relative_to(root).as_posix())
        violations += v
        sends += s
    return violations, sends


# ----------------------------------------------------------------------------- real source
def test_real_source_is_clean():
    violations, _ = scan_tree()
    assert violations == [], "\n".join(f"{v.rel}:{v.line} [{v.rule}] {v.detail}" for v in violations)


def test_exactly_one_send_site_in_oms_submit():
    _, sends = scan_tree()
    assert [(r, s) for r, s, _ in sends] == [SEND_SITE]


def test_allow_list_entries_are_all_still_needed():
    """A stale allow-list entry would silently permit a future use; keep it exact."""
    used: dict = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text())

        def visit(node, scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = scope + (node.name,)
            name = node.id if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES else \
                node.attr if isinstance(node, ast.Attribute) and node.attr in DYNAMIC_ATTRS else None
            if name:
                used[(rel, scope, name)] = used.get((rel, scope, name), 0) + 1
            for c in ast.iter_child_nodes(node):
                visit(c, scope)

        visit(tree, ())
    assert used == ALLOWED_DYNAMIC


# ----------------------------------------------------------------------------- rogue snippets
ROGUE = {
    "direct call elsewhere": ("strategy/rogue.py", "def run(b):\n    b.submit_order(client_id='x')\n"),
    "alias then call": ("strategy/rogue.py", "def run(b):\n    send = b.submit_order\n    send()\n"),
    "pass bound method": ("strategy/rogue.py", "def run(b, f):\n    f(b.submit_order)\n"),
    "alias inside OMS.submit": ("oms.py", "class OMS:\n    def submit(self):\n"
                                          "        s = self.broker.submit_order\n        s()\n"),
    "second call in OMS.submit on other object": ("oms.py", "class OMS:\n    def submit(self, b):\n"
                                                            "        b.submit_order()\n"),
    "call from other OMS method": ("oms.py", "class OMS:\n    def other(self):\n"
                                             "        self.broker.submit_order()\n"),
    "getattr literal": ("strategy/rogue.py", "def run(b):\n    getattr(b, 'submit_order')()\n"),
    "getattr concatenation": ("strategy/rogue.py", "def run(b):\n    getattr(b, 'submit_' + 'order')()\n"),
    "getattr 2-char pieces": ("strategy/rogue.py",
                              "def run(b):\n    getattr(b, 'su' + 'bm' + 'it' + '_o' + 'rd' + 'er')()\n"),
    "getattr via variable": ("strategy/rogue.py", "def run(b, n):\n    getattr(b, n)()\n"),
    "f-string": ("strategy/rogue.py", "def run(b, x):\n    name = f'submit_{x}'\n"),
    "join": ("strategy/rogue.py", "def run():\n    return '_'.join(['submit', 'order'])\n"),
    "percent format": ("strategy/rogue.py", "def run(x):\n    return 'sub%s' % x\n"),
    "fragment constant": ("risk/rogue.py", "PREFIX = 'submit'\n"),
    "suffix constant": ("risk/rogue.py", "SUFFIX = '_order'\n"),
    "attrgetter": ("strategy/rogue.py", "from operator import attrgetter\nf = attrgetter('x')\n"),
    "operator module": ("strategy/rogue.py", "import operator\n"),
    "methodcaller attr": ("strategy/rogue.py", "import functools\nf = functools.methodcaller\n"),
    "vars": ("strategy/rogue.py", "def run(b):\n    return vars(b)\n"),
    "__dict__": ("strategy/rogue.py", "def run(b):\n    return type(b).__dict__\n"),
    "__getattribute__": ("strategy/rogue.py", "def run(b, n):\n    return b.__getattribute__(n)\n"),
    "importlib": ("strategy/rogue.py", "import importlib\nm = importlib.import_module('x')\n"),
    "__import__": ("strategy/rogue.py", "m = __import__('x')\n"),
    "eval": ("strategy/rogue.py", "def run(code):\n    eval(code)\n"),
    "setattr": ("strategy/rogue.py", "def run(b, f):\n    setattr(b, 'x', f)\n"),
    "allow-listed name, other scope": ("store.py", "def other(o):\n    return getattr(o, 'x')\n"),
    "allow-listed scope, one use too many": ("store.py", "def _jsonable(o):\n    getattr(o, 'a')\n"
                                                         "    getattr(o, 'b')\n"),
    "import MockWallex": ("strategy/rogue.py", "from wallexbot.exchange.mock_wallex import MockWallex\n"),
    "import MockWallex aliased": ("ai/rogue.py", "from wallexbot.exchange.mock_wallex import MockWallex as M\n"),
    "import broker module": ("guardian/rogue.py", "import wallexbot.exchange.mock_wallex as mw\n"),
    "import broker base": ("risk/rogue.py", "from wallexbot.exchange.base import Broker\n"),
    "import exchange package": ("risk/rogue.py", "from wallexbot import exchange\n"),
    "relative import": ("strategy/rogue.py", "from ..exchange import mock_wallex\n"),
    "relative import of module": ("strategy/rogue.py", "from ..exchange.mock_wallex import MockClock\n"),
    "submit-like call": ("strategy/rogue.py", "def run(x):\n    x.place_order(1)\n"),
    "submit call outside oms": ("risk/rogue.py", "def run(oms, i):\n    oms.submit(i)\n"),
    "keyword named submit_order": ("strategy/rogue.py", "def run(f, b):\n    f(submit_order=b)\n"),
    "import alias submit_order": ("strategy/rogue.py", "from x import y as submit_order\n"),
    "define submit_order outside exchange": ("risk/rogue.py", "def submit_order(**kw):\n    pass\n"),
    "oms.submit in another guardian scope": ("guardian/flatten.py",
                                             "class _Flattener:\n    def other(self, i):\n"
                                             "        self.oms.submit(i)\n"),
    "oms.submit twice in the allowed scope": ("guardian/flatten.py",
                                              "class _Flattener:\n    def _place_exit(self, i):\n"
                                              "        self.oms.submit(i)\n        self.oms.submit(i)\n"),
    "broker.submit in the allowed scope": ("guardian/flatten.py",
                                           "class _Flattener:\n    def _place_exit(self, i):\n"
                                           "        self.broker.submit(i)\n"),
    "aliased oms in the allowed scope": ("guardian/flatten.py",
                                         "class _Flattener:\n    def _place_exit(self, o, i):\n"
                                         "        o.submit(i)\n"),
    "oms.submit in guardian monitor": ("guardian/monitor.py",
                                       "class Guardian:\n    def tick(self, i):\n        self.oms.submit(i)\n"),
}


@pytest.mark.parametrize("label", sorted(ROGUE))
def test_scanner_flags_bypass(label):
    rel, src = ROGUE[label]
    violations, sends = scan_source(src, rel)
    assert violations, f"bypass not flagged: {label}"
    assert sends == [] or label.startswith(("second call", "alias inside"))


def test_oms_submit_allow_list_entries_are_all_used_exactly():
    """The guardian's exit placement is the one reviewed OMS.submit caller outside the order path."""
    seen: dict = {}
    for (rel, scope), n in ALLOWED_OMS_SUBMIT.items():
        tree = ast.parse((SRC / rel).read_text())

        def visit(node, sc):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sc = sc + (node.name,)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "submit":
                seen[(rel, sc)] = seen.get((rel, sc), 0) + 1
            for c in ast.iter_child_nodes(node):
                visit(c, sc)

        visit(tree, ())
    assert seen == ALLOWED_OMS_SUBMIT


def test_scanner_accepts_the_legitimate_send_site():
    src = "class OMS:\n    def submit(self, i):\n        return self.broker.submit_order(client_id=i)\n"
    assert scan_source(src, "oms.py") == ([], [("oms.py", ("OMS", "submit"), 3)])


def test_scanner_ignores_docstrings_and_unrelated_strings():
    src = '"""Mentions submit_order in prose."""\nKIND = "submit_attempt"\nX = f"{KIND}_ack"\n'
    assert scan_source(src, "risk/ok.py") == ([], [])
