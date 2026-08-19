// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

/* AI writing-assistance overlay for the /edit/ view CodeMirror editor.

   The slash command menu is data-driven: on init it fetches
   GET /api/edit/commands and renders whatever the server says is
   available. Adding a new writing command therefore only requires
   editing app/src/edit_assist.py.

   Trigger:
     - Type "/" at start of line / after whitespace, no selection:
         cursor-trigger. The "/" enters the doc; subsequent typing
         filters the menu (Notion-style). Esc leaves the typed text;
         Enter/click removes the "/" and the filter chars and runs
         the command.
     - Type "/" while text is selected:
         selection-trigger. We undo the slash-replacement, restore
         the selection, and show the menu with selection-requiring
         commands enabled.
     - Ctrl-Shift-/ anywhere (with or without a selection):
         keyboard-trigger, for opening the menu mid-word or at the end
         of a sentence, where a bare "/" is suppressed (or would just be
         punctuation). Nothing enters the doc, so there is nothing to
         clean up on Esc.

   Filtering has two shapes, keyed on whether a literal "/" is in the
   doc (menu.slashStart): with one, typed chars land in the doc after it
   and update() reads them back; without one (selection- and keyboard-
   triggered menus), keydowns are intercepted before CodeMirror sees
   them, so the doc and any selection are left untouched.

   Streaming: fetch /api/edit/assist (SSE), append tokens into a
   StateField, render via Decoration.mark + Decoration.widget.
   Enter accepts/applies (commits a real change), Esc rejects/cancels; in a
   granular per-hunk diff Tab/Shift-Tab move between changes and Space toggles.
*/
(function () {

  // Server-discovered command list. Cached for the page lifetime.
  // Falls back to the two original commands if the fetch fails.
  let COMMANDS = [
    { id: "continue", label: "Continue Writing",  range_source: "cursor",    operation: "insert"  },
    { id: "rewrite",  label: "Rewrite Selection", range_source: "selection", operation: "replace" },
  ];

  // Scope the menu to the current file's vault: editor tools declare a `vaults:`
  // whitelist, so the server filters by the path we send. This script loads in
  // <head> (before the form's hidden path input exists), so the fetch MUST wait
  // for the DOM - otherwise getFilePath() is null, no ?path= is sent, and the
  // server can't scope the menu (it falls back to showing every tool).
  function loadCommands() {
    const p = getFilePath();
    fetch("/api/edit/commands" + (p ? "?path=" + encodeURIComponent(p) : ""))
      .then(r => r.ok ? r.json() : null)
      .then(arr => { if (Array.isArray(arr) && arr.length) COMMANDS = arr; })
      .catch(err => console.warn("EditAssist: command list fetch failed, using fallback", err));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadCommands);
  } else {
    loadCommands();
  }

  // The edit form already carries the document path in a hidden input
  // (used by the save handler and the upload code). Read it from there
  // rather than maintaining a second copy.
  function getFilePath() {
    const input = document.querySelector(
      "form[name='edit_document_form'] input[name='document_name']"
    );
    return input ? input.value : null;
  }

  // Build a viewable /wiki URL from a vault-relative page path (e.g. the op:note
  // digest at "_dada/editors/{slug}/Digest.md"). Strips .md, encodes spaces,
  // keeps slashes. wikiBaseName is its display label (the filename, no dir/.md).
  function wikiUrl(rel) {
    const p = String(rel || "").replace(/\.md$/i, "");
    return encodeURI("/wiki/" + (window.WIKI_VAULT || "main") + "/" + p);
  }
  function wikiBaseName(rel) {
    const p = String(rel || "").replace(/\.md$/i, "");
    const i = p.lastIndexOf("/");
    return i >= 0 ? p.slice(i + 1) : p;
  }

  // Minimal YAML frontmatter parser - only handles the simple `key: value`
  // lines that _voice_hint cares about (audience, voice, tone, style).
  // Nested mappings, lists, and block scalars are intentionally ignored;
  // if richer parsing is ever needed, switch to server-side parsing using
  // the path we already send.
  // Single source of truth for locating a leading YAML frontmatter block, so the
  // key parser and the body-offset helper below can never drift apart. Returns
  // { end, inner } (end = body-start offset, inner = the raw key/value region)
  // or null when there's none. This is the client-side mirror of the server's
  // WikiDoc.strip_frontmatter; kept intentionally lenient (tolerates CRLF, which
  // that LF-only helper does not) because the browser can't call the Python one.
  function matchFrontmatter(docText) {
    if (!docText) return null;
    const m = docText.match(/^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/);
    return m ? { end: m[0].length, inner: m[1] } : null;
  }

  function parseFrontmatter(docText) {
    const fm = matchFrontmatter(docText);
    if (!fm) return null;
    const out = {};
    for (const line of fm.inner.split(/\r?\n/)) {
      const kv = line.match(/^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$/);
      if (!kv) continue;
      let v = kv[2].trim();
      if ((v.startsWith('"') && v.endsWith('"')) ||
          (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      if (v) out[kv[1]] = v;
    }
    return Object.keys(out).length ? out : null;
  }

  // Body-start offset (0 if no frontmatter). Whole-document rewrites exclude the
  // frontmatter - background tasks own it, so rewriting it here just churns it.
  function frontmatterEnd(docText) {
    const fm = matchFrontmatter(docText);
    return fm ? fm.end : 0;
  }

  // The suggestion field is built per buildExtension() call, so capture the live one
  // here to back the hasPending() export. Other editor overlays (the unified diff in
  // edit.html) decorate the same lines and need to know when a proposal is up.
  let activeSuggestionField = null;

  function buildExtension() {
    const cm = window.CMEditor;
    if (!cm) {
      console.error("EditAssist: window.CMEditor not loaded");
      return [];
    }
    const {
      EditorView, StateField, StateEffect, Decoration, WidgetType,
      ViewPlugin, keymap, Prec, showTooltip, tooltips,
    } = cm;
    if (!StateField || !Decoration || !WidgetType || !ViewPlugin || !Prec
        || !showTooltip || !tooltips) {
      console.error("EditAssist: CM bundle missing required exports - rebuild needed");
      return [];
    }

    // --- Effects -----------------------------------------------------------
    const setSuggestion = StateEffect.define();   // value: full Suggestion object
    const appendToken   = StateEffect.define();   // value: token string
    const markReady     = StateEffect.define();
    const clearSuggestion = StateEffect.define();
    const setSources    = StateEffect.define();   // value: list of {path, title, header, linked}
    const setStatus     = StateEffect.define();   // value: working-indicator text (tool-calling editors)
    const resetTokens   = StateEffect.define();   // discard streamed tokens (a `retract` event)
    const markError     = StateEffect.define();   // value: user-facing message; persists until dismissed / next action
    const markNotice    = StateEffect.define();   // value: positive confirmation (op:note saved); persists until dismissed
    const toggleHunk    = StateEffect.define();   // value: hunk index; flips accept<->reject for that diff hunk
    const focusHunk     = StateEffect.define();   // value: hunk index to highlight (keyboard navigation)

    // --- StateField holding the pending suggestion (or null) -------------
    const suggestionField = StateField.define({
      create() { return null; },
      update(value, tr) {
        const hasClear = tr.effects.some(e => e.is(clearSuggestion));
        const hasSet   = tr.effects.some(e => e.is(setSuggestion));
        if (tr.docChanged && value && !hasClear && !hasSet) {
          value = null;
        }
        for (const e of tr.effects) {
          if (e.is(setSuggestion)) value = e.value;
          else if (e.is(clearSuggestion)) value = null;
          else if (e.is(appendToken) && value) {
            // A token arriving means real output has started - the working
            // indicator (statusText) gives way to the streamed text.
            value = { ...value, proposedText: value.proposedText + e.value, statusText: "" };
          } else if (e.is(markReady) && value) {
            value = { ...value, status: "ready" };
          } else if (e.is(setSources) && value) {
            value = { ...value, sources: e.value };
          } else if (e.is(setStatus) && value) {
            value = { ...value, statusText: e.value };
          } else if (e.is(resetTokens) && value) {
            value = { ...value, proposedText: "" };
          } else if (e.is(markError) && value) {
            value = { ...value, status: "error", statusText: e.value };
          } else if (e.is(markNotice) && value) {
            value = { ...value, status: "notice",
                      noticeLabel: e.value.label || "Saved",
                      noticeLinks: e.value.links || [] };
          } else if (e.is(toggleHunk) && value) {
            // Per-hunk accept/reject decision. Undefined defaults to "accept",
            // so the first toggle on a hunk flips it to "reject" (keep original).
            const decisions = { ...(value.decisions || {}) };
            const cur = decisions[e.value] || "accept";
            decisions[e.value] = cur === "accept" ? "reject" : "accept";
            value = { ...value, decisions };
          } else if (e.is(focusHunk) && value) {
            value = { ...value, focusIdx: e.value };
          }
        }
        return value;
      },
      provide: f => EditorView.decorations.from(f, value => buildDecorations(value)),
    });
    activeSuggestionField = suggestionField;

    // --- Decoration construction ------------------------------------------
    function buildDecorations(s) {
      if (!s) return Decoration.none;
      // For a large replace, once the proposal is final, render a granular
      // word-level in-place diff instead of striking the whole range and
      // dumping the whole new text below it. Falls back to the bulk view if the
      // diff can't be computed cheaply/safely (see buildDiffDecorations).
      if (s.status === "ready") {
        const model = granularModel(s);
        if (model) return buildDiffDecorations(s, model);
      }
      const decs = [];
      const streamingEmpty = s.status === "streaming" && !s.proposedText;
      // Strike ONLY for `replace`. An `insert` keeps the range and adds after
      // it, and a `note` doesn't touch the document at all - striking their
      // range made the preview promise a deletion that never happened.
      // And don't strike while merely waiting (the spinner phase) or on error -
      // only once there's proposed text to compare against.
      if (s.operation === "replace" && s.range.to > s.range.from
          && s.status !== "error" && !streamingEmpty) {
        decs.push(
          Decoration.mark({ class: "cm-ai-strike" })
            .range(s.range.from, s.range.to)
        );
      }
      // During the spinner phase, anchor the indicator at the head (where the
      // user invoked the command) so it's on-screen even for a whole-document
      // op. Once there's text, show it WHERE IT WILL LAND: the insertion anchor
      // for an additive op (so a `prepend` proposal renders above the range, not
      // below it), or the range end for a replace, after the struck-out text.
      const widgetPos = (streamingEmpty && s.anchorPos != null) ? s.anchorPos
        : (s.operation !== "replace" && s.insertAt != null) ? s.insertAt
        : s.range.to;
      decs.push(
        Decoration.widget({
          widget: new ProposalWidget(s),
          side: 1,
          block: false,
        }).range(widgetPos)
      );
      return Decoration.set(decs, true);
    }

    class ProposalWidget extends WidgetType {
      constructor(s) { super(); this.s = s; }
      eq(other) {
        return other.s.proposedText === this.s.proposedText
            && other.s.status       === this.s.status
            && other.s.operation    === this.s.operation
            && (other.s.statusText || "") === (this.s.statusText || "")
            && (other.s.sources?.length || 0) === (this.s.sources?.length || 0);
      }
      toDOM(view) {
        const wrap = document.createElement("span");
        // Style based on operation: insert (ghost-like) vs replace (highlighted addition)
        wrap.className = "cm-ai-proposal cm-ai-" + (this.s.operation || "insert");
        const text = document.createElement("span");
        text.className = "cm-ai-proposal-text";
        text.textContent = this.s.proposedText;
        wrap.appendChild(text);
        if (this.s.status === "streaming") {
          // Before any text streams, ALWAYS show the working spinner - not just
          // when a tool-calling editor sent progress text. (Built-in commands
          // send no status event, and a lone blinking cursor read as "nothing is
          // happening".) A tool editor's narration ("Searching the wiki…") fills
          // the label when present; otherwise a generic "Thinking…". Once tokens
          // arrive we switch to the streaming cursor.
          if (!this.s.proposedText) {
            const working = document.createElement("span");
            working.className = "cm-ai-working";
            const spin = document.createElement("span");
            spin.className = "cm-ai-working-spinner";
            const label = document.createElement("span");
            label.className = "cm-ai-working-label";
            label.textContent = this.s.statusText || "Thinking…";
            working.appendChild(spin);
            working.appendChild(label);
            wrap.appendChild(working);
          } else {
            const cur = document.createElement("span");
            cur.className = "cm-ai-streaming-cursor";
            cur.textContent = "▎";
            wrap.appendChild(cur);
          }
        } else if (this.s.status === "error") {
          const err = document.createElement("span");
          err.className = "cm-ai-error";
          err.textContent = this.s.statusText || "Something went wrong.";
          wrap.appendChild(err);
          const chip = document.createElement("span");
          chip.className = "cm-ai-chip";
          const dismiss = document.createElement("button");
          dismiss.type = "button";
          dismiss.className = "cm-ai-chip-reject";
          dismiss.textContent = "Dismiss (Esc)";
          dismiss.addEventListener("mousedown", e => {
            e.preventDefault();
            rejectSuggestion(view);
          });
          chip.appendChild(dismiss);
          wrap.appendChild(chip);
        } else if (this.s.status === "notice") {
          // op:note wrote an external digest page - no document change. Show a
          // positive confirmation with a Dismiss (Esc); Enter can't apply
          // anything here (acceptSuggestion gates on status === "ready").
          const ok = document.createElement("span");
          ok.className = "cm-ai-notice";
          ok.appendChild(document.createTextNode((this.s.noticeLabel || "Saved") + " "));
          // Clickable links to the external pages (digest, memory). Open in a new
          // tab so the user's edit session isn't navigated away from.
          (this.s.noticeLinks || []).forEach((lk, i) => {
            if (i) ok.appendChild(document.createTextNode(" · "));
            const a = document.createElement("a");
            a.className = "cm-ai-notice-link";
            a.href = lk.url;
            a.target = "_blank";
            a.rel = "noopener";
            a.textContent = lk.label;
            ok.appendChild(a);
          });
          wrap.appendChild(ok);
          const chip = document.createElement("span");
          chip.className = "cm-ai-chip";
          const dismiss = document.createElement("button");
          dismiss.type = "button";
          dismiss.className = "cm-ai-chip-reject";
          dismiss.textContent = "Dismiss (Esc)";
          dismiss.addEventListener("mousedown", e => {
            e.preventDefault();
            rejectSuggestion(view);
          });
          chip.appendChild(dismiss);
          wrap.appendChild(chip);
        } else {
          const chip = document.createElement("span");
          chip.className = "cm-ai-chip";
          const accept = document.createElement("button");
          accept.type = "button";
          accept.className = "cm-ai-chip-accept";
          accept.textContent = "Accept (Enter)";
          accept.addEventListener("mousedown", e => {
            e.preventDefault();
            acceptSuggestion(view);
          });
          const reject = document.createElement("button");
          reject.type = "button";
          reject.className = "cm-ai-chip-reject";
          reject.textContent = "Reject (Esc)";
          reject.addEventListener("mousedown", e => {
            e.preventDefault();
            rejectSuggestion(view);
          });
          chip.appendChild(accept);
          chip.appendChild(reject);
          wrap.appendChild(chip);
        }
        // Sources row - present in both streaming and ready states (the
        // `sources` SSE event arrives before the first token), so the
        // user can see the grounding while the continuation is forming.
        if (this.s.sources && this.s.sources.length) {
          const sources = document.createElement("span");
          sources.className = "cm-ai-sources";
          const lead = document.createElement("span");
          lead.className = "cm-ai-sources-lead";
          lead.textContent = "notes consulted: ";
          sources.appendChild(lead);
          this.s.sources.forEach((src, i) => {
            if (i > 0) {
              const sep = document.createElement("span");
              sep.className = "cm-ai-sources-sep";
              sep.textContent = " - ";
              sources.appendChild(sep);
            }
            const link = document.createElement("a");
            link.className = "cm-ai-source-link";
            if (src.linked) link.classList.add("cm-ai-source-linked");
            link.href = "/wiki/" + src.path;
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = src.title || src.path;
            if (src.header) link.title = src.header;
            // Don't let CodeMirror swallow the click as an editor event.
            link.addEventListener("mousedown", e => e.stopPropagation());
            sources.appendChild(link);
          });
          wrap.appendChild(sources);
        }
        return wrap;
      }
      ignoreEvent() { return false; }
    }

    // --- Granular word-level diff (no @codemirror/merge dependency) --------
    // For a large replace we compute a word diff between the original and the
    // proposed text and paint it in place with the decoration API: unchanged
    // text is untouched, deletions are struck where they sit, insertions appear
    // as inline widgets. This replaces the "strike the whole doc, dump the whole
    // new doc below" bulk view. If the diff can't be produced cheaply or fails
    // self-verification, buildDiffDecorations returns null and the caller falls
    // back to the bulk view - so worst case equals the old behavior.
    const DIFF_MIN_CHARS = 120;    // below this, the bulk inline view is fine
    const DIFF_MAX_CHARS = 200000; // |A|+|B| char guard before diffing at all
    // Per-hunk review is only useful when changes are SPARSE. A dense rewrite
    // (or prose->Mermaid/table) interleaves struck-original and inserted words so
    // tightly that neither version is readable - route those to the single bulk
    // accept/reject instead. Two gates: too many hunks, or too little of the
    // original preserved. The hunk cap is generous (Tab/Shift-Tab navigate), so
    // the preserved-fraction is the real discriminator.
    const DIFF_MAX_HUNKS = 200;
    // Fraction of original chars left unchanged. A whole-document spell-check
    // keeps ~0.9 (sparse, readable inline); a grammar rewrite that touches most
    // words keeps ~0.4-0.5 and interleaves into an unreadable mess. 0.6 keeps
    // spell-check granular while sending dense rewrites to bulk.
    const DIFF_MIN_PRESERVED = 0.6;

    // Verify a run list reconstructs both inputs; on any mismatch the caller
    // discards it (falls back). This guards every diff engine we plug in.
    function verifyRuns(runs, original, proposed) {
      let ro = "", rp = "";
      for (const r of runs) {
        if (r.type === "equal") { ro += r.text; rp += r.text; }
        else if (r.type === "delete") ro += r.text;
        else rp += r.text;
      }
      return ro === original && rp === proposed;
    }

    // Diff engine: @codemirror/merge's diff, exposed on the bundle as
    // CMEditor.presentableDiff / CMEditor.diff. Myers-based but memory-bounded
    // (no full O(D*len) trace) and scan/timeout-limited, so it stays cheap on
    // whole documents. Returns char-level Change ranges ({fromA,toA,fromB,toB});
    // we fold them into equal/delete/insert runs. Any shape surprise fails
    // verifyRuns -> null -> the bulk single accept/reject view.
    const DIFF_WORD_CHAR = /[\p{L}\p{N}_]/u;

    function diffRunsCM(original, proposed) {
      const cm = window.CMEditor;
      const fn = cm && (cm.presentableDiff || cm.diff);
      if (typeof fn !== "function") return null;
      let changes;
      try { changes = fn(original, proposed); } catch (e) { return null; }
      if (!changes || typeof changes.length !== "number") return null;

      // presentableDiff is character-level ("colour"->"color" = delete "u").
      // Grow each change out to whole-word boundaries so hunks read as word
      // replacements, not mid-word character edits. Only pull chars that are
      // word-chars AND identical on both sides (i.e. truly equal context).
      const grown = [];
      for (const ch of changes) {
        let { fromA, toA, fromB, toB } = ch;
        if (![fromA, toA, fromB, toB].every(Number.isInteger)) return null;
        if (toA < fromA || toB < fromB) return null;
        while (fromA > 0 && fromB > 0 && original[fromA - 1] === proposed[fromB - 1]
               && DIFF_WORD_CHAR.test(original[fromA - 1])) { fromA--; fromB--; }
        while (toA < original.length && toB < proposed.length
               && original[toA] === proposed[toB] && DIFF_WORD_CHAR.test(original[toA])) {
          toA++; toB++;
        }
        // Merge with the previous grown change if expansion made them touch/overlap.
        const last = grown[grown.length - 1];
        if (last && fromA <= last.toA) {
          last.toA = Math.max(last.toA, toA);
          last.toB = Math.max(last.toB, toB);
        } else {
          grown.push({ fromA, toA, fromB, toB });
        }
      }

      const runs = [];
      let a = 0;
      for (const g of grown) {
        if (g.fromA < a) return null;                 // not ordered -> bail (bulk)
        if (g.fromA > a) runs.push({ type: "equal", text: original.slice(a, g.fromA) });
        const del = original.slice(g.fromA, g.toA);
        const ins = proposed.slice(g.fromB, g.toB);
        if (del) runs.push({ type: "delete", text: del });
        if (ins) runs.push({ type: "insert", text: ins });
        a = g.toA;
      }
      if (a < original.length) runs.push({ type: "equal", text: original.slice(a) });
      return verifyRuns(runs, original, proposed) ? runs : null;
    }

    // Contiguous equal/delete/insert runs for original -> proposed, or null
    // (null -> the caller uses the bulk single accept/reject). The diff comes
    // from CMEditor's bounded engine; if it's unavailable or fails verification
    // we simply fall back to bulk - no second diff implementation to maintain.
    function computeDiffRuns(original, proposed) {
      if (original.length + proposed.length > DIFF_MAX_CHARS) return null;
      return diffRunsCM(original, proposed);
    }

    // Group the flat diff runs into an ordered list of elements: passthrough
    // "equal" text and "hunk" changes (each a delete-and/or-insert region
    // between equal runs). Hunk indices are stable across renders (the diff is
    // deterministic), so per-hunk accept/reject decisions can be keyed by index.
    function computeDiffModel(s) {
      const runs = computeDiffRuns(s.originalText || "", s.proposedText);
      if (!runs) return null;
      const elements = [];
      let hunkIndex = 0;
      let i = 0;
      while (i < runs.length) {
        if (runs[i].type === "equal") {
          elements.push({ kind: "equal", text: runs[i].text });
          i++;
        } else {
          let delText = "", insText = "";
          while (i < runs.length && runs[i].type !== "equal") {
            if (runs[i].type === "delete") delText += runs[i].text;
            else insText += runs[i].text;
            i++;
          }
          elements.push({ kind: "hunk", index: hunkIndex++, delText, insText });
        }
      }
      return { elements };
    }

    // Resolve the final text from a diff model + per-hunk decisions: accepted
    // hunks contribute the insertion, rejected (kept) hunks the original.
    function resolveDiff(model, decisions) {
      decisions = decisions || {};
      let out = "";
      for (const el of model.elements) {
        if (el.kind === "equal") { out += el.text; continue; }
        const accepted = (decisions[el.index] || "accept") === "accept";
        out += accepted ? el.insText : el.delText;
      }
      return out;
    }

    // Returns the diff model IF a granular per-hunk review should be shown, else
    // null (caller falls back to the bulk single accept/reject). Per-hunk review
    // only helps for LOCALIZED edits; a wholesale transform (prose->Mermaid/table,
    // heavy rewrite) shares little with the source so the word diff fragments into
    // noise. Gate on hunk count and how much of the original survives unchanged.
    function granularModel(s) {
      if (!s || s.status !== "ready" || s.operation !== "replace") return null;
      if ((s.range.to - s.range.from) < DIFF_MIN_CHARS || !s.proposedText) return null;
      const model = computeDiffModel(s);
      if (!model) return null;
      let hunkCount = 0, preserved = 0;
      for (const el of model.elements) {
        if (el.kind === "equal") preserved += el.text.length;
        else hunkCount++;
      }
      const origLen = (s.originalText || "").length || 1;
      if (hunkCount === 0 || hunkCount > DIFF_MAX_HUNKS
          || preserved / origLen < DIFF_MIN_PRESERVED) return null;
      return model;
    }

    function hunkCountOf(model) {
      let n = 0;
      for (const el of model.elements) if (el.kind === "hunk") n++;
      return n;
    }

    // Document position where hunk `idx` begins (for focus + scroll-into-view).
    function hunkPos(s, model, idx) {
      let pos = s.range.from, count = 0;
      for (const el of model.elements) {
        if (el.kind === "equal") { pos += el.text.length; continue; }
        if (count === idx) return pos;
        count++;
        pos += el.delText.length;
      }
      return pos;
    }

    // Move the keyboard focus to the next/prev change (wrapping) and scroll it
    // into view.
    function moveFocus(view, s, model, dir) {
      const n = hunkCountOf(model);
      if (!n) return;
      const cur = Math.min(Math.max(s.focusIdx || 0, 0), n - 1);
      const next = (cur + dir + n) % n;
      view.dispatch({ effects: [
        focusHunk.of(next),
        EditorView.scrollIntoView(hunkPos(s, model, next), { y: "center" }),
      ] });
    }

    function buildDiffDecorations(s, model) {
      const decisions = s.decisions || {};
      const nHunks = hunkCountOf(model);
      const focusIdx = Math.min(Math.max(s.focusIdx || 0, 0), Math.max(0, nHunks - 1));
      const decs = [];
      let pos = s.range.from;
      for (const el of model.elements) {
        if (el.kind === "equal") { pos += el.text.length; continue; }
        const accepted = (decisions[el.index] || "accept") === "accept";
        const focused = el.index === focusIdx;
        const delLen = el.delText.length;
        if (delLen > 0) {
          // Accepted: strike the original (it will be replaced). Rejected: mark
          // it subtly as a kept-original. The focused hunk is outlined.
          decs.push(Decoration.mark({
            class: (accepted ? "cm-ai-diff-del" : "cm-ai-diff-kept")
                   + (focused ? " cm-ai-diff-focus" : ""),
          }).range(pos, pos + delLen));
        }
        decs.push(Decoration.widget({
          widget: new DiffHunkWidget(el.index, el.insText, accepted, focused),
          side: 1,
        }).range(pos + delLen));
        pos += delLen;
      }
      decs.push(Decoration.widget({ widget: new DiffApplyWidget(s), side: 1 }).range(s.range.to));
      return Decoration.set(decs, true);
    }

    // One changed region: the proposed insertion (green when accepted, struck
    // when kept) plus a small ✓/✗ toggle. Buttons are tabIndex=-1 so browser Tab
    // never lands on them - Tab is our own next-change navigation.
    class DiffHunkWidget extends WidgetType {
      constructor(index, insText, accepted, focused) {
        super();
        this.index = index; this.insText = insText;
        this.accepted = accepted; this.focused = focused;
      }
      eq(o) {
        return o.index === this.index && o.insText === this.insText
            && o.accepted === this.accepted && o.focused === this.focused;
      }
      toDOM(view) {
        const wrap = document.createElement("span");
        wrap.className = "cm-ai-hunk" + (this.focused ? " focused" : "");
        if (this.insText) {
          const ins = document.createElement("span");
          ins.className = this.accepted ? "cm-ai-diff-ins" : "cm-ai-diff-ins-rejected";
          ins.textContent = this.insText;
          wrap.appendChild(ins);
        }
        // A span, NOT a <button>: the page's `#document button` rule (an ID
        // selector) would otherwise force the full primary-button box onto it.
        const btn = document.createElement("span");
        btn.setAttribute("role", "button");
        btn.className = "cm-ai-hunk-toggle " + (this.accepted ? "accepted" : "rejected");
        btn.textContent = this.accepted ? "✓" : "✗";
        btn.title = this.accepted
          ? "Applying - click (or Space) to keep the original"
          : "Keeping original - click (or Space) to apply";
        btn.addEventListener("mousedown", e => {
          e.preventDefault();
          view.dispatch({ effects: [toggleHunk.of(this.index), focusHunk.of(this.index)] });
        });
        wrap.appendChild(btn);
        return wrap;
      }
      ignoreEvent() { return false; }
    }

    // Trailing chip: Apply commits the accepted hunks; Cancel discards. A hint
    // line spells out the keyboard review controls.
    class DiffApplyWidget extends WidgetType {
      constructor(s) { super(); this.s = s; }
      eq(o) { return o.s.proposedText === this.s.proposedText && o.s.status === this.s.status; }
      toDOM(view) {
        const wrap = document.createElement("span");
        wrap.className = "cm-ai-proposal cm-ai-diff-chip";
        const chip = document.createElement("span");
        chip.className = "cm-ai-chip";
        const apply = document.createElement("button");
        apply.type = "button";
        apply.tabIndex = -1;
        apply.className = "cm-ai-chip-accept";
        apply.textContent = "Apply (Enter)";
        apply.addEventListener("mousedown", e => { e.preventDefault(); acceptSuggestion(view); });
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.tabIndex = -1;
        cancel.className = "cm-ai-chip-reject";
        cancel.textContent = "Cancel (Esc)";
        cancel.addEventListener("mousedown", e => { e.preventDefault(); rejectSuggestion(view); });
        chip.appendChild(apply);
        chip.appendChild(cancel);
        const hint = document.createElement("span");
        hint.className = "cm-ai-diff-hint";
        hint.textContent = "Tab / Shift-Tab: next / prev · Space: keep or apply · Enter: apply all · Esc: cancel";
        wrap.appendChild(chip);
        wrap.appendChild(hint);
        return wrap;
      }
      ignoreEvent() { return false; }
    }

    function acceptSuggestion(view) {
      const s = view.state.field(suggestionField, false);
      if (!s || s.status !== "ready") return;
      // For a granular diff, resolve the final text from the per-hunk decisions
      // (accepted hunks -> insertion, kept hunks -> original). Otherwise the
      // whole proposed text is the insertion, as before. The model is recomputed
      // (pure fn of original/proposed) and matches what buildDiffDecorations drew.
      let insert = s.proposedText;
      const model = granularModel(s);
      if (model) insert = resolveDiff(model, s.decisions);
      // `replace` swaps the range out; every other operation adds at the anchor
      // resolved when the command started (see `insertAt` in startAssist).
      // Fall back to the range end for a suggestion created before insertAt
      // existed, which is what `append` - the old `insert` - always meant.
      const at = s.insertAt != null ? s.insertAt : s.range.to;
      const changes = s.operation === "replace"
        ? { from: s.range.from, to: s.range.to, insert }
        : { from: at, insert };
      // The caret follows the inserted text, so it is measured from where the
      // change STARTS: `replace` deletes the range first, so its text lands at
      // range.from, not at the (now-gone) range.to that `insertAt` carries for
      // the additive ops.
      const caretBase = s.operation === "replace" ? s.range.from : at;
      view.dispatch({
        changes,
        effects: clearSuggestion.of(null),
        selection: { anchor: caretBase + insert.length },
      });
      view.focus();
    }

    function rejectSuggestion(view) {
      view.dispatch({ effects: clearSuggestion.of(null) });
      view.focus();
    }

    // Keymap (Prec.highest so these beat indentWithTab / newline when a
    // suggestion is active). CONFIRM IS ALWAYS ENTER, CANCEL IS ALWAYS ESC -
    // consistent with the slash menu, the custom-prompt input, and every other
    // modal confirm in the editor. Tab is reserved for NAVIGATION:
    //   granular diff: Enter applies all, Esc cancels, Tab/Shift-Tab move between
    //                  changes, Space toggles the focused change (keep<->apply).
    //   single/bulk/insert: Enter accepts, Esc rejects; Tab is swallowed (there's
    //                  nothing to navigate) so it can't accept OR indent the doc
    //                  under a pending proposal.
    // Handlers return false when no suggestion is pending, so normal typing
    // (space, newline, tab-indent) is completely untouched otherwise.
    const aiKeymap = keymap.of([
      {
        key: "Enter",
        run(view) {
          const s = view.state.field(suggestionField, false);
          if (!s) return false;                     // no suggestion: normal newline
          if (s.status === "streaming") return true; // swallow mid-stream
          if (s.status !== "ready") return false;
          acceptSuggestion(view);                   // granular -> apply all; else accept
          return true;
        },
      },
      {
        key: "Tab",
        run(view) {
          const s = view.state.field(suggestionField, false);
          if (!s) return false;                     // no suggestion: normal indent
          if (s.status === "streaming") return true;
          const model = granularModel(s);
          if (model) { moveFocus(view, s, model, +1); return true; }
          return true;                              // single proposal: swallow (Enter confirms)
        },
      },
      {
        key: "Shift-Tab",
        run(view) {
          const s = view.state.field(suggestionField, false);
          if (!s) return false;
          const model = granularModel(s);
          if (!model) return false;
          moveFocus(view, s, model, -1);
          return true;
        },
      },
      {
        key: " ",
        run(view) {
          const s = view.state.field(suggestionField, false);
          if (!s) return false;
          const model = granularModel(s);
          if (!model) return false;   // let space type normally
          const n = hunkCountOf(model);
          const idx = Math.min(Math.max(s.focusIdx || 0, 0), Math.max(0, n - 1));
          view.dispatch({ effects: toggleHunk.of(idx) });
          return true;
        },
      },
      {
        key: "Escape",
        run(view) {
          const s = view.state.field(suggestionField, false);
          if (!s) return false;
          rejectSuggestion(view);
          return true;
        },
      },
    ]);

    // Native CodeMirror tooltip plumbing shared by the slash menu and the
    // autolink/cite pickers. A "slot" is a StateField that surfaces at most one
    // tooltip through CM's built-in tooltip manager, which owns everything the
    // old hand-rolled code did by hand: placement, flip-above-when-it-would-
    // clip, horizontal clamp, reposition-on-scroll, and mount/unmount. We drive
    // a slot by dispatching its effect with a Tooltip (show) or null (hide);
    // the DOM we hand it is the same `.slash-menu` element the render code
    // populates. Three independent slots because the three overlays are
    // mutually exclusive in practice but managed by separate code paths.
    function makeTooltipSlot() {
      const setEffect = StateEffect.define();
      const field = StateField.define({
        create() { return null; },
        update(value, tr) {
          for (const e of tr.effects) if (e.is(setEffect)) value = e.value;
          return value;
        },
        provide: (f) => showTooltip.from(f),
      });
      return { field, setEffect };
    }

    // Wrap a pre-built element as a Tooltip anchored at `pos`. `above:false`
    // prefers rendering below; `strictSide:false` lets the manager flip up when
    // there isn't room. The manager measures `el` (so it must already be
    // populated) and sets its position/top/left inline.
    function elementTooltip(pos, el) {
      return {
        pos,
        above: false,
        strictSide: false,
        arrow: false,
        create() { return { dom: el }; },
      };
    }

    const slashSlot = makeTooltipSlot();
    const autolinkSlot = makeTooltipSlot();
    const citeSlot = makeTooltipSlot();

    // Run `open()` once `pos` is scrolled into view and actually has layout
    // coords. CodeMirror only lays out the visible viewport, so a floating UI
    // anchored to an off-screen position (e.g. a bottom-to-top selection, then
    // "/") otherwise gets null coords and silently never appears. Anchor these
    // UIs at the selection HEAD (where the caret is) and route them through here.
    function whenAnchorVisible(view, pos, open) {
      if (view.coordsAtPos(pos)) { open(); return; }
      view.dispatch({ effects: EditorView.scrollIntoView(pos, { y: "center" }) });
      view.requestMeasure({
        read: () => view.coordsAtPos(pos),
        write: (coords) => { if (coords) open(); },
      });
    }

    // Scroll `container` the minimum amount needed to bring `row` fully into
    // view. The menu is capped at max-height (see .slash-menu in tzara.css) and
    // scrolls internally, so arrow-key navigation past the fold would otherwise
    // move an invisible highlight and force the user onto the mouse. Done by
    // hand rather than row.scrollIntoView({ block: "nearest" }): the menu is a
    // CM tooltip mounted inside the editor's scroller, and scrollIntoView walks
    // every scrollable ancestor, which jogs the document under the caret.
    function keepRowVisible(container, row) {
      // Zero height means the menu hasn't been mounted by the tooltip manager
      // yet (first render happens before the dispatch); nothing can overflow.
      if (!container.clientHeight) return;
      const top = row.offsetTop;
      const bottom = top + row.offsetHeight;
      if (top < container.scrollTop) {
        container.scrollTop = top;
      } else if (bottom > container.scrollTop + container.clientHeight) {
        container.scrollTop = bottom - container.clientHeight;
      }
    }

    // --- Slash menu plugin ------------------------------------------------
    const slashTrigger = ViewPlugin.fromClass(class {
      constructor(view) {
        this.view = view;
        // { el, mode: "cursor"|"selection", slashStart, items, idx, filter, ... }
        // slashStart is the doc offset of a literal typed "/", or null when the
        // menu was opened by keyboard (nothing to strip, filter via keydowns).
        this.menu = null;
      }

      isEnabled(cmd) {
        if (cmd.range_source === "cursor")    return true;
        if (cmd.range_source === "document")  return true;  // operates on the whole buffer
        if (cmd.range_source === "selection") return !this.view.state.selection.main.empty;
        return false;
      }

      // Whether a command should APPEAR in the menu at all (distinct from
      // isEnabled, which greys but still shows). Used to give the custom-prompt
      // trio the scope-appropriate face: a single "Prompt" when text is
      // selected, or "Prompt (replace)" + "Prompt (insert)" when nothing is.
      isVisible(cmd) {
        const hasSel = !this.view.state.selection.main.empty;
        if (cmd.id === "custom") return hasSel;
        if (cmd.id === "custom_replace" || cmd.id === "custom_insert") return !hasSel;
        return true;
      }

      update(update) {
        if (!update.docChanged) return;
        // Skip our own programmatic dispatches (they always carry effects)
        const tr = update.transactions[update.transactions.length - 1];
        if (!tr) return;
        if (tr.effects && tr.effects.length) return;

        // A menu with no "/" in the doc (selection- or keyboard-triggered)
        // filters via intercepted keydowns, so any doc change reaching us moved
        // the ground under its anchor - close rather than misread it as filter.
        if (this.menu && this.menu.slashStart == null) {
          this.closeMenuDeferred();
          return;
        }

        // While a menu is open over a typed "/", doc changes update the filter.
        if (this.menu && this.menu.mode === "cursor") {
          const head = update.state.selection.main.head;
          const slashStart = this.menu.slashStart;
          if (head <= slashStart || head > slashStart + 64) {
            // User backspaced past the slash, or typed too far - close.
            this.closeMenuDeferred();
            return;
          }
          const text = update.state.doc.sliceString(slashStart, head);
          if (!text.startsWith("/")) {
            this.closeMenuDeferred();
            return;
          }
          if (text.includes("\n")) {
            this.closeMenuDeferred();
            return;
          }
          this.menu.filter = text.slice(1).toLowerCase();
          this.renderMenu();
          return;
        }

        // No menu open - look for a slash trigger in this transaction.
        let trigger = null;
        const startDoc = update.startState.doc;
        const startSel = update.startState.selection.main;
        tr.changes.iterChanges((fromA, toA, fromB, toB, inserted) => {
          if (trigger) return;
          const txt = inserted.toString();
          if (txt !== "/") return;
          if (toA - fromA > 0) {
            trigger = {
              kind: "selection",
              origRange: { from: fromA, to: toA },
              origText: startDoc.sliceString(fromA, toA),
              slashPos: toB,
              // Preserve which end the caret was on so we can restore the
              // selection's direction and anchor the menu at the HEAD (visible).
              origHead: startSel.head,
              origAnchor: startSel.anchor,
            };
          } else {
            const eligible = fromA === 0
              || /\s/.test(startDoc.sliceString(fromA - 1, fromA));
            if (eligible) {
              trigger = { kind: "cursor", slashPos: toB };
            }
          }
        });

        if (!trigger) return;
        const view = this.view;
        const t = trigger;
        // Defer; don't dispatch synchronously from inside update().
        setTimeout(() => {
          if (t.kind === "selection") {
            view.dispatch({
              changes: {
                from: t.slashPos - 1,
                to: t.slashPos,
                insert: t.origText,
              },
              // Restore the selection preserving its direction, and keep the head
              // in view - the menu anchors at the head (the end the caret was on),
              // which is on-screen even for a long bottom-to-top selection.
              selection: { anchor: t.origAnchor, head: t.origHead },
              effects: EditorView.scrollIntoView(t.origHead, { y: "nearest" }),
            });
            this.openMenu({ mode: "selection", anchorPos: t.origHead });
          } else {
            this.openMenu({
              mode: "cursor",
              anchorPos: t.slashPos,
              slashStart: t.slashPos - 1,
            });
          }
        }, 0);
      }

      // Ctrl-Shift-/ entry point: open the same menu without a "/" in the doc,
      // for the positions where the typed trigger is deliberately suppressed
      // (mid-word, end of a sentence). Press again to close. With a selection
      // this is indistinguishable from the typed selection-trigger; without
      // one it behaves like cursor mode minus the slash to strip.
      openFromKeyboard() {
        if (this.menu) { this.closeMenu(); return; }
        const sel = this.view.state.selection.main;
        this.openMenu({
          mode: sel.empty ? "cursor" : "selection",
          anchorPos: sel.head,   // head is where the caret is, so it's on-screen
          slashStart: null,
        });
      }

      openMenu({ mode, anchorPos, slashStart }) {
        this.teardownLocal();
        const view = this.view;
        // Scroll the anchor into view first so the menu can't silently fail to
        // appear when it's off-screen (deferred; see whenAnchorVisible).
        whenAnchorVisible(view, anchorPos, () =>
          this._buildMenu({ mode, anchorPos, slashStart }));
      }

      _buildMenu({ mode, anchorPos, slashStart }) {
        const view = this.view;
        const el = document.createElement("div");
        el.className = "slash-menu";

        const onKey = (e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            this.closeMenu();
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            this.advanceIdx(+1);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            this.advanceIdx(-1);
          } else if (e.key === "Enter") {
            e.preventDefault();
            const filtered = this.filteredItems();
            const item = filtered[this.menu.idx];
            if (item && this.isEnabled(item)) this.choose(item);
          } else if (this.menu && this.menu.slashStart == null) {
            // No "/" in the doc: intercept filter keys before CodeMirror sees
            // them, so the user's selection (selection-triggered) or the word
            // under the caret (keyboard-triggered) stays intact while they
            // narrow the menu, and Esc leaves nothing behind. A menu opened
            // over a typed "/" filters via doc-anchored chars in update().
            if (e.key === "Backspace") {
              e.preventDefault();
              e.stopPropagation();
              if (this.menu.filter.length > 0) {
                this.menu.filter = this.menu.filter.slice(0, -1);
                this.renderMenu();
              } else {
                this.closeMenu();
              }
            } else if (
              e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey
            ) {
              e.preventDefault();
              e.stopPropagation();
              this.menu.filter += e.key.toLowerCase();
              this.menu.idx = 0;
              this.renderMenu();
            }
          }
        };
        const onMouse = (e) => {
          if (!el.contains(e.target)) this.closeMenu();
        };

        document.addEventListener("keydown", onKey, true);
        document.addEventListener("mousedown", onMouse, true);

        this.menu = {
          el, mode, anchorPos, slashStart,
          filter: "",
          idx: 0,
          onKey, onMouse,
        };
        // Populate before handing to the tooltip manager so it measures the
        // real height when placing/flipping. Cursor-mode filter keystrokes are
        // doc changes, so the manager repositions automatically; selection mode
        // only ever shrinks the list, so the initial placement stays valid.
        this.renderMenu();
        view.dispatch({ effects: slashSlot.setEffect.of(elementTooltip(anchorPos, el)) });
      }

      filteredItems() {
        const m = this.menu;
        if (!m) return [];
        const visible = COMMANDS.filter(c => this.isVisible(c));
        if (!m.filter) return visible;
        return visible.filter(c => c.label.toLowerCase().includes(m.filter));
      }

      advanceIdx(direction) {
        const items = this.filteredItems();
        if (!items.length) return;
        let i = this.menu.idx + direction;
        for (let tries = 0; tries < items.length; tries++) {
          if (i < 0) i = items.length - 1;
          if (i >= items.length) i = 0;
          if (this.isEnabled(items[i])) { this.menu.idx = i; this.renderMenu(); return; }
          i += direction;
        }
      }

      renderMenu() {
        const m = this.menu;
        if (!m) return;
        const items = this.filteredItems();
        // Clamp idx to a valid enabled item if possible
        if (items.length && !this.isEnabled(items[m.idx] || {})) {
          const firstEnabled = items.findIndex(c => this.isEnabled(c));
          m.idx = firstEnabled >= 0 ? firstEnabled : 0;
        }
        m.el.innerHTML = "";

        if (m.filter) {
          const hint = document.createElement("div");
          hint.className = "slash-menu-filter";
          hint.textContent = "/" + m.filter;
          m.el.appendChild(hint);
        }

        if (!items.length) {
          const empty = document.createElement("div");
          empty.className = "slash-menu-empty";
          empty.textContent = "(no matches)";
          m.el.appendChild(empty);
          return;
        }
        let activeRow = null;
        items.forEach((item, i) => {
          const enabled = this.isEnabled(item);
          const row = document.createElement("div");
          const active = i === m.idx && enabled;
          row.className = "slash-menu-item"
            + (active ? " active" : "")
            + (enabled ? "" : " disabled");
          row.textContent = item.label;
          if (item.description) row.title = item.description;
          row.addEventListener("mousedown", e => {
            e.preventDefault();
            if (enabled) this.choose(item);
          });
          m.el.appendChild(row);
          if (active) activeRow = row;
        });
        if (activeRow) keepRowVisible(m.el, activeRow);
      }

      // Drop listeners + local state without touching the editor. Safe to call
      // from anywhere, including inside update() and destroy(). The tooltip
      // element itself is unmounted by the manager when the field clears.
      teardownLocal() {
        const m = this.menu;
        if (!m) return;
        document.removeEventListener("keydown", m.onKey, true);
        document.removeEventListener("mousedown", m.onMouse, true);
        this.menu = null;
      }

      // Close now: for event-handler contexts (keydown/mousedown/choose) where
      // dispatching synchronously is allowed.
      closeMenu() {
        if (!this.menu) return;
        this.teardownLocal();
        this.view.dispatch({ effects: slashSlot.setEffect.of(null) });
      }

      // Close from inside update(), where dispatch is illegal: tear down local
      // state now and clear the tooltip on the next tick.
      closeMenuDeferred() {
        if (!this.menu) return;
        this.teardownLocal();
        const view = this.view;
        setTimeout(() => view.dispatch({ effects: slashSlot.setEffect.of(null) }), 0);
      }

      choose(cmd) {
        const view = this.view;
        const m = this.menu;
        // Anchor for a possible follow-up prompt input, captured before teardown:
        // the typed slash (cursor/document mode) or the caret / selection HEAD -
        // the head is on-screen even for a long selection.
        const anchorPos = (m.mode === "cursor" && m.slashStart != null)
          ? m.slashStart
          : view.state.selection.main.head;
        if (m.mode === "cursor" && m.slashStart != null) {
          // Remove the slash + any filter chars in one transaction.
          const head = view.state.selection.main.head;
          view.dispatch({
            changes: { from: m.slashStart, to: head },
            selection: { anchor: m.slashStart },
          });
        }
        // (selection mode, and any keyboard-triggered menu: nothing was typed
        // into the doc and the selection is already intact - nothing to remove.)
        this.closeMenu();
        // Custom prompt: capture a one-off instruction first; everything else
        // runs immediately.
        if (cmd.kind === "custom") {
          // Cursor mode removed the slash above, which makes the plugin's
          // update() schedule a deferred slashSlot clear (closeMenuDeferred) on
          // the next tick. Open the prompt input AFTER that clear so it lands in
          // the slot last instead of being wiped. (Selection mode has no slash to
          // remove and no deferred clear, so deferring is harmless there too.)
          setTimeout(() => openPromptInput(view, cmd, anchorPos), 0);
        } else {
          startAssist(view, cmd);
        }
      }

      destroy() { this.teardownLocal(); }
    });

    // Keyboard trigger for the slash menu. Two bindings for one chord: CM
    // resolves a shifted character key by its printed name first ("?" on a US
    // layout, so "Ctrl-?"), then retries with the unshifted base name from the
    // keycode ("Shift-Ctrl-/"). Layouts differ on which of the two arrives, so
    // bind both; only one can match a given keystroke, and neither collides
    // with a browser or CodeMirror default.
    const slashMenuKeymap = keymap.of(["Ctrl-Shift-/", "Ctrl-?"].map(key => ({
      key,
      run(view) {
        const plugin = view.plugin(slashTrigger);
        if (!plugin) return false;
        plugin.openFromKeyboard();
        return true;
      },
    })));

    // --- Custom prompt: capture a one-off instruction, then run it --------
    // Chosen from the "/" menu (kind="custom"). Renders a small input in the
    // slash tooltip slot; on submit we run startAssist with the typed text as
    // the instruction. Selection present -> transform the selection; otherwise
    // "Prompt (replace)" transforms the whole buffer and "Prompt (insert)"
    // generates text at the caret.
    function openPromptInput(view, cmd, anchorPos) {
      const el = document.createElement("div");
      el.className = "slash-prompt";

      const hint = document.createElement("div");
      hint.className = "slash-prompt-hint";
      hint.textContent = cmd.label + " - describe the change, then Enter";
      el.appendChild(hint);

      const input = document.createElement("textarea");
      input.className = "slash-prompt-input";
      input.rows = 2;
      input.placeholder = "e.g. Convert American spelling to British";
      el.appendChild(input);

      let closed = false;
      const close = () => {
        if (closed) return;
        closed = true;
        document.removeEventListener("mousedown", onOutside, true);
        view.dispatch({ effects: slashSlot.setEffect.of(null) });
      };
      const submit = () => {
        const text = input.value.trim();
        close();
        view.focus();
        if (text) startAssist(view, cmd, text);
      };
      const onOutside = (e) => { if (!el.contains(e.target)) close(); };

      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          submit();
        } else if (e.key === "Escape") {
          e.preventDefault();
          close();
          view.focus();
        }
        // Keep the keystroke away from CodeMirror's keymaps while typing.
        e.stopPropagation();
      });
      el.addEventListener("mousedown", (e) => e.stopPropagation());

      whenAnchorVisible(view, anchorPos, () => {
        view.dispatch({ effects: slashSlot.setEffect.of(elementTooltip(anchorPos, el)) });
        document.addEventListener("mousedown", onOutside, true);
        setTimeout(() => input.focus(), 0);
      });
    }

    // --- Streaming a command ----------------------------------------------
    async function startAssist(view, cmd, instruction = "") {
      // Non-LLM kinds bypass the ghost-text streaming pipeline entirely.
      // They have their own UI flow (e.g. autolink renders a picker).
      if (cmd.kind === "autolink") {
        return runAutolink(view, cmd);
      }
      if (cmd.kind === "cite") {
        return runCite(view, cmd);
      }

      const state = view.state;
      let from, to, selection = "", before = "", after = "";
      let content = null, cursorOffset = null;
      const head = state.selection.main.head;

      // selStart/selEnd are the working range in `content` COORDINATES, not
      // document coordinates - document-scope trims the frontmatter off
      // `content`, so its offsets are rebased. The server relies on
      // content.slice(selStart, selEnd) === selection, and derives the text
      // before/after the range from them (a caret is a zero-width selection,
      // so selStart === selEnd there and the same slicing yields
      // before/after-the-cursor).
      let selStart = 0, selEnd = 0;

      if (cmd.range_source === "cursor") {
        // Cursor-mode: the server slices its own windows from content.
        content = state.doc.toString();
        cursorOffset = head;
        if (cmd.operation === "replace" || cmd.operation === "prepend"
            || cmd.operation === "append") {
          // Nothing is selected, so the RANGE is the BLOCK the caret sits in
          // (the paragraph, or the whole fence): replace swaps that block,
          // prepend/append go before/after it. `insert` deliberately skips this
          // and stays at the caret, which is what lets it land mid-sentence.
          // With no block under the caret - a blank line, an empty buffer - the
          // range collapses to the caret and all three behave like insert.
          // Frontmatter is off-limits here for the same reason it is for
          // document scope: background tasks own it.
          const blk = blockAt(content, head);
          const bodyStart = frontmatterEnd(content);
          from = to = head;
          if (blk && blk.from >= bodyStart) { from = blk.from; to = blk.to; }
        } else {
          from = head; to = head;
        }
        selection = state.doc.sliceString(from, to);
        selStart = from; selEnd = to;
      } else if (cmd.range_source === "document") {
        // Whole-buffer transform ("Prompt (replace)" / a document-scope editor
        // tool), EXCLUDING the frontmatter: it's owned/edited by background tasks
        // so rewriting it here just churns it. Operate on the body only; the
        // frontmatter block stays byte-for-byte.
        const whole = state.doc.toString();
        const bodyStart = frontmatterEnd(whole);
        content = whole.slice(bodyStart);
        selection = content;
        // Rebase onto `content`: `head` indexes the WHOLE buffer, but content
        // begins after the frontmatter, so an un-rebased caret lands too far
        // along by exactly the length of the frontmatter block.
        cursorOffset = Math.max(0, head - bodyStart);
        selStart = 0; selEnd = content.length;
        // The range is the body for BOTH operations - the insert rule in
        // acceptSuggestion puts an insert at range.to, i.e. the end of the body.
        from = bodyStart; to = state.doc.length;
      } else {
        const sel = state.selection.main;
        if (sel.from === sel.to) return;
        from = sel.from; to = sel.to;
        selection = state.doc.sliceString(from, to);
        before = state.doc.sliceString(Math.max(0, from - 1000), from);
        after  = state.doc.sliceString(to, Math.min(state.doc.length, to + 500));
        // Send the buffer as well: an editor tool's custom Python receives
        // `editor.document` plus these offsets, and can reconstruct neither
        // the document nor the caret from the truncated windows above.
        content = state.doc.toString();
        cursorOffset = head;
        selStart = from; selEnd = to;
      }

      // Where an ADDITIVE operation's result will land, in document coordinates.
      // Three of the four positions come from the range; `insert` alone is
      // caret-absolute, which is what lets it land mid-sentence. The server
      // resolves the same three-way choice in `content` coordinates for its seam
      // padding (_SEAM_ANCHOR in edit_assist.py) - keep the two in step.
      const insertAt = cmd.operation === "prepend" ? from
                     : cmd.operation === "insert" ? head
                     : to;   // append (and unused for replace / note)

      view.dispatch({
        effects: [
          setSuggestion.of({
            command: cmd.id,
            operation: cmd.operation,
            range: { from, to },
            insertAt,
            anchorPos: head,   // where the spinner sits until text arrives
            originalText: selection,
            proposedText: "",
            statusText: "",
            status: "streaming",
            sources: null,
          }),
          // Scroll the spinner into view so the user sees the op has started -
          // the proposal is otherwise off-screen for a whole-document op.
          EditorView.scrollIntoView(head, { y: "center" }),
        ],
      });

      // Document identity + frontmatter for retrieval-grounded commands
      // (and to finally light up _voice_hint for all commands). Both are
      // best-effort: a missing path or frontmatter just means the server
      // falls back to the cursor-only path, which is what every existing
      // command already uses.
      const path = getFilePath();
      const frontmatter = parseFrontmatter(state.doc.toString());

      try {
        const resp = await fetch("/api/edit/assist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            command: cmd.id, before, after, selection,
            path, frontmatter,
            content, cursor_offset: cursorOffset,
            selection_start: selStart, selection_end: selEnd,
            instruction,
          }),
        });
        if (!resp.ok || !resp.body) {
          throw new Error("HTTP " + resp.status);
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let sawDone = false;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (evt.token) {
              view.dispatch({ effects: appendToken.of(evt.token) });
            } else if (evt.status) {
              // Tool-calling editor progress ("Searching the wiki…"); shown as a
              // working indicator until the transformed text streams in.
              view.dispatch({ effects: setStatus.of(evt.status) });
            } else if (evt.retract) {
              // The loop discarded a turn's streamed tokens (it made a tool call);
              // drop what we've shown so far and keep waiting.
              view.dispatch({ effects: resetTokens.of(null) });
            } else if (evt.sources) {
              view.dispatch({ effects: setSources.of(evt.sources) });
            } else if (evt.error) {
              console.warn("edit_assist error:", evt.error);
              // Show the failure in place and leave it until the user dismisses
              // it (Esc / the Dismiss button) or the next action replaces it. It
              // used to auto-clear after 6s, but that flash was easy to miss
              // (effectively invisible on Firefox) - persistence is the fix.
              view.dispatch({ effects: markError.of(evt.error) });
              return;
            } else if (evt.note_saved) {
              // op:note - the result went to an external digest page, not the
              // document. Confirm with a clickable link (plus a memory link when
              // the editor also consolidated memory this run).
              const links = [{ label: wikiBaseName(evt.note_saved),
                               url: wikiUrl(evt.note_saved) }];
              if (evt.memory_saved) {
                links.push({ label: "memory", url: wikiUrl(evt.memory_saved) });
              }
              view.dispatch({ effects: markNotice.of({ label: "Saved to", links }) });
              return;
            } else if (evt.done) {
              sawDone = true;
              // Distinguish a real result from a no-op: an empty proposal, or a
              // replace whose output equals the original, means nothing changed -
              // say so rather than showing a confusing identical/blank proposal.
              const cur = view.state.field(suggestionField, false);
              const prop = cur ? cur.proposedText : "";
              const orig = cur ? (cur.originalText || "") : "";
              if (!prop.trim() || (cur.operation === "replace" && prop === orig)) {
                view.dispatch({ effects: markError.of("No changes suggested.") });
              } else {
                view.dispatch({ effects: markReady.of(null) });
              }
              return;
            }
          }
        }
        if (!sawDone) view.dispatch({ effects: markReady.of(null) });
      } catch (e) {
        console.error("edit_assist:", e);
        view.dispatch({ effects: clearSuggestion.of(null) });
      }
    }

    // --- Auto-link: candidate-picker flow (no LLM streaming) -------------
    // Sends the current selection + surrounding context to /api/edit/assist
    // (kind="autolink"), then renders a floating picker over the editor.
    // Click → replace the selection with a wikilink. Esc / click-outside
    // dismisses without modifying the document.
    async function runAutolink(view, cmd) {
      const state = view.state;
      const sel = state.selection.main;
      if (sel.from === sel.to) return;
      const from = sel.from, to = sel.to;
      const selection = state.doc.sliceString(from, to);
      const before = state.doc.sliceString(Math.max(0, from - 1000), from);
      const after  = state.doc.sliceString(to, Math.min(state.doc.length, to + 500));

      const path = getFilePath();
      const frontmatter = parseFrontmatter(state.doc.toString());

      // Show a transient placeholder so the user knows something is in flight.
      const pending = showAutolinkPicker(view, { from, to }, selection, null, "loading");

      try {
        const resp = await fetch("/api/edit/assist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            command: cmd.id, before, after, selection,
            path, frontmatter,
          }),
        });
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let candidates = null;
        let matchType = "none";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (Array.isArray(evt.candidates)) {
              candidates = evt.candidates;
              matchType = evt.autolink_match_type || (candidates.length ? "lexical" : "none");
            } else if (evt.error) {
              console.warn("autolink error:", evt.error);
              pending.dismiss();
              return;
            } else if (evt.done) {
              pending.dismiss();
              showAutolinkPicker(view, { from, to }, selection, candidates || [], matchType);
              return;
            }
          }
        }
        // Stream ended without an explicit done event.
        pending.dismiss();
        if (candidates) {
          showAutolinkPicker(view, { from, to }, selection, candidates, matchType);
        }
      } catch (e) {
        console.error("autolink:", e);
        pending.dismiss();
      }
    }

    // Build the wikilink string. Plain `[[Title]]` when the user's selection
    // already matches the page title verbatim; otherwise the piped form
    // `[[/path|original]]` so the prose keeps the writer's phrasing.
    function buildWikilink(candidate, selectionText) {
      const title = candidate.title || candidate.wiki_path;
      if (selectionText === title) return "[[" + title + "]]";
      return "[[/" + candidate.wiki_path + "|" + selectionText + "]]";
    }

    // Floating picker UI. Reusable for future selection-driven, search-style
    // commands (e.g. cite-this-claim) - the candidate shape just needs the
    // same {title, wiki_path, match_type, confidence} fields.
    //
    // Returns {dismiss}. Pass mode="loading" with candidates=null to render
    // a spinner placeholder; call .dismiss() before re-rendering with real
    // candidates.
    function showAutolinkPicker(view, range, selectionText, candidates, mode) {
      // Tear down any previously-open picker so we never stack two.
      if (window.__editAssistAutolinkPicker) {
        try { window.__editAssistAutolinkPicker.dismiss(); } catch (_) {}
      }

      const root = document.createElement("div");
      root.className = "slash-menu edit-assist-autolink-picker";

      function render() {
        root.innerHTML = "";
        if (mode === "loading") {
          const empty = document.createElement("div");
          empty.className = "slash-menu-empty";
          empty.textContent = "Searching for matching pages…";
          root.appendChild(empty);
          return;
        }
        if (!candidates || candidates.length === 0) {
          const empty = document.createElement("div");
          empty.className = "slash-menu-empty";
          empty.textContent = "No matching pages found";
          root.appendChild(empty);
          // Auto-dismiss the empty state after a moment so it doesn't linger.
          setTimeout(dismiss, 1200);
          return;
        }
        const header = document.createElement("div");
        header.className = "slash-menu-filter";
        header.textContent = mode === "lexical"
          ? "Title match"
          : "Semantic match (review before accepting)";
        root.appendChild(header);
        candidates.forEach((c, idx) => {
          const item = document.createElement("div");
          item.className = "slash-menu-item";
          if (idx === 0) item.classList.add("active");

          const label = document.createElement("div");
          label.textContent = c.title || c.wiki_path;
          item.appendChild(label);

          const meta = document.createElement("div");
          meta.className = "edit-assist-picker-meta";
          if (c.match_type === "semantic") {
            meta.textContent = "/" + c.wiki_path + "  -  similarity " + (c.confidence ?? "?");
          } else {
            meta.textContent = "/" + c.wiki_path;
          }
          item.appendChild(meta);

          item.addEventListener("mousedown", (ev) => {
            ev.preventDefault();
            const link = buildWikilink(c, selectionText);
            view.dispatch({
              changes: { from: range.from, to: range.to, insert: link },
              selection: { anchor: range.from + link.length },
            });
            dismiss();
            view.focus();
          });
          root.appendChild(item);
        });
      }

      function dismiss() {
        view.dispatch({ effects: autolinkSlot.setEffect.of(null) });
        document.removeEventListener("mousedown", onOutside, true);
        document.removeEventListener("keydown", onKey, true);
        if (window.__editAssistAutolinkPicker === handle) {
          window.__editAssistAutolinkPicker = null;
        }
      }
      function onOutside(ev) {
        if (!root.contains(ev.target)) dismiss();
      }
      function onKey(ev) {
        if (ev.key === "Escape") { ev.preventDefault(); dismiss(); }
      }

      // Render before handing to the tooltip manager (it measures on mount).
      // Anchor at the selection head (on-screen even for a long selection) and
      // scroll it in so the picker can't appear off-screen.
      render();
      const anchor = view.state.selection.main.head;
      whenAnchorVisible(view, anchor, () =>
        view.dispatch({ effects: autolinkSlot.setEffect.of(elementTooltip(anchor, root)) }));
      document.addEventListener("mousedown", onOutside, true);
      document.addEventListener("keydown", onKey, true);

      const handle = { dismiss };
      window.__editAssistAutolinkPicker = handle;
      return handle;
    }

    // --- Cite-this-claim: footnote-citation picker (kind="cite") ---------
    // Two-edits-on-accept flow: pick a candidate passage, the click
    // handler inserts `[^cite-N]` after the selection AND inserts the
    // `[^cite-N]: ...` definition at the next paragraph break - both as
    // a single CodeMirror transaction so undo restores them together.
    //
    // Footnote IDs are namespaced (`cite-N`) so they never collide with a
    // user's hand-written `[^1]` / `[^foo]` notes; the markdown footnotes
    // extension is configured with UNIQUE_IDS=False (doctransform.py),
    // which means the renderer trusts whatever IDs we emit.

    function nextFootnoteId(docText, prefix) {
      const usedPrefix = prefix || "cite-";
      const used = new Set();
      const re = /\[\^([^\]\s]+)\]/g;
      let m;
      while ((m = re.exec(docText))) used.add(m[1]);
      for (let i = 1; i < 10000; i++) {
        const id = usedPrefix + i;
        if (!used.has(id)) return id;
      }
      // Pathological fallback - only triggers if the doc somehow has
      // 10k existing cite-N markers, which would be a different problem.
      return usedPrefix + Date.now();
    }

    function findNextParagraphEnd(docText, fromPos) {
      // Index of the first blank-line break at or after fromPos. Handles
      // both LF (\n\n) and CRLF (\r\n\r\n) line endings. If no break is
      // found, returns docText.length so the definition appends at EOF.
      const tail = docText.slice(fromPos);
      const m = tail.match(/\r?\n\r?\n/);
      return m ? fromPos + m.index : docText.length;
    }

    function fenceAround(docText, pos) {
      // The fenced code block containing `pos`, or null. A fence spans blank
      // lines, so the paragraph rule below would otherwise cut one in half.
      // Closing rule matches the renderer's: same delimiter character, at least
      // as long as the opener, and no info string - which is what makes a
      // 4-backtick fence safely contain 3-backtick fences (nested fences are
      // real in this vault - see the help docs).
      const re = /^[ \t]*(`{3,}|~{3,})(.*)$/gm;
      let open = null, m;
      while ((m = re.exec(docText)) !== null) {
        const lineEnd = m.index + m[0].length;
        if (!open) {
          open = { start: m.index, marker: m[1] };
        } else if (m[1][0] === open.marker[0]
                   && m[1].length >= open.marker.length && !m[2].trim()) {
          if (pos >= open.start && pos <= lineEnd) {
            return { from: open.start, to: lineEnd };
          }
          open = null;
        }
      }
      // An unclosed fence runs to the end of the buffer.
      if (open && pos >= open.start) return { from: open.start, to: docText.length };
      return null;
    }

    function blockAt(docText, pos) {
      // The block containing `pos`: a fenced block if we're inside one, else the
      // run of text between blank lines. Returns null when there is no block -
      // the caret is on a blank line, or the buffer is empty - which callers
      // treat as "nothing to replace, just insert here".
      const fence = fenceAround(docText, pos);
      if (fence) return fence;
      let from = 0;
      const re = /\r?\n\r?\n/g;
      let m;
      while ((m = re.exec(docText.slice(0, pos))) !== null) {
        from = m.index + m[0].length;
      }
      const to = findNextParagraphEnd(docText, pos);
      return docText.slice(from, to).trim() ? { from, to } : null;
    }

    function buildCitationDef(id, candidate) {
      // `[^id]: [[/path#anchor | Title]] - "quote…"`
      // Anchors are a python-markdown TOC slug of the deepest header in
      // the chunk's header_path; the server computes them so the JS side
      // doesn't need to duplicate the slugify logic.
      const title = candidate.title || candidate.wiki_path;
      let target = "/" + candidate.wiki_path;
      if (candidate.header_anchor) target += "#" + candidate.header_anchor;
      const link = "[[" + target + " | " + title + "]]";
      const quote = candidate.quote ? ' - "' + candidate.quote + '…"' : "";
      return "[^" + id + "]: " + link + quote;
    }

    async function runCite(view, cmd) {
      const state = view.state;
      const sel = state.selection.main;
      if (sel.from === sel.to) return;
      const from = sel.from, to = sel.to;
      const selection = state.doc.sliceString(from, to);
      const before = state.doc.sliceString(Math.max(0, from - 1000), from);
      const after  = state.doc.sliceString(to, Math.min(state.doc.length, to + 500));

      const path = getFilePath();
      const frontmatter = parseFrontmatter(state.doc.toString());

      const pending = showCitePicker(view, { from, to }, selection, null, "loading");

      try {
        const resp = await fetch("/api/edit/assist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            command: cmd.id, before, after, selection,
            path, frontmatter,
          }),
        });
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let candidates = null;
        let matchType = "none";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (Array.isArray(evt.candidates)) {
              candidates = evt.candidates;
              matchType = evt.cite_match_type || (candidates.length ? "semantic" : "none");
            } else if (evt.error) {
              console.warn("cite_claim error:", evt.error);
              pending.dismiss();
              return;
            } else if (evt.done) {
              pending.dismiss();
              showCitePicker(view, { from, to }, selection, candidates || [], matchType);
              return;
            }
          }
        }
        // Stream ended without an explicit done event.
        pending.dismiss();
        if (candidates) {
          showCitePicker(view, { from, to }, selection, candidates, matchType);
        }
      } catch (e) {
        console.error("cite_claim:", e);
        pending.dismiss();
      }
    }

    // Floating picker for citation candidates. Each row shows the source
    // page title, the header path within the page, a snippet of the
    // matching passage, and a similarity score - all four are needed for
    // the user to judge whether the passage actually supports the claim
    // (which is the whole reason this is a picker and not an auto-apply).
    function showCitePicker(view, range, selectionText, candidates, mode) {
      if (window.__editAssistCitePicker) {
        try { window.__editAssistCitePicker.dismiss(); } catch (_) {}
      }

      const root = document.createElement("div");
      root.className = "slash-menu edit-assist-cite-picker";

      function render() {
        root.innerHTML = "";
        if (mode === "loading") {
          const empty = document.createElement("div");
          empty.className = "slash-menu-empty";
          empty.textContent = "Searching for supporting passages…";
          root.appendChild(empty);
          return;
        }
        if (!candidates || candidates.length === 0) {
          const empty = document.createElement("div");
          empty.className = "slash-menu-empty";
          empty.textContent = "No supporting passages found";
          root.appendChild(empty);
          setTimeout(dismiss, 1200);
          return;
        }
        const header = document.createElement("div");
        header.className = "slash-menu-filter";
        header.textContent = "Cite from corpus (review before accepting)";
        root.appendChild(header);

        candidates.forEach((c, idx) => {
          const item = document.createElement("div");
          item.className = "slash-menu-item";
          //if (idx === 0) item.classList.add("active");

          const label = document.createElement("div");
          label.className = "edit-assist-picker-title";
          label.textContent = c.title || c.wiki_path;
          item.appendChild(label);

          if (c.header_path) {
            const hdr = document.createElement("div");
            hdr.className = "edit-assist-picker-subtitle";
            hdr.textContent = c.header_path;
            item.appendChild(hdr);
          }

          if (c.snippet) {
            const snip = document.createElement("div");
            snip.className = "edit-assist-picker-snippet";
            snip.textContent = c.snippet + (c.snippet.length >= 240 ? "…" : "");
            item.appendChild(snip);
          }

          const meta = document.createElement("div");
          meta.className = "edit-assist-picker-meta";
          meta.textContent = "/" + c.wiki_path
            + "  -  similarity " + (c.confidence ?? "?");
          item.appendChild(meta);

          item.addEventListener("mousedown", (ev) => {
            ev.preventDefault();
            const docText = view.state.doc.toString();
            const id = nextFootnoteId(docText);
            const marker = "[^" + id + "]";
            const defLine = "\n\n" + buildCitationDef(id, c);
            const paraEnd = findNextParagraphEnd(docText, range.to);
            // Two non-overlapping changes in one transaction. CodeMirror
            // applies them at their absolute positions in the original
            // document, so paraEnd does not need to be shifted to account
            // for the inline marker insertion at range.to (which precedes
            // paraEnd in the doc).
            view.dispatch({
              changes: [
                { from: range.to, insert: marker },
                { from: paraEnd,  insert: defLine },
              ],
              selection: { anchor: range.to + marker.length },
            });
            dismiss();
            view.focus();
          });
          root.appendChild(item);
        });
      }

      function dismiss() {
        view.dispatch({ effects: citeSlot.setEffect.of(null) });
        document.removeEventListener("mousedown", onOutside, true);
        document.removeEventListener("keydown", onKey, true);
        if (window.__editAssistCitePicker === handle) {
          window.__editAssistCitePicker = null;
        }
      }
      function onOutside(ev) {
        if (!root.contains(ev.target)) dismiss();
      }
      function onKey(ev) {
        if (ev.key === "Escape") { ev.preventDefault(); dismiss(); }
      }

      render();
      const anchor = view.state.selection.main.head;
      whenAnchorVisible(view, anchor, () =>
        view.dispatch({ effects: citeSlot.setEffect.of(elementTooltip(anchor, root)) }));
      document.addEventListener("mousedown", onOutside, true);
      document.addEventListener("keydown", onKey, true);

      const handle = { dismiss };
      window.__editAssistCitePicker = handle;
      return handle;
    }

    return [
      suggestionField,
      Prec.highest(aiKeymap),
      Prec.highest(slashMenuKeymap),
      slashTrigger,
      slashSlot.field,
      autolinkSlot.field,
      citeSlot.field,
      // position:"fixed" parents tooltips to <body>, so the menus can't be
      // clipped by the editor's `.cm-scroller { overflow: auto }`.
      tooltips({ position: "fixed" }),
    ];
  }

  window.EditAssist = {
    extension: buildExtension,
    // True while a suggestion is on screen (streaming, ready, error or notice).
    hasPending: (state) =>
      activeSuggestionField ? state.field(activeSuggestionField, false) != null : false,
  };
})();
