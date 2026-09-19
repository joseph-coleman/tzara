// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Keyboard navigation for every page (loaded from _header.html).

     Mod-P  open the palette            Mod-E  edit this page (on a page view)

   The palette searches this vault's pages. Prefixes switch what it lists:
     >text        commands: the page's links and buttons tagged `data-command`,
                  those a page registers in `window.PaletteCommands`, plus
                  "Switch vault" and "Manage vaults"
     @text        vaults; Enter opens a vault's start page, Tab picks it
     @vault text  pages in another vault
   A page query with no exact match also offers to create that page, and any
   page query offers a full-text search.

   A tagged element runs by el.click(). Optional attributes on it:
     data-command-key="Mod-s"          its shortcut, shown beside the label
     data-command-confirm="Question?"  asked before running from the palette
   Disabled elements and those under a [hidden] ancestor are not listed; a
   checkbox shows its on/off state. window.PaletteCommands entries are
   {label, run, key?, when?}, for actions with no single element to click;
   when() decides at each open whether the command applies.

   Pages come from GET /api/link-targets, the list the editor's [[ autocomplete
   uses, filtered the same way: notes and canvases only, agent run logs only
   once the query names their folder.

   Mod is Cmd on macOS and Ctrl elsewhere. Mod-P works inside the markdown
   editor too; Mod-E does not act while typing anywhere, so the editor keeps it
   for inline code. The pure helpers are top-level named functions so
   app/.test/test_palette.js can slice them out by name.
*/

// Split palette input into what to list:
//   {mode: "commands", query}              ">text"
//   {mode: "vaults", query}                "@text" (no space yet)
//   {mode: "pages", vault: "text", query}  "@text query" - another vault's pages
//   {mode: "pages", vault: null, query}    anything else - this vault's pages
function parseInput(text) {
  if (text.startsWith(">")) return { mode: "commands", vault: null, query: text.slice(1).trim() };
  const at = /^@(\S*)(?:\s+([\s\S]*))?$/.exec(text);
  if (at) {
    if (at[2] === undefined) return { mode: "vaults", vault: null, query: at[1] };
    return { mode: "pages", vault: at[1], query: at[2].trim() };
  }
  return { mode: "pages", vault: null, query: text.trim() };
}

// The vault an `@token` names: an exact slug, else the first whose slug or
// display name starts with it. null when nothing does.
function resolveVault(token, vaults) {
  const t = token.toLowerCase();
  if (!t) return null;
  const exact = vaults.find((v) => v.vault_id.toLowerCase() === t);
  if (exact) return exact;
  return vaults.find((v) => v.vault_id.toLowerCase().startsWith(t)
                         || (v.display_name || "").toLowerCase().startsWith(t)) || null;
}

function isWordStart(lower, i) {
  return i === 0 || /[\s/_\-.]/.test(lower[i - 1]);
}

// Score `label` against a space-separated query, or null when a term does not
// match. Each term must appear as a substring (strong) or, failing that, as an
// in-order subsequence (weak). Word starts and the last path segment rank
// higher; shorter labels win otherwise-equal matches. `marks` are the matched
// character indexes, for highlighting. An empty query matches with score 0.
function fuzzyScore(query, label) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return { score: 0, marks: [] };
  const lower = label.toLowerCase();
  const lastSegment = lower.lastIndexOf("/") + 1;
  const marks = new Set();
  let score = -label.length / 10;
  for (const term of terms) {
    let best = -1;
    for (let i = lower.indexOf(term); i >= 0; i = lower.indexOf(term, i + 1)) {
      if (best < 0) best = i;
      if (isWordStart(lower, i)) { best = i; break; }
    }
    if (best >= 0) {
      score += 100 + (isWordStart(lower, best) ? 50 : 0) + (best >= lastSegment ? 30 : 0);
      for (let k = 0; k < term.length; k++) marks.add(best + k);
      continue;
    }
    let from = 0, prev = -2;
    for (const ch of term) {
      const i = lower.indexOf(ch, from);
      if (i < 0) return null;
      score += i === prev + 1 ? 5 : isWordStart(lower, i) ? 3 : 1;
      marks.add(i);
      prev = i;
      from = i + 1;
    }
  }
  return { score, marks: [...marks].sort((a, b) => a - b) };
}

// Agent-owned pages rank below the author's own; run logs lowest (as in the
// [[ autocomplete's GROUP_BOOST).
function groupPenalty(group) {
  return group === "agent" ? -50 : group === "log" ? -99 : 0;
}

// What the palette navigates to: notes and canvases, not attachments.
function isPage(path) {
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

// Run logs are listed only once the query names their folder (`apod2/logs/`),
// the same opt-in as wikilink_complete.js.
function logsWanted(query, logFolders) {
  const q = query.toLowerCase();
  for (const folder of logFolders) {
    if (q.includes(folder.toLowerCase() + "/")) return true;
  }
  return false;
}

// /api/link-targets rows matching `query`, best first (alphabetical when the
// query is empty), at most `limit`.
function rankPages(rows, query, limit) {
  const logFolders = new Set(rows.filter((r) => r.group === "log").map((r) => logFolder(r.path)));
  const wantLogs = logsWanted(query, logFolders);
  const ranked = [];
  for (const row of rows) {
    if (!isPage(row.path) || (row.group === "log" && !wantLogs)) continue;
    const label = row.path.replace(/\.md$/i, "");
    const match = fuzzyScore(query, label);
    if (match) {
      ranked.push({ path: row.path, label, marks: match.marks,
                    score: match.score + groupPenalty(row.group) });
    }
  }
  ranked.sort((a, b) => b.score - a.score || a.label.localeCompare(b.label));
  return ranked.slice(0, limit);
}

// Whether `query` already names a page, by path or by bare name.
function hasExactPage(rows, query) {
  const q = query.trim().replace(/^\/+|\/+$/g, "").replace(/\.md$/i, "").toLowerCase();
  return rows.some((row) => {
    if (!isPage(row.path)) return false;
    const label = row.path.replace(/\.md$/i, "").toLowerCase();
    return label === q || label.slice(label.lastIndexOf("/") + 1) === q;
  });
}

// Whether `query` can name a new page: something left after trimming slashes,
// and none of the characters Obsidian refuses in file names.
function creatable(query) {
  const rel = query.trim().replace(/^\/+|\/+$/g, "");
  return rel !== "" && !/[\\:*?"<>|#^\[\]]/.test(rel);
}

function encodePath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

function pageHref(vault, path) {
  return "/wiki/" + encodeURIComponent(vault) + "/" + encodePath(path.replace(/\.md$/i, ""));
}

// Opening /edit/ for a path that doesn't exist is create mode.
function createHref(vault, query) {
  const rel = query.trim().replace(/^\/+|\/+$/g, "").replace(/\.md$/i, "");
  return "/edit/" + encodeURIComponent(vault) + "/" + encodePath(rel);
}

function searchHref(vault, query) {
  return "/search/" + encodeURIComponent(vault) + "?q=" + encodeURIComponent(query);
}

function vaultHref(vault) {
  return pageHref(vault.vault_id, vault.default_page || "Main");
}

// A CodeMirror-style key name for display: "Mod-Shift-d" -> "Ctrl+Shift+D"
// ("Cmd+Shift+D" on macOS). Empty when there is no key.
function shortcutLabel(key, mac) {
  if (!key) return "";
  return key.split("-").map((part) => {
    if (part === "Mod") return mac ? "Cmd" : "Ctrl";
    return part.length === 1 ? part.toUpperCase() : part;
  }).join("+");
}

(function () {
  // How long a vault's page list is reused - the same freshness as the [[
  // autocomplete, so pages created in another tab show up on the next open.
  const TARGETS_TTL_MS = 10000;
  // Rows rendered per page query; the ranking puts the useful ones first.
  const MAX_ROWS = 50;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  const script = document.currentScript;
  const currentVault = (script && script.dataset.vault) || "main";

  const targetCache = new Map();   // vault -> {at, rows}
  let vaults = null;
  let dialog = null, input = null, list = null;
  let items = [];
  let active = 0;
  let renderSeq = 0;

  function isMod(event) {
    return isMac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  }

  function isEditable(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }

  async function loadTargets(vault) {
    const hit = targetCache.get(vault);
    if (hit && Date.now() - hit.at < TARGETS_TTL_MS) return hit.rows;
    const resp = await fetch("/api/link-targets?" + new URLSearchParams({ vault, source: "" }));
    if (!resp.ok) throw new Error("pages: HTTP " + resp.status);
    const rows = await resp.json();
    targetCache.set(vault, { at: Date.now(), rows });
    return rows;
  }

  async function loadVaults() {
    if (!vaults) {
      const resp = await fetch("/api/vaults");
      if (!resp.ok) throw new Error("vaults: HTTP " + resp.status);
      vaults = (await resp.json()).vaults || [];
    }
    return vaults;
  }

  // Commands are the page's own links and buttons, so each page offers exactly
  // what it shows (Edit only where editing applies, Save only in the editor,
  // and so on). The first element with a label wins.
  function commands() {
    const out = [];
    const seen = new Set();
    const add = (cmd) => {
      if (seen.has(cmd.label)) return;
      seen.add(cmd.label);
      out.push(cmd);
    };
    for (const el of document.querySelectorAll("[data-command]")) {
      if (el.disabled || el.closest("[hidden]")) continue;
      const detail = [shortcutLabel(el.dataset.commandKey, isMac)];
      if (el.type === "checkbox") detail.push(el.checked ? "on" : "off");
      add({ label: el.dataset.command, run: () => el.click(), href: el.getAttribute("href"),
            detail: detail.filter(Boolean).join(" · "), confirm: el.dataset.commandConfirm });
    }
    for (const c of window.PaletteCommands || []) {
      if (!c.when || c.when()) add({ label: c.label, run: c.run, detail: shortcutLabel(c.key, isMac) });
    }
    add({ label: "Switch vault", fill: "@" });
    add({ label: "Manage vaults", href: "/vaults" });
    return out;
  }

  async function buildRows(p) {
    if (p.mode === "commands") {
      const rows = [];
      for (const c of commands()) {
        const match = fuzzyScore(p.query, c.label);
        if (match) rows.push({ ...c, marks: match.marks, score: match.score });
      }
      rows.sort((a, b) => b.score - a.score);   // stable: nav order on ties
      return rows.length ? rows : [{ label: "No matching command", muted: true }];
    }

    if (p.mode === "vaults") {
      const rows = [];
      for (const v of await loadVaults()) {
        const label = v.display_name || v.vault_id;
        const onName = fuzzyScore(p.query, label);
        const match = onName || fuzzyScore(p.query, v.vault_id);
        if (!match) continue;
        rows.push({
          label, marks: onName ? onName.marks : [], score: match.score,
          detail: v.vault_id + (v.vault_id === currentVault ? " · current" : ""),
          href: vaultHref(v), complete: "@" + v.vault_id + " ",
        });
      }
      rows.sort((a, b) => b.score - a.score);
      return rows.length ? rows : [{ label: "No vault matches “@" + p.query + "”", muted: true }];
    }

    let vault = currentVault;
    let detail = "";
    if (p.vault) {
      const v = resolveVault(p.vault, await loadVaults());
      if (!v) return [{ label: "No vault matches “@" + p.vault + "”", muted: true }];
      vault = v.vault_id;
      if (vault !== currentVault) detail = v.display_name || v.vault_id;
    }
    const targets = await loadTargets(vault);
    const rows = rankPages(targets, p.query, MAX_ROWS).map((r) => ({
      label: r.label, marks: r.marks, dir: r.label.lastIndexOf("/") + 1,
      detail, href: pageHref(vault, r.path),
    }));
    if (p.query) {
      if (creatable(p.query) && !hasExactPage(targets, p.query)) {
        rows.push({ label: "Create page “" + p.query + "”", detail, href: createHref(vault, p.query) });
      }
      rows.push({ label: "Search for “" + p.query + "”", detail, href: searchHref(vault, p.query) });
    }
    return rows.length ? rows : [{ label: "No pages", muted: true }];
  }

  async function refresh() {
    const seq = ++renderSeq;
    let rows;
    try {
      rows = await buildRows(parseInput(input.value));
    } catch (e) {
      console.warn("palette:", e);
      rows = [{ label: "Couldn't load: " + e.message, muted: true }];
    }
    if (seq !== renderSeq) return;   // a later keystroke owns the list
    items = rows;
    active = Math.max(0, items.findIndex((r) => !r.muted));
    draw();
  }

  // The label as text runs: the folder part dimmed, matched characters bold.
  function labelNode(item) {
    const out = document.createElement("span");
    out.className = "palette-label";
    const marks = new Set(item.marks || []);
    const dir = item.dir || 0;
    let run = null, runKey = "";
    for (let i = 0; i < item.label.length; i++) {
      const key = (i < dir ? "d" : "t") + (marks.has(i) ? "b" : "");
      if (key !== runKey) {
        run = document.createElement(marks.has(i) ? "b" : "span");
        if (i < dir) run.className = "palette-dir";
        out.append(run);
        runKey = key;
      }
      run.textContent += item.label[i];
    }
    return out;
  }

  function draw() {
    list.replaceChildren(...items.map((item, i) => {
      const li = document.createElement("li");
      li.id = "palette-opt-" + i;
      li.className = "palette-item" + (item.muted ? " muted" : "") + (i === active ? " active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.append(labelNode(item));
      if (item.detail) {
        const detail = document.createElement("span");
        detail.className = "palette-detail";
        detail.textContent = item.detail;
        li.append(detail);
      }
      if (!item.muted) {
        li.addEventListener("mousemove", () => setActive(i));
        li.addEventListener("click", (e) => activate(item, isMod(e)));
      }
      return li;
    }));
    input.setAttribute("aria-activedescendant", items.length ? "palette-opt-" + active : "");
    if (list.children[active]) list.children[active].scrollIntoView({ block: "nearest" });
  }

  function setActive(i) {
    if (i === active || !items[i] || items[i].muted) return;
    const rows = list.children;
    rows[active].classList.remove("active");
    rows[active].setAttribute("aria-selected", "false");
    active = i;
    rows[i].classList.add("active");
    rows[i].setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", rows[i].id);
    rows[i].scrollIntoView({ block: "nearest" });
  }

  function move(step) {
    for (let n = 1; n <= items.length; n++) {
      const i = (active + step * n + items.length * n) % items.length;
      if (!items[i].muted) { setActive(i); return; }
    }
  }

  function activate(item, newTab) {
    if (item.fill !== undefined) {
      input.value = item.fill;
      input.focus();
      refresh();
      return;
    }
    if (newTab && item.href && item.href !== "#") {
      window.open(item.href, "_blank", "noopener");
      return;
    }
    dialog.close();
    if (item.confirm && !window.confirm(item.confirm)) return;
    if (item.run) item.run();
    else if (item.href) window.location.href = item.href;
  }

  function onInputKey(event) {
    if (event.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      move(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = items[active];
      if (item && !item.muted) activate(item, isMod(event));
    } else if (event.key === "Tab") {
      event.preventDefault();   // focus stays in the palette
      const item = items[active];
      if (item && item.complete) {
        input.value = item.complete;
        refresh();
      }
    }
  }

  function build() {
    const mod = isMac ? "Cmd" : "Ctrl";
    dialog = document.createElement("dialog");
    dialog.className = "palette";
    dialog.setAttribute("aria-label", "Go to page or command");
    const body = document.createElement("div");
    body.className = "palette-body";
    input = document.createElement("input");
    input.className = "palette-input";
    input.type = "text";
    input.placeholder = "Go to a page…   > commands   @ vaults";
    input.spellcheck = false;
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", "palette-list");
    input.setAttribute("aria-autocomplete", "list");
    list = document.createElement("ul");
    list.className = "palette-list";
    list.id = "palette-list";
    list.setAttribute("role", "listbox");
    const hint = document.createElement("div");
    hint.className = "palette-hint";
    hint.textContent = "↑↓ select · Enter open · " + mod
                       + "+Enter new tab · Tab pick vault · Esc close";
    body.append(input, list, hint);
    dialog.append(body);
    document.body.append(dialog);
    input.addEventListener("input", refresh);
    input.addEventListener("keydown", onInputKey);
    // A click that lands on the dialog itself (not its body) is the backdrop.
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  }

  function toggle() {
    if (!dialog) build();
    if (dialog.open) {
      dialog.close();
      return;
    }
    input.value = "";
    items = [];
    list.replaceChildren();
    dialog.showModal();   // top layer, focus trap, Esc, focus restored on close
    input.focus();
    refresh();
  }

  // Capture phase, so Mod-P reaches the palette before CodeMirror sees it.
  window.addEventListener("keydown", (event) => {
    if (event.isComposing || !isMod(event) || event.altKey || event.shiftKey) return;
    const key = event.key.toLowerCase();
    if (key === "p") {
      event.preventDefault();   // the browser's Print
      event.stopPropagation();
      toggle();
    } else if (key === "e" && !isEditable(event.target)) {
      const edit = document.getElementById("nav_edit");
      if (edit) {
        event.preventDefault();
        edit.click();
      }
    }
  }, true);
})();
