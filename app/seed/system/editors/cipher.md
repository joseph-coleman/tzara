---
type: editor
label: "Decoder Ring"
description: Encode the selection with a classic cipher ROT13, a decoder ring for your diary.
scope: selection
operation: replace
Tags: cipher, rot13, python, text-encoding
GenerateMetadata: false
Summary: This demonstrates a simple tool call that works on a selection of text to rotate the letters by 13 places. Using the tool again on the encoded text will decode it.  The "label" field is used to list this as a "Decoder Ring" in the editor's slash menu.
---

# Prompt

The user wants to encode the selected text. Call the tool `rot13` and output ONLY its exact return value. No preamble, no commentary, no code fences.

```python
def rot13():
    """ROT13-encode the selected text."""
    import codecs
    return codecs.encode(editor.selection, "rot_13")
```
