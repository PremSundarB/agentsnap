"""Function-level change detection for Python files (stretch goal).

Given the old and new text of a file, report which functions/classes the
edit touched, as dotted names like ``Parser.parse`` (or ``<module>`` for
top-level code). Two agents that edited the same file but disjoint sets of
symbols are very likely to merge cleanly; the conflict report says so.

Only Python is supported for now; other languages return ``None``
("unknown") and the caller falls back to file-level reporting.
"""

from __future__ import annotations

import ast
import difflib

MODULE = "<module>"


def _symbol_spans(source: str) -> list[tuple[int, int, str]] | None:
    """(first_line, last_line, dotted_name) for every def/class, 1-based."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                spans.append((first, child.end_lineno or child.lineno, name))
                visit(child, name + ".")

    visit(tree, "")
    return spans


def _innermost(spans: list[tuple[int, int, str]], line: int) -> str:
    best = None
    for first, last, name in spans:
        if first <= line <= last and (best is None or first >= best[0]):
            best = (first, last, name)
    return best[2] if best else MODULE


def changed_symbols(old: str, new: str) -> frozenset[str] | None:
    """Symbols touched by the edit old -> new, or None if either side won't parse."""
    old_spans, new_spans = _symbol_spans(old), _symbol_spans(new)
    if old_spans is None or new_spans is None:
        return None
    old_lines, new_lines = old.splitlines(), new.splitlines()
    touched: set[str] = set()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # Removed/replaced lines are looked up in the old file, inserted
        # lines in the new file (which also catches brand-new functions).
        # Blank lines are separators, not code, so they touch nothing.
        for ln in range(i1 + 1, i2 + 1):
            if old_lines[ln - 1].strip():
                touched.add(_innermost(old_spans, ln))
        for ln in range(j1 + 1, j2 + 1):
            if new_lines[ln - 1].strip():
                touched.add(_innermost(new_spans, ln))
    return frozenset(touched)


def is_python(path: str) -> bool:
    return path.endswith((".py", ".pyi"))
