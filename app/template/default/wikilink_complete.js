// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

/* [[wikilink]] autocomplete for the /edit/ view CodeMirror editor.

   `[[` offers the vault's notes and canvases, `![[` every file (attachments
   embed), and `#` after a target (`[[Page#`) that note's headings. The link
   TEXT comes from GET /api/link-targets, computed server-side by the resolver
   the renderer, graph and move-rewriter share: the shortest form that still
   resolves to the file from this document's folder. Nothing here re-implements
   resolution, so an accepted completion can never render as a broken link.
   A leading `/` (`[[/Folder/...`) asks for a root-anchored link instead.
   Agent-owned files (the server's `group`) rank below the author's pages, and
   agent run logs are left out until the query names their folder.

   The pure helpers are top-level named functions so
   app/.test/test_wikilink_complete.js can slice them out by name.
*/

// Where the caret sits relative to an open wikilink, from the line text before it:
//   {kind: "page", embed, anchored, from, query}   inside `[[query` / `![[/query`
//   {kind: "heading", embed, target, from, query}  inside `[[target#query`
//   null                                           anywhere else (incl. `#^block`, `|alias`)
// `from` is the offset within lineBefore where the replaceable query starts.
function wikilinkContext(lineBefore) {
  const heading = /(!?)\[\[([^\[\]|#\n]+)#([^\[\]|#^\n]*)$/.exec(lineBefore);
  if (heading) {
    return { kind: "heading", embed: heading[1] === "!", target: heading[2],
             from: lineBefore.length - heading[3].length, query: heading[3] };
  }
  const page = /(!?)\[\[(\/?)([^\[\]|#\n]*)$/.exec(lineBefore);
  if (page) {
    return { kind: "page", embed: page[1] === "!", anchored: page[2] === "/",
             from: lineBefore.length - page[3].length, query: page[3] };
  }
  return null;
}

// Length of an existing link target continuing past the caret, so completing
// inside `[[Qu|antum]]` replaces the whole target. Only counts when the link is
// visibly closed (`]]`, `|alias` or `#heading` follows); otherwise the rest of
// the line is prose and is left alone.
function targetTail(lineAfter) {
  const m = /^[^\[\]|#\n]*(?=\]\]|\||#)/.exec(lineAfter);
  return m ? m[0].length : 0;
}

// Closing brackets to add after an accepted completion: none when the link is
// already closed or continues (closeBrackets turns a typed `[[` into `[[]]`).
function closingInsert(after) {
  return /^(\]\]|\||#)/.test(after) ? "" : "]]";
}

// Files a completion offers: a plain [[link]] targets notes and canvases; an
// embed may target any file.
function offeredFor(path, embed) {
  if (embed) return true;
  const lower = path.toLowerCase();
  return lower.endsWith(".md") || lower.endsWith(".canvas");
}

// The folder a file sits in (its last directory segment), e.g. "logs".
function logFolder(path) {
  const cut = path.lastIndexOf("/");
  if (cut < 0) return "";
  const dir = path.slice(0, cut);
  return dir.slice(dir.lastIndexOf("/") + 1);
}

// Agent run logs outnumber real pages by orders of magnitude, so they are offered
// only once the query names their folder (`[[apod2/logs/`) - the list_documents
// rule for agents. `logFolders` comes from the rows, not a hard-coded name.
function logsWanted(query, logFolders) {
  const q = query.toLowerCase();
  for (const folder of logFolders) {
    if (q.includes(folder.toLowerCase() + "/")) return true;
  }
  return false;
}

(function () {
  // How long a fetched target list is reused across `[[` sessions. Short: pages
  // created in another tab should show up on the next `[[`.
  const TARGETS_TTL_MS = 10000;
  // Lezer markdown nodes whose text is literal code - no completion inside.
  const CODE_NODES = new Set(["FencedCode", "CodeBlock", "InlineCode", "CodeText"]);
  // Characters a query may contain; typing anything else ends the session.
  const QUERY_RE = /^[^\[\]|#\n]*$/;
  // Agent-owned pages rank below the author's own; opted-in run logs lowest.
  // Boost only breaks near-ties - a clearly better match still wins.
  const GROUP_BOOST = { agent: -50, log: -99 };

  function extension(opts) {
    const cm = window.CMEditor;
    if (!cm || !cm.autocompletion || !cm.syntaxTree || !cm.pickedCompletion) {
      console.warn("wikilink_complete: CodeMirror bundle lacks autocompletion - disabled");
      return [];
    }
    const { autocompletion, pickedCompletion, syntaxTree, EditorView, Prec } = cm;
    const vault = (opts && opts.vault) || "main";
    const source = (opts && opts.source) || "";
    let targets = null;
    let targetsAt = 0;

    async function getJSON(route, params) {
      const qs = new URLSearchParams(Object.assign({ vault, source }, params));
      const resp = await fetch(route + "?" + qs.toString());
      if (!resp.ok) throw new Error(route + ": HTTP " + resp.status);
      return resp.json();
    }

    async function loadTargets() {
      if (!targets || Date.now() - targetsAt > TARGETS_TTL_MS) {
        targets = await getJSON("/api/link-targets", {});
        targetsAt = Date.now();
      }
      return targets;
    }

    function inCode(state, pos) {
      for (let node = syntaxTree(state).resolveInner(pos, -1); node; node = node.parent) {
        if (CODE_NODES.has(node.name)) return true;
      }
      return false;
    }

    // Swap the query for `text`, close the link unless it already is, and leave
    // the caret after `]]` (or right after the text when an alias/heading follows).
    function applyText(text) {
      return (view, completion, from, to) => {
        const after = view.state.sliceDoc(to, to + 2);
        const close = closingInsert(after);
        const pastBrackets = close || after.startsWith("]]") ? 2 : 0;
        view.dispatch({
          changes: { from, to, insert: text + close },
          selection: { anchor: from + text.length + pastBrackets },
          annotations: pickedCompletion.of(completion),
          userEvent: "input.complete",
        });
      };
    }

    async function wikilinkSource(context) {
      const { state, pos } = context;
      const line = state.doc.lineAt(pos);
      const wl = wikilinkContext(state.sliceDoc(line.from, pos));
      if (!wl || inCode(state, pos)) return null;
      const from = line.from + wl.from;
      const to = pos + targetTail(state.sliceDoc(pos, line.to));

      let options;
      let validFor = QUERY_RE;
      try {
        if (wl.kind === "heading") {
          const data = await getJSON("/api/link-headings", { target: wl.target });
          // CM ranks by match score, then alphabetically. A boost falling with
          // position makes ties - including the whole list before any query is
          // typed - keep outline order. CM clamps boost to -99..99, so past ~200
          // headings the tail ties and falls back to alphabetical.
          options = data.headings.map((h, i) => ({
            label: h.text,
            detail: "h" + h.level,
            boost: Math.max(99 - i, -99),
            apply: applyText(h.text),
          }));
        } else {
          const all = await loadTargets();
          const logFolders = new Set(
            all.filter((t) => t.group === "log").map((t) => logFolder(t.path)));
          const wantLogs = logsWanted(wl.query, logFolders);
          // CM reuses this result while typing stays validFor, so it must go stale
          // when the query starts (or stops) naming a log folder - otherwise a
          // session opened at a bare `[[` could never offer the logs.
          validFor = (text) => QUERY_RE.test(text)
                               && logsWanted(text, logFolders) === wantLogs;
          options = all
            .filter((t) => offeredFor(t.path, wl.embed)
                           && (t.group !== "log" || wantLogs))
            .map((t) => {
              // Label by full path so a typed folder fragment matches too. An
              // anchored query already has its "/", so it gets the root path.
              const label = t.path.replace(/\.md$/i, "");
              return { label, boost: GROUP_BOOST[t.group] || 0,
                       apply: applyText(wl.anchored ? label : t.link) };
            });
        }
      } catch (e) {
        console.warn("wikilink_complete:", e);
        return null;
      }
      if (context.aborted) return null;
      return { from, to, options, validFor };
    }

    return [
      // `override` keeps markdown()'s own HTML-tag completion switched off, as it
      // was before any autocompletion extension was installed.
      autocompletion({ override: [wikilinkSource], icons: false }),
      // CM-owned classes: these must live in EditorView.theme() - a rule in
      // tzara.css's @layer site loses to CM's unlayered base theme. Prec.highest
      // also puts them over oneDark's tooltip colors. Mirrors .slash-menu.
      Prec.highest(EditorView.theme({
        ".cm-tooltip.cm-tooltip-autocomplete": {
          backgroundColor: "var(--bg-color)",
          color: "var(--fg-color)",
          border: "1px solid var(--darker)",
          borderRadius: "var(--radius-sm)",
          boxShadow: "0 4px 16px rgba(0, 0, 0, 0.25)",   // same as .slash-menu
          fontSize: "var(--text-smd)",
          overflow: "hidden",
        },
        ".cm-tooltip.cm-tooltip-autocomplete > ul": {
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          maxHeight: "40vh",
          padding: "var(--space-xs) 0",
        },
        ".cm-tooltip.cm-tooltip-autocomplete > ul > li": {
          padding: "var(--space-xs) var(--space-md)",
          lineHeight: "1.35",
        },
        ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": {
          backgroundColor: "var(--lighter)",
          color: "var(--fg-color)",
        },
        ".cm-completionMatchedText": {
          textDecoration: "none",
          fontWeight: "bold",
        },
        ".cm-completionDetail": {
          fontStyle: "normal",
          fontSize: "var(--text-sm)",
          marginLeft: "var(--space-sm)",
          opacity: "0.6",
        },
      })),
    ];
  }

  window.WikilinkComplete = { extension };
})();
