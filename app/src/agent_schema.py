# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static (AST-based) tool-schema derivation for agent-file custom tools.

Agent files carry human-authored Python functions in fenced ```python blocks.
The model needs Ollama tool schemas for them, but `inspect.signature` /
`get_type_hints` require LIVE function objects - which would mean exec'ing
agent code on the worker at registry-reconcile time (import side effects,
syntax errors crashing the loader). Per the design review, derivation is
STATIC instead: `ast.parse` extracts names, parameter annotation strings, and
docstrings with zero execution anywhere - the agent kernel remains the only
place agent code ever runs, and `ast.parse` doubles as free syntax validation
at load time.

Fidelity contract (matches the umbrella design): everything past the bare
signature is optional. Docstring -> description; common annotation strings map
to JSON-schema types; anything exotic falls back to "string" (the model copes;
semantics live in the prompt the author writes anyway).
"""

import ast

# Annotation-string -> JSON-schema type. Keys are compared on the UNPARSED
# annotation with whitespace stripped; subscripted generics match on the base.
_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "tuple": "array",
    "set": "array",
    "dict": "object",
    "none": "null",
}


def _annotation_to_type(annotation: ast.expr | None) -> str:
    if annotation is None:
        return "string"
    try:
        text = ast.unparse(annotation).strip()
    except Exception:
        return "string"
    base = text.split("[", 1)[0].strip().lower()
    # "str | None" style unions: use the first non-none member.
    if "|" in text:
        for part in text.split("|"):
            t = part.split("[", 1)[0].strip().lower()
            if t != "none":
                base = t
                break
    # typing.Optional[str] / typing.List[str]
    if base.startswith("typing."):
        base = base[len("typing."):]
    if base == "optional":
        inner = text.split("[", 1)[1].rsplit("]", 1)[0] if "[" in text else "str"
        base = inner.split("[", 1)[0].strip().lower()
    return _TYPE_MAP.get(base, "string")


def validate_source(py_source: str) -> list[str]:
    """Syntax-check only (no execution). Returns a list of error strings."""
    try:
        ast.parse(py_source)
        return []
    except SyntaxError as e:
        return [f"python syntax error: {e.msg} (line {e.lineno})"]


def functions_from_source(py_source: str) -> list[dict]:
    """Extract top-level function defs as Ollama tool schemas - statically.

    Returns [{"name", "schema", "params"}] where schema is the full
    `{"type": "function", "function": {...}}` dict the loop passes to the
    model. Functions whose names start with `_` are treated as private
    helpers and skipped (authors can factor code without bloating the tool
    list). Raises nothing: call validate_source first for syntax errors.
    """
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return []

    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue

        args = node.args
        properties: dict = {}
        required: list[str] = []
        # Positional + keyword params, aligned with their defaults from the right.
        positional = list(args.posonlyargs) + list(args.args)
        n_defaults = len(args.defaults)
        first_defaulted = len(positional) - n_defaults
        for i, a in enumerate(positional):
            if a.arg == "self":
                continue
            properties[a.arg] = {"type": _annotation_to_type(a.annotation)}
            if i < first_defaulted:
                required.append(a.arg)
        for a, default in zip(args.kwonlyargs, args.kw_defaults):
            properties[a.arg] = {"type": _annotation_to_type(a.annotation)}
            if default is None:
                required.append(a.arg)

        doc = ast.get_docstring(node) or f"Custom tool {node.name} (see agent prompt)."
        out.append({
            "name": node.name,
            "params": list(properties.keys()),
            "schema": {"type": "function", "function": {
                "name": node.name,
                "description": doc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }},
        })
    return out
