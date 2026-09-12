// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Markdown formatting keys for the /edit/ view CodeMirror editor.

     Mod-b  **bold**      Mod-Shift-x  ~~strikethrough~~    Mod-e  `code`
     Mod-i  *italic*      Mod-Shift-h  ==highlight==        Mod-k  [[wikilink]]

   Each key toggles: on text already wrapped in its markers (just outside the
   selection, or the selection's own first and last characters) it removes them,
   otherwise it wraps. Every selection range is handled on its own, so multiple
   cursors work. Mod-k on an empty selection inserts `[[]]` and opens the
   wikilink autocomplete (wikilink_complete.js).

   The pure helpers are top-level named functions so app/.test/test_edit_keys.js
   can slice them out by name.
*/

// Marker syntax for a format kind. Run-based kinds repeat `char` `size` times;
// `alt` is an equivalent character recognized when removing, never inserted.
// Bold and italic share `*`, so they are told apart by run length (`emphasis`).
function formatSpec(kind) {
  switch (kind) {
    case "bold":      return { char: "*", alt: "_", size: 2, emphasis: true };
    case "italic":    return { char: "*", alt: "_", size: 1, emphasis: true };
    case "strike":    return { char: "~", size: 2 };
    case "highlight": return { char: "=", size: 2 };
    case "code":      return { char: "`", size: 1 };
    case "link":      return { open: "[[", close: "]]" };
    default:          return null;
  }
}

// Length of the run of `ch` ending at `pos`, not reaching below `floor`.
function runBefore(text, pos, ch, floor) {
  let n = 0;
  while (pos - n > floor && text[pos - n - 1] === ch) n++;
  return n;
}

// Length of the run of `ch` starting at `pos`, not reaching `ceil`.
function runAfter(text, pos, ch, ceil) {
  let n = 0;
  while (pos + n < ceil && text[pos + n] === ch) n++;
  return n;
}

// Whether a marker run of length `run` (the shorter side) carries this format.
// `***` is bold AND italic: italic owns the odd star, bold the pair.
function runCarries(spec, run) {
  if (spec.emphasis) return spec.size === 1 ? run % 2 === 1 : run >= 2;
  return run >= spec.size;
}

// `_` only delimits emphasis at a word boundary (snake_case is not italic).
function isWordChar(ch) {
  return !!ch && /[\p{L}\p{N}]/u.test(ch);
}

// Wrap [from, to) in open/close, leaving surrounding whitespace outside the
// markers (`**word **` is not bold in CommonMark). The selection keeps covering
// the wrapped text; an empty one ends up between the markers.
function planWrap(text, from, to, open, close) {
  const inner = text.slice(from, to);
  let a = from, b = from;
  if (inner.trim()) {
    a = from + (inner.length - inner.trimStart().length);
    b = to - (inner.length - inner.trimEnd().length);
  }
  const changes = a === b
    ? [{ from: a, to: a, insert: open + close }]
    : [{ from: a, to: a, insert: open }, { from: b, to: b, insert: close }];
  return { changes, from: a + open.length, to: b + open.length };
}

// Toggle for fixed open/close strings (`[[` `]]`).
function planLiteral(text, from, to, open, close) {
  if (from >= open.length && text.slice(from - open.length, from) === open
      && text.slice(to, to + close.length) === close) {
    return { changes: [{ from: from - open.length, to: from, insert: "" },
                       { from: to, to: to + close.length, insert: "" }],
             from: from - open.length, to: to - open.length };
  }
  const inner = text.slice(from, to);
  if (inner.length >= open.length + close.length
      && inner.startsWith(open) && inner.endsWith(close)) {
    return { changes: [{ from, to: from + open.length, insert: "" },
                       { from: to - close.length, to, insert: "" }],
             from, to: to - open.length - close.length };
  }
  return planWrap(text, from, to, open, close);
}

// Plan one range's toggle over `text`, a window of the document (positions are
// window-relative). Returns {changes, from, to}: changes against the window as it
// is, and the new selection in the window after those changes.
function planToggle(text, from, to, kind) {
  const spec = formatSpec(kind);
  if (spec.open) return planLiteral(text, from, to, spec.open, spec.close);
  const n = spec.size;
  for (const ch of spec.alt ? [spec.char, spec.alt] : [spec.char]) {
    const underscore = ch === "_";
    // Outside: markers hug the selection (also a caret between a fresh pair).
    const left = runBefore(text, from, ch, 0);
    const right = runAfter(text, to, ch, text.length);
    if (runCarries(spec, Math.min(left, right))
        && !(underscore && (isWordChar(text[from - left - 1]) || isWordChar(text[to + right])))) {
      return { changes: [{ from: from - n, to: from, insert: "" },
                         { from: to, to: to + n, insert: "" }],
               from: from - n, to: to - n };
    }
    // Inside: the selection itself starts and ends with the markers.
    const inLeft = runAfter(text, from, ch, to);
    const inRight = runBefore(text, to, ch, from);
    if (to - from >= 2 * n && inLeft < to - from
        && runCarries(spec, Math.min(inLeft, inRight))
        && !(underscore && (isWordChar(text[from - 1]) || isWordChar(text[to])))) {
      return { changes: [{ from, to: from + n, insert: "" },
                         { from: to - n, to, insert: "" }],
               from, to: to - 2 * n };
    }
  }
  const marker = spec.char.repeat(n);
  return planWrap(text, from, to, marker, marker);
}

(function () {
  // Characters of context each side of a selection that a toggle inspects:
  // more than the longest marker run that means anything (`***`).
  const PAD = 8;

  // A pending writing-assistant proposal owns its ranges; formatting under it
  // would shift them.
  function pending(state) {
    const ea = window.EditAssist;
    return !!(ea && ea.hasPending && ea.hasPending(state));
  }

  function toggle(view, kind) {
    const { state } = view;
    // EditorSelection is not a bundle export; the live selection's class is it.
    const EditorSelection = state.selection.constructor;
    const spec = state.changeByRange((range) => {
      const base = Math.max(0, range.from - PAD);
      const end = Math.min(state.doc.length, range.to + PAD);
      const plan = planToggle(state.sliceDoc(base, end), range.from - base, range.to - base, kind);
      const from = base + plan.from;
      const to = base + plan.to;
      return {
        changes: plan.changes.map((c) => ({ from: base + c.from, to: base + c.to, insert: c.insert })),
        range: range.head < range.anchor ? EditorSelection.range(to, from)
                                         : EditorSelection.range(from, to),
      };
    });
    // A userEvent history won't join, so each toggle is its own undo step
    // (an unlabeled change merges into the surrounding typing).
    view.dispatch({ ...spec, scrollIntoView: true, userEvent: "input.format" });
  }

  // Keys are swallowed even while blocked, so Ctrl-B & co. never reach the browser.
  function formatCommand(kind) {
    return (view) => {
      if (!pending(view.state)) toggle(view, kind);
      return true;
    };
  }

  function linkCommand(view) {
    if (pending(view.state)) return true;
    toggle(view, "link");
    const cm = window.CMEditor;
    const main = view.state.selection.main;
    if (main.empty && main.from >= 2 && view.state.sliceDoc(main.from - 2, main.from) === "[["
        && cm.startCompletion) {
      cm.startCompletion(view);
    }
    return true;
  }

  function extension() {
    const cm = window.CMEditor;
    if (!cm || !cm.keymap || !cm.Prec) {
      console.warn("edit_keys: CodeMirror bundle lacks keymap/Prec - disabled");
      return [];
    }
    // Prec.high: ahead of defaultKeymap, which binds Mod-i to selectParentSyntax.
    return [cm.Prec.high(cm.keymap.of([
      { key: "Mod-b", run: formatCommand("bold") },
      { key: "Mod-i", run: formatCommand("italic") },
      { key: "Mod-Shift-x", run: formatCommand("strike") },
      { key: "Mod-Shift-h", run: formatCommand("highlight") },
      { key: "Mod-e", run: formatCommand("code") },
      { key: "Mod-k", run: linkCommand },
    ]))];
  }

  // toggle is exported for app/.test/test_edit_keys.js's real-bundle checks.
  window.EditKeys = { extension, toggle };
})();
