"""Robust coercion of model-supplied tool-call arguments.

Small / heavily-quantized models routinely mangle the *shape* of a tool
argument even when the intent is clear: wrapping a scalar in brackets
(``'[1]'``), nesting a string under a dict key (``{"text": "..."}``),
returning a one-element list where a scalar was asked for, or emitting a
float where an int belongs.  A bare ``int()`` / ``str()`` at the tool
boundary turns that noise into a ``ValueError`` that aborts the whole agent
turn.

These helpers live in their own low-level module (no project imports) so that
every tool-dispatch subsystem - interactive chat (``chat.py``) and the agent
capabilities menu (``agent_capabilities.py``) - can recover from the same
class of malformed output identically, rather than each re-deriving its own
coercion and drifting apart.
"""
import json
import re


def arg_as_str(value, default="") -> str:
    """Coerce a tool-call argument value to a plain string.

    Small models sometimes return a dict (e.g. {"text": "..."}) or other
    non-string type where a string was expected.  This extracts the most
    useful string representation without wrapping it in braces/brackets.
    ``default`` (empty string unless supplied) is returned for an absent
    (``None``) value; any present value still coerces to some string.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Try common keys the model might nest content under
        for key in ("text", "content", "value"):
            if key in value and isinstance(value[key], str):
                return value[key]
        # Last resort: join all string values
        str_vals = [v for v in value.values() if isinstance(v, str)]
        if str_vals:
            return str_vals[0]
        return json.dumps(value)
    if isinstance(value, list):
        return "\n".join(arg_as_str(item) for item in value)
    return str(value)


def arg_as_int(value, default=None):
    """Coerce a tool-call argument value to an int, or ``default`` if unparseable.

    Small models frequently mangle a scalar index: wrapping it in brackets
    (``'[1]'``), returning an actual one-element list (``[1]``), emitting a
    float (``1.0``), or decorating it (``'#2'``, ``'section 3'``).  Rather than
    let ``int()`` raise and abort the whole agent turn, extract the first
    integer we can find and fall back to ``default`` (None unless the caller
    supplies one, ``dict.get``-style: ``arg_as_int(param, 7)``).

    The fallback keys on *unparseable*, not falsiness, so a genuine ``0`` is
    returned as ``0`` - unlike ``as_int(x) if as_int(x) else 7``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        # bool is an int subclass; a boolean index is meaningless noise.
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, list):
        # e.g. the model returned [1] instead of 1 - use the first element.
        return arg_as_int(value[0], default) if value else default
    if isinstance(value, dict):
        for key in ("index", "value", "section_index"):
            if key in value:
                return arg_as_int(value[key], default)
        return default
    # String (or anything else): pull the first signed integer out of the text.
    match = re.search(r'-?\d+', str(value))
    return int(match.group()) if match else default


def arg_as_float(value, default=None):
    """Coerce a tool-call argument value to a float, or ``default`` if unparseable.

    Mirrors :func:`arg_as_int` for ``number``-typed schema args: unwraps
    bracketed strings / one-element lists / nested dicts and extracts the
    first numeric literal (including a decimal part) rather than raising.
    ``default`` (None unless supplied) is returned when nothing parses.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return arg_as_float(value[0], default) if value else default
    if isinstance(value, dict):
        for key in ("value", "index"):
            if key in value:
                return arg_as_float(value[key], default)
        return default
    match = re.search(r'-?\d+(?:\.\d+)?', str(value))
    return float(match.group()) if match else default


def arg_as_list(value, default=None) -> list:
    """Coerce a tool-call argument value to a list of strings.

    ``array``-typed schema args draw a wider range of malformed shapes than
    scalars do, because a model that cannot emit JSON arrays reaches for
    whatever it can: a bare string where a list belongs, a JSON array still
    wrapped in quotes, a newline- or comma-joined run, or a dict keyed by
    ``items``.  Each of those carries the intended list perfectly well, so
    each is unwrapped rather than rejected.

    A bare string is treated as ONE item unless it is JSON or contains
    newlines - splitting every string on commas would corrupt legitimate
    single items that contain one ("Bayes' theorem, applied").
    """
    if value is None:
        return list(default) if default else []
    if isinstance(value, list):
        return [arg_as_str(v) for v in value if arg_as_str(v).strip()]
    if isinstance(value, dict):
        for key in ("items", "values", "list"):
            if key in value:
                return arg_as_list(value[key], default)
        return [arg_as_str(value)]
    text = arg_as_str(value).strip()
    if not text:
        return list(default) if default else []
    if text[0] in "[(" and text[-1] in "])":
        try:
            parsed = json.loads(text.replace("(", "[", 1)[::-1].replace(")", "]", 1)[::-1])
            if isinstance(parsed, list):
                return [arg_as_str(v) for v in parsed if arg_as_str(v).strip()]
        except (ValueError, TypeError):
            pass
    if "\n" in text:
        return [ln.strip() for ln in text.split("\n") if ln.strip()]
    return [text]
