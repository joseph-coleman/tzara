---
type: editor
label: "Decoder Ring"
description: "Encode the selection with a classic cipher (ROT13, Atbash, or reverse) - a decoder ring for your diary."
scope: selection
operation: replace
Tags: cipher, rot13, atbash, reverse, python, text-encoding
Summary: The prompt directs encoding of the selected text with a specified cipher—ROT13, Atbash, or reverse—defaulting to ROT13 if none is given, and requires outputting only the cipher’s exact result with no additional text. It provides Python functions that implement each of these three encoding methods.
---

# Prompt

The user wants to encode the selected text. Choose the cipher they named (ROT13, Atbash, or reverse); if none is specified, use ROT13. Call the matching tool and output ONLY its exact return value. No preamble, no commentary, no code fences.

```python
def rot13():
    """ROT13-encode the selected text."""
    import codecs
    return codecs.encode(editor.selection, "rot_13")

def atbash():
    """Atbash-cipher the selected text (a<->z, b<->y, ...)."""
    import string
    lo, up = string.ascii_lowercase, string.ascii_uppercase
    table = str.maketrans(lo + up, lo[::-1] + up[::-1])
    return editor.selection.translate(table)

def reverse():
    """Reverse the selected text."""
    return editor.selection[::-1]
```
