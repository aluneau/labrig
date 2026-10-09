#!/usr/bin/env python3
"""Checks the backend for code that works on this machine's Python but not on RHEL 9 / Alma 10.

- Python 3.9 grammar (no `match`, no parenthesized context managers, ...).
- PEP 701 f-strings (3.12+, which ast.parse(feature_version) doesn't catch): a single-quoted f-string
  spanning lines, or reusing its own quote inside a replacement field.
- Annotations that use a name defined later in the module. Python >= 3.14 evaluates annotations
  lazily, so these import fine there but raise NameError on 3.9-3.13 (class bodies and function
  signatures are evaluated at definition time). `X | None` in annotations is also flagged on 3.9.

Usage: scripts/check-python.py [paths...]   (default: backend/app scripts/vm-manager-helper scripts/vm-manager-update)
"""
import ast
import io
import pathlib
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT = [ROOT / "backend" / "app", ROOT / "scripts" / "vm-manager-helper", ROOT / "scripts" / "vm-manager-update"]


def files(paths):
    for p in paths:
        p = pathlib.Path(p)
        yield from (sorted(p.rglob("*.py")) if p.is_dir() else [p])


def annotations(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if arg is not None and arg.annotation is not None:
                yield arg.annotation
        if node.returns is not None:
            yield node.returns


def fstring_problems(path, src):
    """PEP 701 f-strings, through the 3.12+ tokenizer (FSTRING_START / FSTRING_END tokens)"""
    start_tok = getattr(tokenize, "FSTRING_START", None)
    if start_tok is None:  # running on < 3.12: such code would not parse at all
        return []
    problems, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == start_tok:
            quote = tok.string.lstrip("rRfFbBuU")
            if stack and quote.startswith(stack[-1][1]):
                problems.append(f"{path}:{tok.start[0]}: f-string reuses its quote inside {{}} (Python 3.12+ only)")
            stack.append((tok.start[0], quote))
        elif tok.type == tokenize.FSTRING_END and stack:
            line, quote = stack.pop()
            if len(quote) == 1 and tok.end[0] != line:
                problems.append(f"{path}:{line}: single-quoted f-string spans several lines (Python 3.12+ only)")
        elif tok.type == tokenize.STRING and stack and tok.string.lstrip("rRbBuU").startswith(stack[-1][1]):
            problems.append(f"{path}:{tok.start[0]}: f-string reuses its quote inside {{}} (Python 3.12+ only)")
    return problems


def check(path):
    src = path.read_text()
    try:
        tree = ast.parse(src, feature_version=(3, 9))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: not Python 3.9 syntax: {e.msg}"]
    fstrings = fstring_problems(path, src)
    if fstrings:
        return fstrings
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
           and any(a.name == "annotations" for a in n.names) for n in tree.body):
        return []
    defined_at = {}
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_at.setdefault(node.name, i)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined_at.setdefault(t.id, i)
    problems = []
    for i, node in enumerate(tree.body):
        exprs = list(annotations(node))
        if isinstance(node, ast.ClassDef):
            for b in node.body:
                if isinstance(b, ast.AnnAssign):
                    exprs.append(b.annotation)
                exprs += annotations(b)
        for expr in exprs:
            for n in ast.walk(expr):
                if isinstance(n, ast.Name) and defined_at.get(n.id, -1) > i:
                    problems.append(f"{path}:{n.lineno}: '{n.id}' is used in an annotation before its "
                                    "definition (NameError on Python < 3.14)")
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
                    problems.append(f"{path}:{n.lineno}: 'X | Y' in an annotation fails on Python 3.9 (use Optional/Union)")
    return problems


def main():
    problems = [p for f in files(sys.argv[1:] or DEFAULT) for p in check(f)]
    print("\n".join(sorted(set(problems))) or "Python 3.9 compatibility: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
