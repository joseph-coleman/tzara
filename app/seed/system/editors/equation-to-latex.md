---
type: editor
label: Equation To Latex
description: Convert a selected equation (python or not) and output LaTeX.
scope: selection
operation: replace
vaults: main
max_iterations: 4
log: true
Title: Equation To Latex
Date: 2026-07-30 13:21:55.591883+00:00
Tags: latex, sympy, equation-parsing, python, expression-conversion, parsing, tool
Summary: The user asks to invoke the `equation_to_latex` tool on the currently selected text and return only its exact LaTeX output, without any preamble, commentary, or formatting.
---

# Prompt

The user wants to convert the selected text to LaTeX. Call the tool  `equation_to_latex` and output ONLY its exact return value. No preamble, no commentary, no code fences.

# Tools

```python
import keyword, re
from sympy import Eq, Function, Mul, S, Symbol, latex
from sympy.parsing.sympy_parser import (
    parse_expr,
    standard_transformations,
    convert_xor,
    implicit_multiplication_application,
)

TRANSFORMS = standard_transformations + (
  convert_xor,                          # treat ^ as ** instead of bitwise XOR
  implicit_multiplication_application,  # "2x" -> 2*x, "sin x" -> sin(x)
)

# Names that keep their sympy meaning; every other identifier 
# becomes a plain symbol. 
KEEP = {
  'sin', 'cos', 'tan', 'sec', 'csc', 'cot', 'asin', 'acos',
  'atan', 'atan2', 'sinh', 'cosh', 'tanh', 'asinh', 'acosh',
  'atanh', 'exp', 'log', 'ln', 'sqrt', 'cbrt', 'Abs', 'sign',
  'factorial', 'binomial', 'floor', 'ceiling', 'Max', 'Min', 'erf',
  'Derivative', 'Integral', 'Sum', 'Product', 'diff', 'integrate',
  'limit', 'pi', 'oo',
}

_IDENT = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\s*(\(?)')


def _prepare(text):
    """Map every free identifier to a Symbol (or Function if it's called).
    
    Returns the (possibly rewritten) source and a local_dict. Python keywords
    such as `lambda` are mangled to a safe name that still prints correctly.
    """
    local_dict, out, pos = {}, [], 0
    for m in _IDENT.finditer(text):
        name, called = m.group(1), m.group(2)
        if name in KEEP:
            continue
        safe = f'_kw_{name}' if keyword.iskeyword(name) else name
        local_dict[safe] = Function(name) if called else Symbol(name)
        if safe != name:
            out.append(text[pos:m.start(1)])
            out.append(safe)
            pos = m.end(1)
    out.append(text[pos:])
    return ''.join(out), local_dict


def _tidy(e):
    """Drop the redundant unit factors that evaluate=False leaves behind."""
    if not getattr(e, 'args', ()):
        return e
    args = [_tidy(x) for x in e.args]
    if e.func is Mul:
        kept = [x for x in args if x != S.One] or [S.One]
        return kept[0] if len(kept) == 1 else Mul(*kept, evaluate=False)
    return e.func(*args, evaluate=False)


def equation_to_latex():
    """Convert a math expression string to LaTeX.
    """

    s = editor.selection 
    preserve_order=True
    kw = {}

    if preserve_order:
        kw.setdefault('order', 'none')
    text, local_dict = _prepare(s)
    
    def parse(t):
        e = parse_expr(t, local_dict=local_dict,
                       transformations=TRANSFORMS,
                       evaluate=not preserve_order)
        return _tidy(e) if preserve_order else e
    
    if "=" in text:
        lhs, rhs = text.split("=", 1)
        expr = Eq(parse(lhs), parse(rhs), evaluate=False)  # keep it an equation
    else:
        expr = parse(text)
    return f"$${latex(expr, **kw)}$$"

    
```
