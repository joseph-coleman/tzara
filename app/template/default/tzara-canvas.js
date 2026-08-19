// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.TzaraCanvas = factory();
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // Default tween duration shared by node animateTo/Size/Bounds, camera
  // tweens, and edge.animate(). Overridable per-call via opts.duration.
  const DEFAULT_ANIM_MS = 350;
  // Default duration for the pulse visual effect.
  const PULSE_DEFAULT_MS = 600;

  // ====================================================================
  // Easing presets, exposed at TzaraCanvas.easings. Semantic names so
  // host code reads naturally - "easing: TzaraCanvas.easings.smooth"
  // instead of "easing: 'ease-in-out'". CSS-style strings still work too
  // via resolveEasing() below; consumers can pick either form, or pass
  // their own (t: 0..1) => number function.
  // ====================================================================
  const EASINGS = {
    // Constant speed. No acceleration.
    linear: t => t,
    // Fast start, gentle landing. The natural default for camera moves and
    // node animations - feels like things "settling into place." (≡ ease-out)
    smooth: t => 1 - (1 - t) * (1 - t),
    // Slow start, snaps to the end. Use when an animation needs anticipation
    // before a definitive arrival. (≡ ease-in)
    snappy: t => t * t,
    // Symmetric: gentle on both ends, fast through the middle. Best for
    // back-and-forth motion and breathing/looping effects. (≡ ease-in-out)
    glide:  t => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2,
    matrix: t => t / 2 + (1 - Math.pow(1 - 2 * t, 11)) / 4,
  };

  // ====================================================================
  // Keyboard shortcut machinery
  // --------------------------------------------------------------------
  // Three pieces work together:
  //
  //   1. parseKey / eventToCanonical / normalizeBinding - turn human
  //      strings like "Mod+Z" and KeyboardEvents into a shared canonical
  //      form used by the matcher. "Mod" resolves to Meta on Mac and
  //      Ctrl on everything else.
  //   2. DEFAULT_SHORTCUTS - the built-in action registry. Each entry
  //      names the Canvas method to dispatch to, lists its default key
  //      bindings, and carries the description/group used by the help
  //      dialog. Host-registered actions live alongside these at runtime.
  //   3. HELP_TOPICS - ordering + non-keyboard items (mouse interactions
  //      like Click, Shift+Drag, Alt+Click+Drag) for the help dialog.
  //      Items tagged with `action:` pull their key labels from the live
  //      registry, so a rebound or disabled action stays in sync.
  // ====================================================================

  const IS_MAC = (typeof navigator !== "undefined") && /Mac|iPhone|iPad|iPod/i.test(
    (navigator.platform || "") + " " + (navigator.userAgent || "")
  );

  // Map common spelling variants to a single canonical key name. Lookup is
  // case-insensitive (caller lowercases before probing). Keeps the parser
  // tolerant of host shorthand while the matcher works in one space.
  const _KEY_ALIASES = {
    esc: "Escape", escape: "Escape",
    "return": "Enter", enter: "Enter",
    spc: "Space", space: " ",
    del: "Delete", "delete": "Delete",
    ins: "Insert", insert: "Insert",
    bksp: "Backspace", backspace: "Backspace",
    left: "ArrowLeft", arrowleft: "ArrowLeft",
    right: "ArrowRight", arrowright: "ArrowRight",
    up: "ArrowUp", arrowup: "ArrowUp",
    down: "ArrowDown", arrowdown: "ArrowDown",
    home: "Home", end: "End", tab: "Tab",
    pageup: "PageUp", pagedown: "PageDown",
  };

  // Parse "Mod+Shift+Z" into {ctrl,alt,shift,meta,key}. Returns null for
  // garbage input. Unknown modifier tokens are a parse error; unknown key
  // names are passed through (single chars are fine, multi-char keys are
  // compared against e.key as-is).
  function parseKey(str) {
    if (typeof str !== "string") return null;
    const parts = str.split("+").map(s => s.trim()).filter(Boolean);
    if (!parts.length) return null;
    const out = { ctrl: false, alt: false, shift: false, meta: false, key: "" };
    for (let i = 0; i < parts.length; i++) {
      const isLast = i === parts.length - 1;
      const raw = parts[i];
      const low = raw.toLowerCase();
      if (!isLast) {
        if (low === "mod") {
          if (IS_MAC) out.meta = true; else out.ctrl = true;
        } else if (low === "ctrl" || low === "control") {
          out.ctrl = true;
        } else if (low === "alt" || low === "option" || low === "opt") {
          out.alt = true;
        } else if (low === "shift") {
          out.shift = true;
        } else if (low === "meta" || low === "cmd" || low === "command" || low === "super" || low === "win") {
          out.meta = true;
        } else {
          return null; // unknown modifier
        }
      } else {
        // Last segment: also accept it as a modifier alias if no key has
        // been set yet (e.g. someone writes "Mod" alone, though we still
        // need a key - that case falls through and out.key stays empty).
        if (low === "mod") { if (IS_MAC) out.meta = true; else out.ctrl = true; }
        else if (low === "ctrl" || low === "control") { out.ctrl = true; }
        else if (low === "alt" || low === "option" || low === "opt") { out.alt = true; }
        else if (low === "shift") { out.shift = true; }
        else if (low === "meta" || low === "cmd" || low === "command" || low === "super" || low === "win") { out.meta = true; }
        else {
          out.key = Object.prototype.hasOwnProperty.call(_KEY_ALIASES, low) ? _KEY_ALIASES[low] : raw;
        }
      }
    }
    if (!out.key) return null;
    return out;
  }

  // Reduce a parsed binding to a single canonical string. Used as the
  // index key in the matcher map; same function turns a live KeyboardEvent
  // into something we can look up.
  //
  // Shift handling: for a bare single-char key (no other modifiers) Shift
  // is dropped because the character itself already encodes shift state on
  // the user's layout - pressing "?" sends e.key="?" with shiftKey=true on
  // a US keyboard, and binding "?" should match. With any other modifier
  // present, Shift becomes meaningful again so that Mod+Z and Mod+Shift+Z
  // (typical undo vs redo) stay distinct.
  function _canonicalize(p) {
    let { ctrl, alt, shift, meta, key } = p;
    if (typeof key === "string" && key.length === 1) {
      key = key.toLowerCase();
      if (!ctrl && !alt && !meta) shift = false;
    }
    const mods = [];
    if (ctrl)  mods.push("ctrl");
    if (alt)   mods.push("alt");
    if (meta)  mods.push("meta");
    if (shift) mods.push("shift");
    return mods.length ? mods.join("+") + "+" + key : key;
  }

  function eventToCanonical(e) {
    return _canonicalize({
      ctrl:  !!e.ctrlKey,
      alt:   !!e.altKey,
      shift: !!e.shiftKey,
      meta:  !!e.metaKey,
      key:   e.key || "",
    });
  }

  // Accepts: string | string[] | null. Returns { display: string[], canonical: string[] }
  // - display is the original tokens (deduped on canonical) for help-dialog
  // rendering and get()/all() round-trips; canonical is what the matcher
  // indexes. Invalid entries warn and are dropped. null/empty returns
  // { display: [], canonical: [] } so callers can treat "disabled" as
  // "no canonical bindings."
  function normalizeBinding(input) {
    if (input == null) return { display: [], canonical: [] };
    const arr = Array.isArray(input) ? input : [input];
    const display = [];
    const canonical = [];
    const seen = new Set();
    for (const s of arr) {
      if (typeof s !== "string" || !s.trim()) continue;
      const parsed = parseKey(s);
      if (!parsed) {
        console.warn('TzaraCanvas: ignored invalid shortcut binding "' + s + '"');
        continue;
      }
      const c = _canonicalize(parsed);
      if (seen.has(c)) continue;
      seen.add(c);
      display.push(s);
      canonical.push(c);
    }
    return { display, canonical };
  }

  // Built-in action registry. Each entry's handler name resolves to a
  // Canvas method at construction time; handlers return truthy to signal
  // the dispatcher should preventDefault. Defaults are *display* strings -
  // they're parsed/canonicalized when copied into the per-instance store.
  const DEFAULT_SHORTCUTS = [
    { action: "help",           group: "Help",                  defaults: ["?"],                 handlerName: "_doHelp",
      description: "Show this keyboard-shortcut dialog. Press Escape to close." },
    { action: "focusFirst",     group: "Focus navigation",      defaults: ["Home"],              handlerName: "_doFocusFirst",
      description: "Focus the first node in document order." },
    { action: "focusLast",      group: "Focus navigation",      defaults: ["End"],               handlerName: "_doFocusLast",
      description: "Focus the last node in document order." },
    { action: "toggleMoveMode", group: "Focus navigation",      defaults: ["M"],                 handlerName: "_doToggleMoveMode",
      description: "Toggle between Navigate and Move mode when a selection is present. The new mode is announced via the live region." },
    { action: "moveLeft",       group: "Movement / navigation", defaults: ["ArrowLeft"],         handlerName: "_doMoveLeft",
      description: "In Move mode, move selected nodes one grid step left. In Navigate mode, focus the nearest node to the left." },
    { action: "moveRight",      group: "Movement / navigation", defaults: ["ArrowRight"],        handlerName: "_doMoveRight",
      description: "In Move mode, move selected nodes one grid step right. In Navigate mode, focus the nearest node to the right." },
    { action: "moveUp",         group: "Movement / navigation", defaults: ["ArrowUp"],           handlerName: "_doMoveUp",
      description: "In Move mode, move selected nodes one grid step up. In Navigate mode, focus the nearest node above." },
    { action: "moveDown",       group: "Movement / navigation", defaults: ["ArrowDown"],         handlerName: "_doMoveDown",
      description: "In Move mode, move selected nodes one grid step down. In Navigate mode, focus the nearest node below." },
    { action: "nudgeLeft",      group: "Movement / navigation", defaults: ["Shift+ArrowLeft"],   handlerName: "_doNudgeLeft",
      description: "Move selected nodes 1 pixel left (fine adjust)." },
    { action: "nudgeRight",     group: "Movement / navigation", defaults: ["Shift+ArrowRight"],  handlerName: "_doNudgeRight",
      description: "Move selected nodes 1 pixel right (fine adjust)." },
    { action: "nudgeUp",        group: "Movement / navigation", defaults: ["Shift+ArrowUp"],     handlerName: "_doNudgeUp",
      description: "Move selected nodes 1 pixel up (fine adjust)." },
    { action: "nudgeDown",      group: "Movement / navigation", defaults: ["Shift+ArrowDown"],   handlerName: "_doNudgeDown",
      description: "Move selected nodes 1 pixel down (fine adjust)." },
    { action: "selectFocused",  group: "Selection",             defaults: ["Space"],             handlerName: "_doSelectFocused",
      description: "Toggle selection of the focused node, so it can be moved with arrow keys / Move mode." },
    { action: "cancel",         group: "Selection",             defaults: ["Escape"],            handlerName: "_doCancel",
      description: "Clear the current selection. Also cancels an in-progress resize or edge draft." },
    { action: "delete",         group: "Editing",               defaults: ["Delete", "Backspace"], handlerName: "_doDelete",
      description: "Delete the current selection (nodes and/or the selected edge)." },
  ];

  // Help-dialog content template. Group order matches the user's mental
  // model rather than the dispatch order. Items with `action:` pull their
  // key labels from the live shortcut registry at render time. Items
  // without `action:` are mouse interactions - their `keys` array renders
  // verbatim. Host-registered actions append at the end in their declared
  // group (or "Custom").
  const HELP_TOPICS = [
    { group: "Help",
      items: [ { action: "help" } ],
    },
    { group: "Focus navigation",
      items: [
        { keys: ["Tab", "Shift+Tab"], description: "Enter or leave the canvas. One Tab stop lands on the currently-focused node; arrow keys take over from there." },
        { action: "focusFirst" },
        { action: "focusLast" },
        { action: "toggleMoveMode" },
      ],
    },
    { group: "Selection",
      items: [
        { action: "selectFocused",          description: "Toggle the focused node in or out of the selection (keyboard)." },
        { keys: ["Click"],                  description: "Replace the current selection with the clicked node." },
        { keys: ["Shift+Click"],            description: "Toggle a node in or out of the selection." },
        { keys: ["Drag empty space"],       description: "Marquee select (replaces existing selection)." },
        { keys: ["Shift+Drag empty space"], description: "Marquee add/remove (preserves existing selection)." },
        { action: "cancel" },
      ],
    },
    { group: "Moving selected nodes",
      items: [
        { action: "moveLeft", description: "Move selection one grid step (or shift focus in Navigate mode)." },
        { action: "moveRight" }, { action: "moveUp" }, { action: "moveDown" },
        { action: "nudgeLeft" }, { action: "nudgeRight" }, { action: "nudgeUp" }, { action: "nudgeDown" },
        { keys: ["Alt+Click+Drag"], description: "Drag a node without snapping to the grid." },
      ],
    },
    { group: "Editing",
      items: [
        { keys: ["Double-click"], description: "Begin editing a text node's content." },
        { keys: ["Enter"],        description: "Inside edit: commit changes." },
        { keys: ["Escape"],       description: "Inside edit: cancel changes." },
        { action: "delete" },
      ],
    },
    { group: "Links",
      items: [
        { keys: ["Ctrl+Click", "Cmd+Click"], description: "Follow a link node's URL without engaging the drag-to-select threshold." },
      ],
    },
  ];

  // Resolve an easing argument to a (t)=>n function. Accepts:
  //   - A function - returned as-is.
  //   - A semantic preset name: 'linear', 'smooth', 'snappy', 'glide'.
  //   - A CSS-style alias: 'ease-in', 'ease-out', 'ease-in-out'.
  // Falls back to EASINGS.smooth for unknown values (matches the prior default).
  function resolveEasing(easing) {
    if (typeof easing === "function") return easing;
    if (typeof easing === "string") {
      if (EASINGS[easing]) return EASINGS[easing];
      switch (easing) {
        case 'ease-in':     return EASINGS.snappy;
        case 'ease-out':    return EASINGS.smooth;
        case 'ease-in-out': return EASINGS.glide;
        case 'ease-out-in': return EASINGS.matrix;
      }
    }
    return EASINGS.smooth;
  }

  // ====================================================================
  // Theme stylesheet, injected into <head> the first time Canvas is built.
  // Scoped to .tzara-canvas-root, which the constructor adds to its root.
  // Multiple Canvas instances on one page share this single <style> element.
  // ====================================================================
  const _tzaraCanvasStyles = String.raw`

      /*
         Theme tokens scoped to each Canvas instance. The Canvas constructor
         adds .tzara-canvas-root to the element passed into new Canvas(...),
         so all these variables inherit to its subtree. Host pages can 
         override any --tc-* var by setting it higher in the tree, and 
         multiple Canvas instances on one page theme independently. 
      */

      .tzara-canvas-root {
          & .canvas-node {
            /* Structural border defaults live in CSS so that themes which
               override border longhands (borderStyle, borderWidth, borderLeft,
               etc.) via node.style({...}) can be cleared via style(null) /
               clearTheme without leaving border-style: none behind. Clearing
               an inline longhand falls back to the cascade, so we need a
               matching rule here. Color stays inline - it's set per node from
               the preset. Cleared longhands are picked up on the next paint;
               no mousemove "fixup" needed. */
            border: 3px solid;
            border-radius: 6px;
          }

          & .grouplabel,
          & .filelabel,
          & .linklabel {

            position: relative;
            left: -0.5rem;
            top: -2.5rem;
            width:auto;
            /* display:inline-block; */
            scale: 1;
            font-size: 1.2em;

            border: 3px solid;
            border-radius: 6px;

            cursor: grab;
            user-select: none;
          }
      }

      .tzara-canvas-root {
        color-scheme: light dark;

        --tc-surface: #ffffff;
        --tc-surface-hover: #eef0f4;
        --tc-surface-active: #e2e8f5;
        --tc-surface-danger-hover: #fde2e2;
        --tc-btn-bg: #f5f6f8;
        --tc-btn-hover: #e9ebf0;

        --tc-border: #c8ccd4;
        --tc-border-muted: #e4e7ed;
        --tc-divider: #d0d4dc;

        --tc-text: #000000;
        --tc-text-on-accent: #ffffff;

        --tc-accent: #2979ff;

        --tc-shadow: rgba(0,0,0,0.18);
        --tc-selection-shadow: rgba(0,0,0,0.53);

        --tc-edge-preview: #2979ff;
        --tc-marquee-fill: #D2BCE5;
        --tc-marquee-stroke: #000000;
        --tc-marquee-line-width: 0px;
        --tc-marquee-line-style: solid;
        --tc-marquee-radius: 10px;
        --tc-marquee-opacity: 1;
        --tc-marquee-glow: none;
        --tc-arrow-fill: #000000;
        --tc-scrollbar-thumb: #888888;
        --tc-scrollbar-thumb-hover: #666666;
        --tc-hitbox-bg: rgba(252,252,255,0.13);
        --tc-guide-stroke: #8a1a52;
        --tc-grid-stroke: rgba(0,0,0,0.08);
        --tc-grid-stroke-major: rgba(0,0,0,0.18);
        --tc-grid-dot: rgba(0,0,0,0.22);
        --tc-grid-dot-major: rgba(0,0,0,0.45);

        /* Node effect colors. Layered fallback: a per-call color from
           node.pulse({color}) / node.highlight({color}) overrides via the
           inline --tc-fx-color; otherwise the effect-specific var below
           applies. Defaults route through --tc-accent so dark mode and
           accent overrides flow through automatically. */
        --tc-pulse-color: var(--tc-accent);
        --tc-highlight-color: var(--tc-accent);
        --tc-flash-bg: var(--tc-accent);
      }

      @media (prefers-color-scheme: dark) {
        .tzara-canvas-root {
          --tc-surface: #1e2024;
          --tc-surface-hover: #2d3138;
          --tc-surface-active: #32394a;
          --tc-surface-danger-hover: #4a2428;
          --tc-btn-bg: #2a2e33;
          --tc-btn-hover: #363a41;

          --tc-border: #3a3f47;
          --tc-border-muted: #2d3138;
          --tc-divider: #3a3f47;

          --tc-text: #e6e8ec;
          --tc-text-on-accent: #ffffff;

          --tc-accent: #6ea8ff;

          --tc-shadow: rgba(255, 255, 255, 0.5);
          --tc-selection-shadow: rgba(93, 94, 104, 0.7);

          --tc-edge-preview: #6ea8ff;
          --tc-marquee-fill: rgb(70, 59, 93);
          //--tc-marquee-fill: rgba(123,93,181,0.35);
          --tc-marquee-stroke: #c0c4ca;
          --tc-marquee-glow: none;
          --tc-arrow-fill: #e6e8ec;
          --tc-scrollbar-thumb: #5a606a;
          --tc-scrollbar-thumb-hover: #7a818c;
          --tc-hitbox-bg: rgba(255,255,255,0.03);
          --tc-guide-stroke: #079500;
          --tc-grid-stroke: rgba(255,255,255,0.06);
          --tc-grid-stroke-major: rgba(255,255,255,0.14);
          --tc-grid-dot: rgba(255,255,255,0.18);
          --tc-grid-dot-major: rgba(255,255,255,0.38);
        }
      }



      /* Marquee (rubber-band selection). Lives in a dedicated DOM layer
         that sits BELOW group_container so marqueeing across a group does
         not paint over the group body. CSS-var-driven defaults; per-canvas
         overrides are written as inline styles via canvas.marqueeStyle(). */
      .tzara-canvas-root .canvas-marquee {
        position: absolute;
        pointer-events: none;
        box-sizing: border-box;
        background: var(--tc-marquee-fill);
        border-width: var(--tc-marquee-line-width);
        border-style: var(--tc-marquee-line-style);
        border-color: var(--tc-marquee-stroke);
        border-radius: var(--tc-marquee-radius);
        opacity: var(--tc-marquee-opacity);
        box-shadow: var(--tc-marquee-glow);
        display: none;
      }

      .tzara-canvas-root .tc-toolbar-hidden { display: none !important; }
      /* Global (always-on) toolbar. Distinct from .tc-toolbar (floating) so
         its default display is 'flex' rather than 'none'. */
      .tzara-canvas-root .tc-toolbar-canvas {
        position: absolute;
        display: flex;
        flex-direction: column;
        padding: 4px;
        background: var(--tc-surface);
        color: var(--tc-text);
        border: 1px solid var(--tc-border);
        border-radius: 8px;
        font-family: system-ui, sans-serif;
        font-size: 12px;
        pointer-events: auto;
        user-select: none;
        z-index: 1001;
      }
      /* Corner placement modifiers - toggled by CanvasToolbarController.setPosition. */
      .tzara-canvas-root .tc-toolbar-pos-tl { top: 20px;    left: 20px;  }
      .tzara-canvas-root .tc-toolbar-pos-tr { top: 20px;    right: 20px; }
      .tzara-canvas-root .tc-toolbar-pos-bl { bottom: 20px; left: 20px;  }
      .tzara-canvas-root .tc-toolbar-pos-br { bottom: 20px; right: 20px; }
      /* Vertical orientation: outer becomes a row so trigger-row + drawer sit
         side-by-side; trigger-row stacks its buttons; drawer flips its border
         from top to side and opens with column flow. row-reverse gives a
         right-anchor toolbar a left-opening drawer (toward canvas interior). */
      .tzara-canvas-root .tc-toolbar-vertical { flex-direction: row; }
      .tzara-canvas-root .tc-toolbar-vertical.tc-toolbar-anchor-left { flex-direction: row-reverse; }
      .tzara-canvas-root .tc-toolbar-vertical .tc-trigger-row { flex-direction: column; }
      .tzara-canvas-root .tc-toolbar-vertical .tc-drawer {
        margin-top: 0;
        border-top: none;
        margin-left: 4px;
        padding: 2px 2px 2px 6px;
        border-left: 1px solid var(--tc-border-muted);
      }
      .tzara-canvas-root .tc-toolbar-vertical.tc-toolbar-anchor-left .tc-drawer {
        margin-left: 0;
        border-left: none;
        margin-right: 4px;
        padding: 2px 6px 2px 2px;
        border-right: 1px solid var(--tc-border-muted);
      }
      .tzara-canvas-root .tc-toolbar-vertical .tc-drawer.open { flex-direction: column; }
      .tzara-canvas-root .tc-toolbar {
        position: absolute;
        display: none;
        flex-direction: column;
        gap: 0;
        padding: 4px;
        background: var(--tc-surface);
        color: var(--tc-text);
        border: 1px solid var(--tc-border);
        border-radius: 8px;
        box-shadow: 0 2px 6px var(--tc-shadow);
        font-family: system-ui, sans-serif;
        font-size: 12px;
        pointer-events: auto;
        white-space: nowrap;
        user-select: none;
      }
      .tzara-canvas-root .tc-trigger-row {
        display: flex;
        align-items: center;
        gap: 2px;
      }
      .tzara-canvas-root .tc-trigger {
        width: 28px;
        height: 28px;
        border: none;
        border-radius: 6px;
        background: transparent;
        color: inherit;
        cursor: pointer;
        font-size: 16px;
        line-height: 1;
        padding: 0;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }
      .tzara-canvas-root .tc-trigger:hover { background: var(--tc-surface-hover); }
      .tzara-canvas-root .tc-trigger.active { background: var(--tc-surface-active); }
      .tzara-canvas-root .tc-trigger-delete:hover { background: var(--tc-surface-danger-hover); }
      /* Disabled state - works uniformly for emoji, SVG icons, and image
         URLs by desaturating + fading the trigger contents. */
      .tzara-canvas-root .tc-trigger:disabled {
        filter: grayscale(1);
        opacity: 0.4;
        cursor: not-allowed;
      }
      .tzara-canvas-root .tc-trigger:disabled:hover { background: transparent; }
      .tzara-canvas-root .tc-drawer {
        display: none;
        align-items: center;
        gap: 6px;
        padding: 6px 2px 2px 2px;
        margin-top: 4px;
        border-top: 1px solid var(--tc-border-muted);
      }
      .tzara-canvas-root .tc-drawer.open { display: flex; }
      .tzara-canvas-root .tc-panel { display: none; align-items: center; gap: 6px; }
      .tzara-canvas-root .tc-panel.active { display: flex; }
      .tzara-canvas-root .tc-swatch {
        width: 18px;
        height: 18px;
        border-radius: 50%;
        border: 2px solid;
        cursor: pointer;
        box-sizing: border-box;
      }
      .tzara-canvas-root .tc-swatch.selected {
        outline: 2px solid var(--tc-accent);
        outline-offset: 1px;
      }
      .tzara-canvas-root .tc-sep {
        width: 1px;
        align-self: stretch;
        background: var(--tc-divider);
        margin: 0 2px;
      }
      .tzara-canvas-root .tc-hex {
        width: 72px;
        padding: 2px 4px;
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        background: var(--tc-surface);
        color: var(--tc-text);
        font: inherit;
      }
      .tzara-canvas-root .tc-bg-row {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .tzara-canvas-root .tc-bg-style-btn.active {
        background: var(--tc-accent);
        color: var(--tc-text-on-accent);
        border-color: var(--tc-accent);
      }
      .tzara-canvas-root .tc-label-input {
        width: 120px;
        padding: 2px 4px;
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        background: var(--tc-surface);
        color: var(--tc-text);
        font: inherit;
      }
      .tzara-canvas-root .tc-btn {
        min-width: 24px;
        height: 22px;
        padding: 0 6px;
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        background: var(--tc-btn-bg);
        color: var(--tc-text);
        cursor: pointer;
        font: inherit;
        line-height: 1;
      }
      .tzara-canvas-root .tc-btn:hover { background: var(--tc-btn-hover); }
      .tzara-canvas-root .tc-btn.active {
        background: var(--tc-accent);
        color: var(--tc-text-on-accent);
        border-color: var(--tc-accent);
      }

      .tzara-canvas-root .tc-file-picker {
        position: absolute;
        top: 52px;
        left: 20px;
        min-width: 280px;
        max-width: 420px;
        background: var(--tc-btn-bg);
        border: 1px solid var(--tc-border);
        border-radius: 8px;
        padding: 8px;
        display: none;
        flex-direction: column;
        gap: 6px;
        z-index: 1002;
        box-shadow: 0 4px 16px rgba(0,0,0,0.15);
      }
      .tzara-canvas-root .tc-file-picker.open { display: flex; }
      .tzara-canvas-root .tc-file-picker-input {
        background: var(--tc-btn-bg);
        color: var(--tc-text);
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        padding: 6px 8px;
        font: inherit;
        outline: none;
      }
      .tzara-canvas-root .tc-file-picker-input:focus {
        border-color: var(--tc-accent, var(--tc-border));
      }
      .tzara-canvas-root .tc-file-picker-list {
        max-height: 240px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
      }
      .tzara-canvas-root .tc-file-picker-row {
        padding: 6px 8px;
        border-radius: 4px;
        cursor: pointer;
        color: var(--tc-text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        flex: 0 0 auto;
      }
      .tzara-canvas-root .tc-file-picker-row:hover { background: var(--tc-surface-hover); }
      .tzara-canvas-root .tc-file-picker-row.active { background: var(--tc-surface-hover); }
      .tzara-canvas-root .tc-file-picker-empty {
        padding: 6px 8px;
        color: var(--tc-text);
        opacity: 0.6;
        font-size: 12px;
      }
      .tzara-canvas-root .tc-file-picker-hint {
        border-top: 1px solid var(--tc-border-muted);
        padding-top: 6px;
        font-size: 11px;
        color: var(--tc-text);
        opacity: 0.6;
      }

      .tzara-canvas-root .tc-settings-panel {
        position: absolute;
        top: 52px;
        left: 20px;
        min-width: 220px;
        background: var(--tc-btn-bg);
        border: 1px solid var(--tc-border);
        border-radius: 8px;
        padding: 10px 12px;
        display: none;
        flex-direction: column;
        gap: 10px;
        z-index: 1002;
        box-shadow: 0 4px 16px rgba(0,0,0,0.15);
        color: var(--tc-text);
        font-size: 13px;
      }
      .tzara-canvas-root .tc-settings-panel.open { display: flex; }
      .tzara-canvas-root .tc-settings-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
      }
      .tzara-canvas-root .tc-select {
        font: inherit;
        color: var(--tc-text);
        background: var(--tc-btn-bg);
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        padding: 2px 6px;
        cursor: pointer;
      }
      .tzara-canvas-root .tc-select:focus {
        outline: none;
        border-color: var(--tc-accent);
      }
      .tzara-canvas-root .tc-switch {
        position: relative;
        width: 34px;
        height: 18px;
        flex: 0 0 auto;
        cursor: pointer;
      }
      .tzara-canvas-root .tc-switch input {
        opacity: 0;
        width: 0;
        height: 0;
      }
      .tzara-canvas-root .tc-switch .tc-slider {
        position: absolute;
        inset: 0;
        background: var(--tc-border);
        border-radius: 18px;
        transition: background 0.15s ease;
      }
      .tzara-canvas-root .tc-switch .tc-slider::before {
        content: "";
        position: absolute;
        top: 2px;
        left: 2px;
        width: 14px;
        height: 14px;
        background: var(--tc-surface);
        border-radius: 50%;
        transition: transform 0.15s ease;
        box-shadow: 0 1px 2px rgba(0,0,0,0.2);
      }
      .tzara-canvas-root .tc-switch input:checked + .tc-slider {
        background: var(--tc-accent);
      }
      .tzara-canvas-root .tc-switch input:checked + .tc-slider::before {
        transform: translateX(16px);
      }

      /* Accessibility primitives .
         .tc-sr-only hides content visually while leaving it in the
         accessibility tree. Used for the canvas heading landmark, the
         connections-summary spans , and the live region . */
      .tzara-canvas-root .tc-sr-only {
        position: absolute !important;
        width: 1px !important;
        height: 1px !important;
        padding: 0 !important;
        margin: -1px !important;
        overflow: hidden !important;
        clip: rect(0, 0, 0, 0) !important;
        white-space: nowrap !important;
        border: 0 !important;
      }
      /* Keyboard focus indicator for nodes. Deliberately distinct from
         the selection outline so focused-but-not-selected is visible. The
         outline uses outline (not box-shadow) so it survives node style
         overrides that touch box-shadow. */
      .tzara-canvas-root .canvas-node:focus-visible {
        outline: 2px dashed var(--tc-accent);
        outline-offset: 2px;
      }

      /* Keyboard-shortcut help dialog . Opens on the help action's
         current binding (default '?'). Renders from HELP_TOPICS plus the
         live shortcut registry so what the dialog shows always matches
         what the canvas actually responds to. Sits above everything else
         in the canvas viewport (z above the toolbar at 1001). The
         backdrop captures clicks for "click outside to close" while
         staying interaction-transparent over the rest of the page. */
      .tzara-canvas-root .tc-help-backdrop {
        position: absolute;
        inset: 0;
        background: rgba(0, 0, 0, 0.4);
        z-index: 2000;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .tzara-canvas-root .tc-help-dialog {
        background: var(--tc-surface);
        color: var(--tc-text);
        border: 1px solid var(--tc-border);
        border-radius: 8px;
        box-shadow: 0 8px 32px var(--tc-shadow);
        padding: 16px 20px;
        max-width: min(680px, 90%);
        max-height: 80%;
        overflow-y: auto;
        font-family: system-ui, sans-serif;
        font-size: 13px;
        line-height: 1.4;
      }
      .tzara-canvas-root .tc-help-dialog header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 12px;
      }
      .tzara-canvas-root .tc-help-dialog h2 {
        margin: 0;
        font-size: 16px;
      }
      .tzara-canvas-root .tc-help-close {
        background: var(--tc-btn-bg);
        color: var(--tc-text);
        border: 1px solid var(--tc-border);
        border-radius: 4px;
        padding: 4px 10px;
        cursor: pointer;
        font: inherit;
      }
      .tzara-canvas-root .tc-help-close:hover { background: var(--tc-btn-hover); }
      .tzara-canvas-root .tc-help-dialog h3 {
        margin: 14px 0 6px;
        font-size: 13px;
        font-weight: 600;
        color: var(--tc-text);
        border-bottom: 1px solid var(--tc-border-muted);
        padding-bottom: 4px;
      }
      .tzara-canvas-root .tc-help-dialog dl {
        margin: 0;
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: 4px 16px;
      }
      .tzara-canvas-root .tc-help-dialog dt {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px;
        white-space: nowrap;
      }
      .tzara-canvas-root .tc-help-dialog kbd {
        display: inline-block;
        background: var(--tc-btn-bg);
        border: 1px solid var(--tc-border);
        border-radius: 3px;
        padding: 1px 5px;
        margin: 0 2px 0 0;
        font: inherit;
        font-size: 11px;
        line-height: 1.4;
      }
      .tzara-canvas-root .tc-help-dialog dd {
        margin: 0;
      }

  `;

  /////////////////////////////////////////////////////////////////////////////////
  // Module-private helpers (color, geometry, html, defaults).
  // Closure-scoped - never reach the global namespace.
  ///////////////////////////////////////////////////////////////////////////////
    function hexToRGB(hexColor) {
      const r = parseInt(hexColor.substring(1, 3), 16);
      const g = parseInt(hexColor.substring(3, 5), 16);
      const b = parseInt(hexColor.substring(5, 7), 16);
      return {r, g, b};
    }



    function rgbToHsv(rgbColor) {
        var r = rgbColor.r / 255, g=rgbColor.g / 255, b=rgbColor.b / 255;
        r = Math.min(1.0, Math.max(0.0, r));
        g = Math.min(1.0, Math.max(0.0, g));
        b = Math.min(1.0, Math.max(0.0, b));
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        const delta = max - min;
        let h, s, v = max;
        if (delta === 0) {
          h = 0;
        } else if (max === r) {
          h = ((g - b) / delta) % 6;
        } else if (max === g) {
          h = (b - r) / delta + 2;
        } else {
          h = (r - g) / delta + 4;
        }

        h = Math.round(h * 60);
        if (h < 0) h += 360;

        s = max === 0 ? 0 : delta / max;
        s = +(s * 100).toFixed(2);
        v = +(v * 100).toFixed(2);

        return { h, s, v };
      }


    function hsvToRgb(hsvColor) {
      var h=hsvColor.h, s=hsvColor.s, v=hsvColor.v;
      s /= 100;
      v /= 100;

      h = Math.min(360, Math.max(0, h));
      s = Math.min(1.0, Math.max(0.0, s));
      v = Math.min(1.0, Math.max(0.0, v));


      const c = v * s;
      const x = c * (1 - Math.abs((h / 60) % 2 - 1));
      const m = v - c;

      let r, g, b;

      if (h >= 0 && h < 60) {
        r = c; g = x; b = 0;
      } else if (h >= 60 && h < 120) {
        r = x; g = c; b = 0;
      } else if (h >= 120 && h < 180) {
        r = 0; g = c; b = x;
      } else if (h >= 180 && h < 240) {
        r = 0; g = x; b = c;
      } else if (h >= 240 && h < 300) {
        r = x; g = 0; b = c;
      } else {
        r = c; g = 0; b = x;
      }

      r = Math.round((r + m) * 255);
      g = Math.round((g + m) * 255);
      b = Math.round((b + m) * 255);

      return { r, g, b };
    }

    function rgbToHex(rgbColor) {
      return '#' + ((1 << 24) + (rgbColor.r << 16) + (rgbColor.g << 8) + rgbColor.b).toString(16).slice(1);
    }

    function hexToHSV(hexColor){
      return rgbToHsv(hexToRGB(hexColor));
    }
    function hsvToHex(hsvColor){
      return rgbToHex(hsvToRgb(hsvColor))
    }


    // Page-level dark-mode flag used by canvasColor's palette derivation.
    // prefers-color-scheme is document-wide, so a single matchMedia is fine
    // even when multiple Canvas instances exist on the page. Per-instance
    // CSS-variable overrides still flow through each Canvas's this._palette.
    const _canvasColorDarkMedia =
      (typeof window !== 'undefined' && window.matchMedia)
        ? window.matchMedia('(prefers-color-scheme: dark)')
        : { matches: false };

    function canvasColor(color = "default") {
      var bgcolor, borderColor, hsv;
      switch (color) {
          case "1":
            bgcolor = "#E8AEB5";
            borderColor = "#e93147"; break;
          case "2":
            bgcolor = "#EABDAF";
            borderColor = "#ec7650"; break;
          case "3":
            bgcolor = "#DDD2AD";
            borderColor = "#e0ac00"; break;
          case "4":
            bgcolor = "#BCD3C4";
            borderColor = "#08b94e"; break;
          case "5":
            bgcolor = "#9BDBDA";
            borderColor = "#00bfbc"; break;
          case "6":
            bgcolor = "#C3B6ED";
            borderColor = "#7852ee"; break;
          case "default":
            bgcolor = "#EEEEEF";
            borderColor = "#666666"; break;
          default:
            hsv = hexToHSV(color)
            hsv.s = hsv.s * 0.5;
            hsv.v = hsv.v * 1.5;
            bgcolor = hsvToHex(hsv);
            borderColor = color;
        }

      // In dark mode, push bg toward a dark, lower-saturation tone so node
      // text stays readable, and keep the border vivid so the preset is
      // still recognizable. Applied uniformly to presets and custom hex.
      if (_canvasColorDarkMedia.matches) {
        var bgHsv = hexToHSV(bgcolor);
        bgHsv.s = bgHsv.s * 0.77;
        bgHsv.v = bgHsv.v * 0.15;
        bgcolor = hsvToHex(bgHsv);
        var bdHsv = hexToHSV(borderColor);
        bdHsv.v = Math.min(100, bdHsv.v * 1.15);
        borderColor = hsvToHex(bdHsv);
      }

      return {bgcolor, borderColor};

    }

    function getBoundingBox(rectangles) {
            let left = Infinity;
            let right = -Infinity;
            let top = Infinity;
            let bottom = -Infinity;

            for (let rectangle of rectangles) {
              if (rectangle.x < left) {
                left = rectangle.x;
              }
              if (rectangle.x + rectangle.width > right) {
                right = rectangle.x + rectangle.width;
              }
              if (rectangle.y < top) {
                top = rectangle.y;
              }
              if (rectangle.y + rectangle.height > bottom) {
                bottom = rectangle.y + rectangle.height;
              }
            }

            return {
              left: left,
              right: right,
              top: top,
              bottom: bottom,
              width: right - left,
              height: bottom - top
            };
          }

    function rectBorderPoint(rect, target) {
          const cx = rect.x + rect.width / 2, cy = rect.y + rect.height / 2;
          const dx = target.x - cx, dy = target.y - cy;
          const absDx = Math.abs(dx), absDy = Math.abs(dy);
          const hw = rect.width / 2, hh = rect.height / 2;
          if (absDx / hw > absDy / hh) return { x: cx + (dx > 0 ? hw : -hw), y: cy + dy * hw / absDx };
          return { x: cx + dx * hh / absDy, y: cy + (dy > 0 ? hh : -hh) };
      }

    function rectSidePoint(rect, side) {
          const cx = rect.x + rect.width / 2, cy = rect.y + rect.height / 2;
          const dx = side === "left" ? -1 : side === "right" ? 1 : 0;
          const dy = side === "top" ? -1 : side === "bottom" ? 1 : 0;
          return { x: cx + dx * rect.width / 2, y: cy + dy * rect.height / 2, dx, dy };
      }

    function drawBezierEdge(ctx, fromRect, fromSide, toRect, toSide, strokeColor) {
          const fromC = { x: fromRect.x + fromRect.width / 2, y: fromRect.y + fromRect.height / 2 };
          const toC   = { x: toRect.x   + toRect.width   / 2, y: toRect.y   + toRect.height   / 2 };
          const start = fromSide ? rectSidePoint(fromRect, fromSide) : { ...rectBorderPoint(fromRect, toC), dx: 0, dy: 0 };
          const end   = toSide   ? rectSidePoint(toRect,   toSide)   : { ...rectBorderPoint(toRect,   fromC), dx: 0, dy: 0 };
          const { ctrl1, ctrl2 } = CanvasEdge._bezierControls(start, end);
          ctx.strokeStyle = strokeColor || "#666666";
          ctx.beginPath();
          ctx.moveTo(start.x, start.y);
          ctx.bezierCurveTo(ctrl1.x, ctrl1.y, ctrl2.x, ctrl2.y, end.x, end.y);
          ctx.stroke();
      }

    function renderCanvasThumbnail(ctx, canvasJson, bufW, bufH) {
          const nodes = canvasJson.nodes || [];
          const edges = canvasJson.edges || [];
          if (!nodes.length) return;
          const bbox = getBoundingBox(nodes);
          if (!isFinite(bbox.width) || !isFinite(bbox.height) || bbox.width <= 0 || bbox.height <= 0) return;

          const pad = 0.95;
          const s = Math.min(bufW / bbox.width, bufH / bbox.height) * pad;
          const tx = (bufW - bbox.width * s) / 2 - bbox.left * s;
          const ty = (bufH - bbox.height * s) / 2 - bbox.top * s;

          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, bufW, bufH);
          ctx.setTransform(s, 0, 0, s, tx, ty);
          ctx.lineWidth = 2 / s;

          const r = 6;
          const byId = {};
          for (const n of nodes) byId[n.id] = n;

          for (const n of nodes) {
            const colors = canvasColor(n.color ?? "default");
            const isGroup = n.type === "group";
            ctx.fillStyle = isGroup ? colors.bgcolor + "66" : colors.bgcolor;
            ctx.strokeStyle = colors.borderColor;
            const x = n.x, y = n.y, w = n.width, h = n.height;
            ctx.beginPath();
            if (ctx.roundRect) {
              ctx.roundRect(x, y, w, h, r);
            } else {
              ctx.rect(x, y, w, h);
            }
            ctx.fill();
            ctx.stroke();
          }

          for (const e of edges) {
            const fromN = byId[e.fromNode], toN = byId[e.toNode];
            if (!fromN || !toN) continue;
            const edgeColors = canvasColor(e.color ?? "default");
            drawBezierEdge(ctx, fromN, e.fromSide, toN, e.toSide, edgeColors.borderColor);
          }
      }

      //////////////////////////////////////////////////////////////////////////////
      // file-node helpers
      ////////////////////////////////////////////////////////////////////////////

      function defaultResolveFile(path) { return path; }

      // Shared <style> nodes the library writes into <head>. They are document-
      // global (injected once, keyed by id) and shared across every live Canvas
      // instance, so they're refcounted: the last destroy() reclaims them.
      const SHARED_STYLE_IDS = [
        "tzara-canvas-styles",
        "tzara-canvas-node-effects",
        "tzara-canvas-node-styles",
      ];
      let _tzaraLiveInstances = 0;
      function _releaseSharedStyles() {
        for (const id of SHARED_STYLE_IDS) {
          const el = document.getElementById(id);
          if (el && el.parentNode) el.parentNode.removeChild(el);
        }
      }

      function escapeHtml(s) {
        return String(s)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/\x22/g, "&quot;");
      }

      function defaultConvertMarkdown(md) {
        return escapeHtml(md).replace(/\n/g, "<br/>");
      }

      // Gate an href before it lands on an <a>. Only http(s)/mailto are allowed
      // through, so attacker-supplied javascript:/data: urls from .canvas data
      // can't execute when the link card's open affordance is clicked. Returns
      // the url for safe schemes, null otherwise (anchor renders as plain text).
      function safeLinkHref(url) {
        const s = String(url || "").trim();
        if (!s) return null;
        let scheme;
        try { scheme = (new URL(s, "http://_")).protocol; }
        catch (_) { return null; }
        // Relative urls resolve against the dummy base → "http:"; absolute urls
        // keep their own scheme. Reject anything not in the allowlist.
        return (scheme === "http:" || scheme === "https:" || scheme === "mailto:") ? s : null;
      }

      function fileKind(path) {
        const ext = (path || "").split(".").pop().toLowerCase();
        if (["png","jpg","jpeg","gif","webp","svg"].includes(ext)) return "image";
        if (ext === "md") return "markdown";
        if (ext === "canvas") return "canvas";
        if (ext === "pdf") return "link";
        return "link";
      }

      // Single outbound-HTML chokepoint. Any string that is about to cross into
      // a host DOM node's innerHTML passes through here. When the host supplied
      // a `sanitize` option (e.g. DOMPurify), it runs on the markup first; with
      // no sanitizer this is an identity write. The built-in convertMarkdown
      // escapes, so the default path is safe; a host-supplied converter owns its
      // own escaping unless it also supplies `sanitize`.
      function renderUntrustedHTML(el, html, sanitize) {
        el.innerHTML = (typeof sanitize === "function") ? sanitize(html) : html;
      }

      // Inbound data contract (Phase 4). Untrusted .canvas geometry is repaired,
      // not trusted: a node with a default geometry when a field is non-finite,
      // and an upper bound so an absurd width/height can't blow the canvas extent.
      const DEFAULT_NODE_GEOMETRY = { x: 0, y: 0, width: 250, height: 60 };
      const MAX_NODE_EXTENT = 100000;

      // Shared rule predicates so the load-time normalization gate
      // (_normalizeData) and the read-only validate() can't drift on "what we
      // check" vs "what we repair".
      function finiteOr(value, fallback) {
        return Number.isFinite(value) ? value : fallback;
      }
      // Canonical identity of an edge for duplicate detection: same endpoints on
      // the same sides. Inlined historically in validate()/cleanup().
      function edgeKey(e) {
        return e.fromNode + '|' + e.fromSide + '→' + e.toNode + '|' + e.toSide;
      }
      // Order-insensitive structural equality, used by io.reconcile to decide
      // whether a node/edge field (incl. the _extraData passthrough bag, whose
      // key order can differ between a loaded node and fresh incoming data) has
      // actually changed. Handles the JSON-ish values that flow through .canvas
      // data: primitives, arrays, and plain objects.
      function deepEqual(a, b) {
        if (a === b) return true;
        if (a == null || b == null) return a === b;
        if (typeof a !== "object" || typeof b !== "object") return false;
        const aArr = Array.isArray(a), bArr = Array.isArray(b);
        if (aArr !== bArr) return false;
        if (aArr) {
          if (a.length !== b.length) return false;
          for (let i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false;
          return true;
        }
        const ak = Object.keys(a), bk = Object.keys(b);
        if (ak.length !== bk.length) return false;
        for (const k of ak) {
          if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
          if (!deepEqual(a[k], b[k])) return false;
        }
        return true;
      }

      ///////////////////////////////////////////////////////////////////////////////
      const NODE_KNOWN_KEYS = new Set([
        "id","x","y","width","height","type","text","label",
        "file","url","color","background","backgroundStyle",
        "accessibility"
      ]);

      // CanvasNode ////////////////////////////////////////////////////////////////
      /////////////////////////////////////////////////////////////////////////////
      class CanvasNode {
        constructor(parent, node) {
          this.parent = parent;
          this.id = node.id;
          this.x = node.x;
          this.y = node.y;
          this.width = node.width;
          this.height = node.height;
          this.type = node.type;
          this.text = node.text ?? "";
          this.label = node.label;
          this.file = node.file
          this.url = node.url ?? null;

          this._extraData = {};
          for (const k of Object.keys(node)) {
            if (!NODE_KNOWN_KEYS.has(k)) this._extraData[k] = node[k];
          }

          // Accessibility : host-authored a11y data is a top-level
          // `accessibility` key in the .canvas JSON. It's listed in
          // NODE_KNOWN_KEYS so it bypasses the _extraData unknown-key
          // bag and lands directly on this runtime field. toData()
          // writes it back; setAccessibility() updates it. _a11y_hints
          // is resolved lazily on first apply so the host callback
          // sees a fully-constructed node. 
          this._a11y_authored = (node.accessibility && typeof node.accessibility === "object")
            ? { ...node.accessibility }
            : null;
          this._a11y_hints = undefined;

          this.isDown = false;
          this.mousemove_offset = [0, 0];
          var color = node.color ?? "default";
          var colors = parent._resolveColor(color, "node");

          this.color =  color;
          this.backgroundColor = colors.bgcolor;
          this.borderColor = colors.borderColor;
        
          this._dom = document.createElement("div");
          if (node.type == "group") {
            var group_label = document.createElement("span");
            group_label.className = "grouplabel";
            group_label.setAttribute("aria-hidden", "true");
            group_label.textContent = this.label;
              group_label.style.border = "3px solid";
              group_label.style.borderRadius = "6px";
            group_label.style.borderColor = this.borderColor;
            group_label.style.backgroundColor = this.backgroundColor + "66";
              group_label.style.position = "relative";
              group_label.style.left = "0";
              group_label.style.top = "-2rem";
              group_label.style.scale = 1;
              group_label.style.display = "inline-block";

            this.background = node.background ?? null;
            this.backgroundStyle = node.backgroundStyle ?? "cover";
            this._backgroundImageAspect = null;
          }
          this._dom.id = parent._instanceId + "-" + this.id;
          this._dom.className = "canvas-node";
          this._dom.style.position = "absolute";
          this._dom.style.left = this.x + "px";
          this._dom.style.top = this.y + "px";
          this._dom.style.width = this.width + "px";
          this._dom.style.height = this.height + "px";
          this._dom.style.backgroundColor = this.backgroundColor;
          this._dom.style.borderRadius = "6px";
          this._dom.style.border = "3px solid";
          this._dom.style.borderColor = this.borderColor;
          this._dom.style.boxSizing = "border-box";
          this._dom.style.userSelect = "none";
          this._dom.style.display = "block";
          this._dom.style.flexDirection = "column";
          this._dom.style.zIndex = (node.type === "group") ? 10 : 100;
          // Apply canvas-wide style defaults on top of the hardcoded inline
          // styles above. Per-node node.style() overrides are applied later
          // (none exist yet at construction time, but setText/setColor paths
          // will reapply them so they always win).
          this._applyCanvasDefaultStyle();
          if (node.type != "group" && node.type != "file" && node.type != "link") {
          this._dom.style.overflowY = "auto";
          this._dom.style.overflowX = "hidden";
          this._scrollEl = this._dom;
          }


          this._dom.setAttribute("tabindex", "-1");
          this._applyA11y();

          if (node.type === "link") {
            this._dom.innerHTML = "";
          } else if (node.type === "file" && this.file) {
            this._renderFile();
          } else if (node.type === "text" || (node.type !== "group" && this.text)) {
            const inner = document.createElement("div");
            inner.style.padding = "5px";
            this._dom.appendChild(inner);
            this._inner = inner;
            this._html = null;
            this._htmlVersion = 0;
            this._renderMarkdownContent();
          } else {
            this._dom.innerHTML = "";
          }

          // Groups live in a separate layer below the edge <canvas> so edges
          // visually pass over group bodies. Content nodes stay in
          // drawing_container above the canvas.
          if (node.type === "group") {
            this.parent.group_container.appendChild(this._dom);
          } else {
            this.parent.drawing_container.appendChild(this._dom);
          }

          if (node.type == "group") {
            this._dom.style.backgroundColor = this.backgroundColor + "66";
            this._dom.appendChild(group_label);
            this.group_label = group_label;
            this._applyGroupBackground();
          }

          if (node.type === "link") {
            this._renderLink();
          }

          if (node.type === "file" && this.file) {
            const file_label = document.createElement("span");
            file_label.className = "filelabel";
            file_label.setAttribute("aria-hidden", "true");
            file_label.textContent = String(this.file).split("/").pop();
            file_label.style.border = "3px solid";
            file_label.style.borderRadius = "6px";
            file_label.style.borderColor = this.borderColor;
            file_label.style.backgroundColor = this.backgroundColor + "66";
            file_label.style.position = "relative";
            file_label.style.left = "0";
            file_label.style.top = "-2rem";
            file_label.style.scale = 1;
            file_label.style.display = "inline-block";
            this._dom.appendChild(file_label);
            this.file_label = file_label;
          }

          // if the canvas has no Tab target yet, claim it. This
          // covers runtime additions to an empty canvas - the initial
          // wrapper setup in the Canvas constructor already handles
          // bulk-loaded files. Idempotent thanks to the null check.
          if (parent.focusedNode == null && typeof parent.setFocusedNode === "function") {
            parent.setFocusedNode(this, { focus: false });
          }

          // connections-summary span. Lives in canvas._a11yDescriptions
          // (outside _dom) so screen-reader browse mode through node
          // content doesn't trip over it twice; aria-describedby pulls
          // its text on focus. Empty until an edge attaches to this node.
          this._setupA11ySummary();
        }

        // ---- a11y mirror  ----

        _setupA11ySummary() {
          if (!this.parent || !this.parent._a11yDescriptions) return;
          const id = this.parent._instanceId + "-" + this.id + "-sum";
          this._a11ySummarySpan = document.createElement("span");
          this._a11ySummarySpan.id = id;
          this.parent._a11yDescriptions.appendChild(this._a11ySummarySpan);
          this._dom.setAttribute("aria-describedby", id);
        }

        _destroyA11ySummary() {
          if (this._a11ySummarySpan && this._a11ySummarySpan.parentNode) {
            this._a11ySummarySpan.parentNode.removeChild(this._a11ySummarySpan);
          }
          this._a11ySummarySpan = null;
        }

        // Renders this.text (raw markdown) via parent.convertMarkdown into this._inner.
        // Shows the raw text immediately as a placeholder, then swaps in the converted
        // HTML when the (possibly async) callback resolves. Sanitization (the host
        // `sanitize` chokepoint) is applied ONCE here, at cache time, so this._html is
        // always the trusted string: every other reader (notably the _exitEdit
        // cancel/restore path) can inject it raw without re-routing through the seam.
        // Version counter prevents a stale in-flight conversion from clobbering a newer
        // result or the edit buffer.
        _renderMarkdownContent() {
          const inner = this._inner;
          if (!inner) return;
          const version = ++this._htmlVersion;
          this._renderConverted(inner, this.text || "", {
            version,
            getCurrentVersion: () => this._htmlVersion,
            cache: true,          // keep this._html as the trusted edit-restore cache
            skipIfEditing: true,  // don't clobber the live edit buffer
          });
        }

        // Shared "convert markdown → (await) → version-check → sanitize-at-cache →
        // inject" pipeline. Both node-text rendering (_renderMarkdownContent) and the
        // file-markdown branch of _renderFile route through here so the async
        // out-of-order guard and the sanitize-once-at-cache behavior can't drift apart
        // between the two paths. The version guard discards a stale/superseded
        // conversion (a newer render, a torn-down node) before it touches the DOM.
        // opts: { path, version, getCurrentVersion, cache, skipIfEditing }
        //   - path: optional second arg passed to convertMarkdown (file context).
        //   - version/getCurrentVersion: captured counter + a getter for the live one;
        //     the continuation bails when they diverge.
        //   - cache: store the sanitized result on this._html (text path only).
        //   - skipIfEditing: skip the inject while this node is being edited.
        _renderConverted(inner, md, opts = {}) {
          const { path, version, getCurrentVersion, cache = false, skipIfEditing = false } = opts;
          inner.textContent = md;                    // already-escaped raw placeholder
          const result = this.parent.convertMarkdown(md, path);
          Promise.resolve(result).then(html => {
            if (typeof getCurrentVersion === "function" && version !== getCurrentVersion()) return;
            const safe = this.parent.sanitize ? this.parent.sanitize(html) : html;
            if (cache) this._html = safe;            // this._html is the trusted cache (see _exitEdit)
            if (skipIfEditing && this.parent.editing === this) return;
            inner.innerHTML = safe;                  // sanitize already applied at cache time
          });
        }

        _renderFile() {
          // Version guard for the async file paths below (mirrors _htmlVersion for
          // node text): a later setFile()/reconcile/destroy bumps the counter, so an
          // in-flight fetch+convert from a superseded render bails before writing into
          // the (possibly detached) inner element. Self-initializing so both the
          // constructor and setFile entry points are covered without a class field.
          this._fileVersion = (this._fileVersion || 0) + 1;
          const fileVersion = this._fileVersion;
          this._a11yImageEl = null;
          const inner = document.createElement("div");
          inner.className = "canvas-node-scroll";
          inner.style.padding = "10px";
          inner.style.position = "absolute";
          inner.style.top = "0";
          inner.style.left = "0";
          inner.style.right = "0";
          inner.style.bottom = "0";
          inner.style.boxSizing = "border-box";
          inner.style.overflowY = "auto";
          inner.style.overflowX = "hidden";
          this._dom.appendChild(inner);
          this._scrollEl = inner;

          const path = this.file;
          const url = this.parent.resolveFile(path);
          const kind = fileKind(path);
          const filename = String(path).split("/").pop();

          if (kind === "image") {
            const img = document.createElement("img");
            img.addEventListener("load", () => {
              if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                this._imageAspect = img.naturalWidth / img.naturalHeight;
              }
            });
            img.src = url;
            // Wrapping node carries role="img" + aria-label="Image: <name>"
            // via _applyA11y(), so the inner <img> defaults to empty alt
            // to avoid double-announcement. _applyA11y() rewrites alt
            // from the effective accessibility.alt field if a host has
            // authored one - track the element so the override can land.
            this._a11yImageEl = img;
            img.alt = "";
            img.style.maxWidth = "100%";
            img.style.maxHeight = "100%";
            img.style.display = "block";
            inner.style.padding = "0";
            inner.appendChild(img);
            // Re-apply so a host-authored accessibility.alt override
            // lands on the newly-created <img>. The earlier apply in
            // the constructor (or setFile path) ran before _a11yImageEl
            // existed.
            this._applyA11y();
            return;
          }

          if (kind === "canvas") {
            const c = document.createElement("canvas");
            c.style.width = "100%";
            c.style.height = "100%";
            c.style.display = "block";
            c.style.pointerEvents = "none";
            inner.style.padding = "0";
            inner.appendChild(c);
            fetch(url)
              .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
              .then(json => {
                if (fileVersion !== this._fileVersion) return;
                const nodes = json.nodes || [];
                if (!nodes.length) throw new Error("empty canvas");
                const bbox = getBoundingBox(nodes);
                const maxDim = 1024;
                const s = Math.min(maxDim / bbox.width, maxDim / bbox.height);
                c.width = Math.max(1, Math.round(bbox.width * s));
                c.height = Math.max(1, Math.round(bbox.height * s));
                renderCanvasThumbnail(c.getContext("2d"), json, c.width, c.height);
              })
              .catch(() => {
                if (fileVersion !== this._fileVersion) return;
                inner.innerHTML = "";
                inner.style.padding = "10px";
                const a = document.createElement("a");
                a.href = url;
                a.target = "_blank";
                a.rel = "noopener";
                a.textContent = filename;
                inner.appendChild(a);
              });
            return;
          }

          if (kind === "markdown") {
            inner.textContent = filename;
            fetch(url)
              .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
              .then(md => {
                if (fileVersion !== this._fileVersion) return;
                this._renderConverted(inner, md, {
                  path,
                  version: fileVersion,
                  getCurrentVersion: () => this._fileVersion,
                });
              })
              .catch(() => {
                if (fileVersion !== this._fileVersion) return;
                inner.innerHTML = "";
                const a = document.createElement("a");
                a.href = url;
                a.target = "_blank";
                a.rel = "noopener";
                a.textContent = filename;
                inner.appendChild(a);
              });
            return;
          }

          // link fallback (.canvas, .pdf, unknown)
          const a = document.createElement("a");
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener";
          a.textContent = filename;
          inner.appendChild(a);
        }

        _applyGroupBackground() {
          if (this.type !== "group") return;
          const dom = this._dom;
          if (!this.background) {
            dom.style.backgroundImage = "";
            dom.style.backgroundSize = "";
            dom.style.backgroundRepeat = "";
            dom.style.backgroundPosition = "";
            this._backgroundImageAspect = null;
            return;
          }
          const url = this.parent.resolveFile(this.background);
          dom.style.backgroundImage = `url("${url}")`;
          switch (this.backgroundStyle) {
            case "repeat":
              dom.style.backgroundSize = "auto";
              dom.style.backgroundRepeat = "repeat";
              dom.style.backgroundPosition = "";
              break;
            case "ratio":
              dom.style.backgroundSize = "contain";
              dom.style.backgroundRepeat = "no-repeat";
              dom.style.backgroundPosition = "center";
              this._loadBackgroundAspect(url);
              break;
            case "cover":
            default:
              dom.style.backgroundSize = "cover";
              dom.style.backgroundRepeat = "no-repeat";
              dom.style.backgroundPosition = "center";
              break;
          }
        }

        _loadBackgroundAspect(url) {
          const probe = new Image();
          probe.addEventListener("load", () => {
            if (probe.naturalWidth > 0 && probe.naturalHeight > 0) {
              this._backgroundImageAspect = probe.naturalWidth / probe.naturalHeight;
            }
          });
          probe.src = url;
        }

        // Embed gate (2.2): decide what URL, if any, this link node may load
        // into a live <iframe>. resolveEmbed (host vetting/rewrite) wins; else
        // allowEmbeds embeds the raw url; default is null (render inert card).
        _resolveEmbedUrl() {
          const c = this.parent;
          const url = this.url || "";
          if (!url) return null;
          if (typeof c.resolveEmbed === "function") {
            try {
              const r = c.resolveEmbed(url);
              return (typeof r === "string" && r) ? r : null;
            } catch (e) {
              console.error("resolveEmbed callback threw:", e);
              return null;
            }
          }
          return c.allowEmbeds ? url : null;
        }

        _renderLink() {
          // Floating URL label (mirrors group_label) - sole drag/select handle.
          const label = document.createElement("span");
          label.className = "linklabel";
          label.setAttribute("aria-hidden", "true");
          label.textContent = this.url || "(no url)";
          label.title = this.url || "";
          label.style.borderColor = this.borderColor;
          label.style.backgroundColor = this.backgroundColor + "66";
          label.style.position = "relative";
          label.style.left = "0";
          label.style.top = "-2rem";
          label.style.scale = 1;
          label.style.maxWidth = "calc(100% - 1rem)";
          label.style.display = "inline-block";
          label.style.overflow = "hidden";
          label.style.textOverflow = "ellipsis";
          label.style.whiteSpace = "nowrap";
          // Stays auto so the listener fires even when the iframe is live.
          label.style.zIndex = "2";
          label.style.pointerEvents = "auto";

          label.addEventListener("pointerdown", (e) => {
            this.parent.hitbox_container.style.pointerEvents = "";
            for (const m of this.parent._nodes) {
              if (m.type === "link") m._setIframeInteractive(false);
            }
            this.parent.event_mousedown(e);
          });

          this._dom.appendChild(label);
          this.link_label = label;
          this._renderLinkBody();
        }

        // Builds (or rebuilds) the link node's body - a live sandboxed iframe
        // when the embed gate allows it, otherwise an inert clickable card.
        // Idempotent: tears down any prior body so setUrl can flip modes when
        // the embed decision changes.
        _renderLinkBody() {
          if (this._iframeWrap) { this._iframeWrap.remove(); this._iframeWrap = null; this._iframeEl = null; }
          if (this._linkCard)   { this._linkCard.remove();   this._linkCard = null; }

          const embedUrl = this._resolveEmbedUrl();
          this._embedUrl = embedUrl;

          if (embedUrl) {
            // Iframe wrap fills the entire bbox.
            const wrap = document.createElement("div");
            wrap.style.position = "absolute";
            wrap.style.top = "0";
            wrap.style.left = "0";
            wrap.style.right = "0";
            wrap.style.bottom = "0";
            wrap.style.overflow = "hidden";

            const iframe = document.createElement("iframe");
            iframe.style.width = "100%";
            iframe.style.height = "100%";
            iframe.style.border = "0";
            iframe.style.display = "block";
            iframe.style.pointerEvents = "none";
            iframe.setAttribute("sandbox", "allow-scripts allow-forms");
            iframe.setAttribute("allow", "camera 'none'; microphone 'none'; geolocation 'none'");
            iframe.referrerPolicy = "no-referrer-when-downgrade";
            iframe.loading = "lazy";
            iframe.src = embedUrl;
            wrap.appendChild(iframe);

            wrap.addEventListener("mouseleave", () => {
              // Cursor leaving the iframe area: cached mouse coords are stale
              // (no canvas mousemove fired while iframe was live). Unconditionally
              // suppress this iframe and restore the hitbox layer so the next
              // canvas mousemove re-evaluates against fresh coordinates.
              this._setIframeInteractive(false);
              const p = this.parent;
              const anyOn = p._nodes.some(m => m.type === "link" && m._iframeEl?.style.pointerEvents === "auto");
              p.hitbox_container.style.pointerEvents = anyOn ? "none" : "";
            });

            this._dom.insertBefore(wrap, this.link_label);
            this._iframeWrap = wrap;
            this._iframeEl = iframe;
          } else {
            // Inert card: shows the url with a clickable open affordance. The
            // card itself stays pointer-events:none so the node remains
            // draggable; only the open anchor is interactive.
            const card = document.createElement("div");
            card.className = "tc-linkcard";
            card.style.position = "absolute";
            card.style.inset = "0";
            card.style.display = "flex";
            card.style.flexDirection = "column";
            card.style.alignItems = "center";
            card.style.justifyContent = "center";
            card.style.gap = "0.4rem";
            card.style.padding = "0.5rem";
            card.style.boxSizing = "border-box";
            card.style.overflow = "hidden";
            card.style.textAlign = "center";
            card.style.pointerEvents = "none";

            const a = document.createElement("a");
            a.className = "tc-linkcard-open";
            a.textContent = this.url || "(no url)";
            a.title = this.url || "";
            a.style.pointerEvents = "auto";
            a.style.wordBreak = "break-all";
            a.style.maxHeight = "100%";
            a.style.overflow = "hidden";
            const href = safeLinkHref(this.url);
            if (href) {
              a.href = href;
              a.target = "_blank";
              a.rel = "noopener noreferrer";
            }
            card.appendChild(a);

            this._dom.insertBefore(card, this.link_label);
            this._linkCard = card;
          }
        }

        _setIframeInteractive(on) {
          if (!this._iframeEl) return;
          this._iframeEl.style.pointerEvents = on ? "auto" : "none";
        }

        setUrl(url) {
          const next = url || "";
          if (next === this.url) return;
          const prev = this.url;
          this.url = next;
          // Rebuild the body rather than just re-pointing an iframe src: the new
          // url may flip the embed decision (e.g. resolveEmbed vetoes it), so the
          // node must be able to switch between iframe and card.
          if (this.type === "link" && this.link_label) {
            this._renderLinkBody();
          }
          if (this.link_label) {
            this.link_label.textContent = this.url || "(no url)";
            this.link_label.title = this.url || "";
          }
          this._applyA11y();
          this.parent._emit('nodeUpdate', { node: this, kind: 'url', url: next, prevUrl: prev });
        }

        _moveBy(dx, dy) {
          this.x = this.x + dx;
          this.y = this.y + dy;
          this._dom.style.left = this.x + "px";
          this._dom.style.top = this.y + "px";
          this._refreshAttached();
        }

        _positionAt(x,y) {
          this.x = x;
          this.y = y;
          this._dom.style.left = x + "px";
          this._dom.style.top = y + "px";
          this._refreshAttached();
        }

        _sizeAt(w, h) {
          this.width = w;
          this.height = h;
          this._dom.style.width  = w + "px";
          this._dom.style.height = h + "px";
          this._refreshAttached();
        }

        sideMidpoint(side) {
          const w = this._dom.offsetWidth  || this.width;
          const h = this._dom.offsetHeight || this.height;
          switch (side) {
            case "left":   return { x: this.x,         y: this.y + h / 2, dx: -1, dy:  0 };
            case "right":  return { x: this.x + w,     y: this.y + h / 2, dx:  1, dy:  0 };
            case "top":    return { x: this.x + w / 2, y: this.y,         dx:  0, dy: -1 };
            case "bottom": return { x: this.x + w / 2, y: this.y + h,     dx:  0, dy:  1 };
          }
        }

        _createHandleDom(side, isTarget) {
          // Handles live in the same world-transformed container as nodes,
          // but are NOT children of node._dom (that has overflow:auto which
          // would clip them).
          const el = document.createElement("div");
          el.className = "canvas-connect-handle";
          el.dataset.side = side;
          el.setAttribute("contenteditable", "false");
          el.style.position = "absolute";
          el.style.boxSizing = "border-box";
          // hitbox_container owns all events
          el.style.pointerEvents = "none";
          el.style.display = "none";
          // Visual styling (size, fill, stroke, etc.) is centralized in
          // canvas._applyConnectHandle so canvas.connectHandleStyle({...})
          // can theme every handle uniformly.
          this.parent._applyConnectHandle(el, { active: false });
          this._refreshHandlePosition(el, side);
          this.parent.drawing_container.appendChild(el);
          return el;
        }

        _refreshHandlePosition(el, side) {
          const mid = this.sideMidpoint(side);
          el.style.left = mid.x + "px";
          el.style.top  = mid.y + "px";
        }

        showConnectHandle(side) {
          if (!this._connectHandle) {
            this._connectHandle = this._createHandleDom(side, false);
          }
          this._connectHandleSide = side;
          this._refreshHandlePosition(this._connectHandle, side);
          this._connectHandle.style.display = "block";
          // Re-apply (inactive variant) in case connectHandleStyle changed
          // while this handle was hidden.
          this.parent._applyConnectHandle(this._connectHandle, { active: false });
        }

        hideConnectHandle() {
          if (this._connectHandle) this._connectHandle.style.display = "none";
          this._connectHandleSide = null;
        }

        showTargetHandles(activeSide) {
          if (!this._targetHandles) {
            this._targetHandles = {
              left:   this._createHandleDom("left",   true),
              right:  this._createHandleDom("right",  true),
              top:    this._createHandleDom("top",    true),
              bottom: this._createHandleDom("bottom", true),
            };
          }
          for (const s of ["left","right","top","bottom"]) {
            const h = this._targetHandles[s];
            this._refreshHandlePosition(h, s);
            h.style.display = "block";
            this.parent._applyConnectHandle(h, { active: s === activeSide });
          }
        }

        hideTargetHandles() {
          if (!this._targetHandles) return;
          for (const s of ["left","right","top","bottom"]) {
            this._targetHandles[s].style.display = "none";
          }
        }

        // ============================================================
        // Public Node API: setters, effects, queries
        // ============================================================

        // Derived accessibility defaults for this node, computed from
        // type and current content. Forms the base layer of the
        // precedence stack - host hints and authored data merge on top..
        _deriveA11y() {
          const trunc = (s, n) => {
            if (s == null) return "";
            const t = String(s).replace(/\s+/g, " ").trim();
            return t.length > n ? t.slice(0, n - 1) + "…" : t;
          };
          if (this.type === "group") {
            const gl = trunc(this.label, 60);
            return { role: "group", label: "Group: " + (gl || "unlabeled") };
          }
          if (this.type === "link") {
            const url = this.url || "";
            let host = url;
            try {
              const u = new URL(url);
              host = u.hostname || url;
            } catch (_) { /* not parseable - fall back to raw */ }
            return { role: "link", label: "Link: " + (trunc(host, 60) || "(no url)") };
          }
          if (this.type === "file") {
            const path = this.file || "";
            const basename = String(path).split("/").pop() || "";
            const kind = fileKind(path);
            if (kind === "image") {
              return { role: "img", label: "Image: " + (trunc(basename, 60) || "(no file)") };
            }
            const ext = (basename.split(".").pop() || "").toLowerCase();
            const prefix = ext ? ext.toUpperCase() + " file" : "File";
            return { role: "article", label: prefix + ": " + (trunc(basename, 60) || "(no file)") };
          }
          const text = this.text || "";
          const firstLine = String(text).split(/\r?\n/).find(l => l.trim()) || "";
          return { role: "article", label: "Text node: " + (trunc(firstLine, 60) || "(empty)") };
        }

        // Returns the merged effective a11y data for this node without
        // touching the DOM. Public - backs node.getAccessibility().
        _effectiveA11y() {
          const derived = this._deriveA11y();
          // Lazy-resolve hints on first access so the host callback
          // sees a fully-constructed node.
          if (this._a11y_hints === undefined) {
            const cb = this.parent && this.parent._accessibilityHints;
            if (typeof cb === "function") {
              try {
                const r = cb(this);
                this._a11y_hints = (r && typeof r === "object") ? { ...r } : null;
              } catch (_) {
                this._a11y_hints = null;
              }
            } else {
              this._a11y_hints = null;
            }
          }
          return Object.assign({}, derived, this._a11y_hints || {}, this._a11y_authored || {});
        }

        // Resolve effective a11y and apply to the DOM. Called from
        // construction, from setText/setFile/setUrl/_exitEdit (so the
        // derived layer stays in sync with content), and from
        // setAccessibility() (so the authored layer takes effect).
        // When the effective label changes, propagates the change to
        // each incident edge's <li> text and to neighbors' connections
        // summaries  - those reference this node's label.
        _applyA11y() {
          if (!this._dom) return;
          const eff = this._effectiveA11y();
          const prevLabel = this._lastAppliedA11yLabel;
          this._dom.setAttribute("role", eff.role);
          this._dom.setAttribute("aria-label", eff.label);
          if (eff.roleDescription) this._dom.setAttribute("aria-roledescription", String(eff.roleDescription));
          else this._dom.removeAttribute("aria-roledescription");
          if (eff.lang) this._dom.setAttribute("lang", String(eff.lang));
          else this._dom.removeAttribute("lang");
          if (eff.hidden === true) this._dom.setAttribute("aria-hidden", "true");
          else this._dom.removeAttribute("aria-hidden");
          if (this._a11yImageEl) {
            this._a11yImageEl.alt = (eff.alt != null) ? String(eff.alt) : "";
          }
          this._lastAppliedA11yLabel = eff.label;
          if (prevLabel !== undefined && prevLabel !== eff.label) {
            const c = this.parent;
            if (c && c._edges) {
              for (const e of c._edges) {
                if (e.fromNode !== this.id && e.toNode !== this.id) continue;
                if (typeof e._applyA11y === "function") e._applyA11y();
                const otherId = (e.fromNode === this.id) ? e.toNode : e.fromNode;
                const other = c.getNode(otherId);
                if (other) c._refreshA11yNodeSummary(other);
              }
            }
          }
        }

        // ---- accessibility API  ----
        // Returns the effective (merged) accessibility data for this
        // node - derived defaults overlaid with host hints overlaid
        // with authored data. Read-only snapshot.
        getAccessibility() {
          return this._effectiveA11y();
        }

        // Update authored accessibility fields and reapply. Pass null
        // for an individual field to clear it (falls back to hint/
        // derived). Pass null for the whole partial to clear all
        // authored fields. Marks the canvas dirty so the change
        // round-trips through toData() into the `accessibility` key.
        setAccessibility(partial) {
          if (partial === null) {
            if (!this._a11y_authored) return this;
            this._a11y_authored = null;
          } else if (partial && typeof partial === "object") {
            const next = this._a11y_authored ? { ...this._a11y_authored } : {};
            for (const k of Object.keys(partial)) {
              const v = partial[k];
              if (v == null) delete next[k];
              else next[k] = v;
            }
            this._a11y_authored = (Object.keys(next).length > 0) ? next : null;
          } else {
            return this;
          }
          this._applyA11y();
          // description lives in the aria-describedby span - keep
          // it in sync. Label changes are already propagated by
          // _applyA11y to incident edges and neighbors.
          if (typeof this.parent._refreshA11yNodeSummary === "function") {
            this.parent._refreshA11yNodeSummary(this);
          }
          this.parent._markDirty();
          return this;
        }

        // ---- state setters ----
        setPosition(x, y) {
          if (this.x === x && this.y === y) return this;
          const prevX = this.x, prevY = this.y;
          this._positionAt(x, y);
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('nodeMove',   { node: this, x, y, prevX, prevY });
          this.parent._emit('nodeUpdate', { node: this, kind: 'position', x, y, prevX, prevY });
          return this;
        }

        setSize(w, h) {
          if (this.width === w && this.height === h) return this;
          const prevW = this.width, prevH = this.height;
          this._sizeAt(w, h);
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('nodeResize', { node: this, width: w, height: h, prevWidth: prevW, prevHeight: prevH });
          this.parent._emit('nodeUpdate', { node: this, kind: 'size', width: w, height: h, prevWidth: prevW, prevHeight: prevH });
          return this;
        }

        // Return the current text. While in edit mode, returns the live
        // value from the contenteditable (which may differ from this.text
        // if the user has typed since beginEdit()). Otherwise returns
        // this.text. Symmetric with setText() and safe for round-trips
        // like setText(getText() + " more").
        getText() {
          if (this.parent.editing === this) {
            const inner = this.parent._editInner;
            if (inner) return inner.innerText;
          }
          return this.text;
        }

        setText(text) {
          const next = text == null ? "" : String(text);
          if (next === this.text) return this;
          const prev = this.text;
          this.text = next;
          // If this node is currently in edit mode, push the new value
          // into the contenteditable so the user sees it. _exitEdit(true)
          // will read it back from inner.innerText; _exitEdit(false) will
          // revert to _editOriginalText (captured at _enterEdit time).
          if (this.parent.editing === this) {
            const inner = this.parent._editInner;
            if (inner) {
              inner.textContent = next;
              const range = document.createRange();
              range.selectNodeContents(inner);
              range.collapse(false);
              const sel = window.getSelection();
              if (sel) {
                sel.removeAllRanges();
                sel.addRange(range);
              }
            }
            this._applyA11y();
            this.parent._markDirty();
            this.parent._emit('nodeUpdate', { node: this, kind: 'text', text: next, prevText: prev });
            return this;
          }
          // text-only re-render is supported on text nodes and on file
          // nodes that fall back to markdown content.
          if (this._inner) {
            this._html = null;
            this._renderMarkdownContent();
          }
          this._applyA11y();
          this.parent._markDirty();
          this.parent._emit('nodeUpdate', { node: this, kind: 'text', text: next, prevText: prev });
          return this;
        }

        setColor(key) {
          const next = key == null ? "default" : key;
          if (next === this.color) return this;
          const prev = this.color;
          this.color = next;
          const colors = this.parent._resolveColor(next, "node");
          this.backgroundColor = colors.bgcolor;
          this.borderColor     = colors.borderColor;
          // Groups use a translucent fill ("+66" alpha) like the constructor.
          if (this.type === "group") {
            this._dom.style.backgroundColor = this.backgroundColor + "66";
            if (this.group_label) {
              this.group_label.style.backgroundColor = this.backgroundColor + "66";
              this.group_label.style.borderColor     = this.borderColor;
            }
          } else {
            this._dom.style.backgroundColor = this.backgroundColor;
          }
          this._dom.style.borderColor = this.borderColor;
          if (this.file_label) {
            this.file_label.style.backgroundColor = this.backgroundColor + "66";
            this.file_label.style.borderColor     = this.borderColor;
          }
          if (this.link_label) {
            this.link_label.style.borderColor = this.borderColor;
          }
          // Re-apply canvas defaults after preset, so per-node overrides
          // (applied next) layer on top of them.
          this._applyCanvasDefaultStyle();
          this._applyStyleOverrides();
          // Aux label preset colors were just re-painted above; restore any
          // per-node label overrides so they continue to win.
          this._applyLabelStyleOverrides();
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('nodeUpdate', { node: this, kind: 'color', color: next, prevColor: prev });
          return this;
        }

        // Apply consumer style overrides on top of the color preset. Pass an
        // object of camelCase CSS properties (e.g. {borderColor, backgroundColor,
        // boxShadow, borderWidth, opacity, color}). Any valid HTMLElement.style
        // key is accepted. Overrides persist across setColor() calls.
        //   node.style({ borderColor: '#ef4444' })  - set one or more overrides
        //   node.style({ borderColor: null })       - clear one override (revert to preset)
        //   node.style(null)                        - clear all overrides
        //   node.style()                            - returns a copy of current overrides
        //
        // Pass a `label: { ... }` sub-object to style the node's auxiliary
        // label element (group_label / file_label / link_label). Text nodes
        // have no aux label, so the sub-object is silently ignored.
        //   node.style({ label: { fontWeight: 700, background: 'transparent' }})
        //   node.style({ label: { borderColor: null }})  - clear one
        //   node.style({ label: null })                  - clear all label overrides
        style(overrides) {
          if (arguments.length === 0) {
            const main = this._styleOverrides ? Object.assign({}, this._styleOverrides) : {};
            if (this._labelStyleOverrides) main.label = Object.assign({}, this._labelStyleOverrides);
            return main;
          }
          if (overrides === null) {
            if (this._styleOverrides) {
              for (const k of Object.keys(this._styleOverrides)) this._dom.style[k] = '';
              this._styleOverrides = null;
            }
            // Re-paint preset over the now-cleared properties.
            if (this.type === "group") {
              this._dom.style.backgroundColor = this.backgroundColor + "66";
            } else {
              this._dom.style.backgroundColor = this.backgroundColor;
            }
            this._dom.style.borderColor = this.borderColor;
            // Canvas-wide defaults must outlive a per-node clear.
            this._applyCanvasDefaultStyle();
            // Clearing all also clears the label sub-overrides.
            this._setLabelOverrides(null);
            return this;
          }
          if (typeof overrides !== 'object') return this;

          // Peel off label sub-object first; it's handled by its own helper
          // so the main loop only sees node-level (camelCase CSS) keys.
          if ('label' in overrides) {
            this._setLabelOverrides(overrides.label);
          }

          if (!this._styleOverrides) this._styleOverrides = {};
          for (const k of Object.keys(overrides)) {
            if (k === 'label') continue;
            const v = overrides[k];
            if (v == null) {
              delete this._styleOverrides[k];
              this._dom.style[k] = '';
              // Re-apply preset for color keys we just cleared.
              if (k === 'backgroundColor') {
                this._dom.style.backgroundColor = this.type === "group"
                  ? this.backgroundColor + "66"
                  : this.backgroundColor;
              } else if (k === 'borderColor') {
                this._dom.style.borderColor = this.borderColor;
              }
              // If a canvas default exists for this key, restore it after
              // the wipe so canvas-wide defaults survive a per-node clear.
              // Object form first, then fn form on top - same layering
              // defaultNodeStyle uses. The fn-keys tracking Map already
              // contains this key from when the fn first applied it, so
              // no extra bookkeeping needed here.
              const d = this.parent && this.parent._defaultNodeStyle;
              if (d && d[k] != null) this._dom.style[k] = d[k];
              const fn = this.parent && this.parent._defaultNodeStyleFn;
              if (fn) {
                let fnRes = null;
                try { fnRes = fn(this); } catch (_) { fnRes = null; }
                if (fnRes && typeof fnRes === 'object' && fnRes[k] != null) {
                  this._dom.style[k] = fnRes[k];
                }
              }
            } else {
              this._styleOverrides[k] = v;
              this._dom.style[k] = v;
            }
          }
          // Mirrors edge.style(): drop the empty bag so state stays
          // introspectable (no lingering {} when all keys were cleared
          // or only label-sub-keys were passed).
          if (this._styleOverrides && Object.keys(this._styleOverrides).length === 0) {
            this._styleOverrides = null;
          }
          return this;
        }

        // The auxiliary label element on this node, if any: group_label
        // for group nodes, file_label for files, link_label for links.
        // Text nodes return null.
        _auxLabelEl() {
          return this.group_label || this.file_label || this.link_label || null;
        }

        // Internal: per-node aux-label style override storage and apply.
        // Mirrors the main style() semantics but writes to the aux label
        // element instead of _dom. Preset-bound keys (backgroundColor,
        // borderColor) are restored from the node's preset when cleared,
        // matching how the main path handles those keys on _dom.
        _setLabelOverrides(sub) {
          const el = this._auxLabelEl();
          if (sub === null) {
            if (this._labelStyleOverrides && el) {
              for (const k of Object.keys(this._labelStyleOverrides)) el.style[k] = '';
              // Re-paint preset on label.
              el.style.backgroundColor = this.backgroundColor + '66';
              el.style.borderColor     = this.borderColor;
            }
            this._labelStyleOverrides = null;
            return;
          }
          if (typeof sub !== 'object') return;
          if (!el) return; // no aux label on this node type
          if (!this._labelStyleOverrides) this._labelStyleOverrides = {};
          for (const k of Object.keys(sub)) {
            const v = sub[k];
            if (v == null) {
              delete this._labelStyleOverrides[k];
              el.style[k] = '';
              if (k === 'backgroundColor') el.style.backgroundColor = this.backgroundColor + '66';
              else if (k === 'borderColor') el.style.borderColor = this.borderColor;
            } else {
              this._labelStyleOverrides[k] = v;
              el.style[k] = v;
            }
          }
          if (Object.keys(this._labelStyleOverrides).length === 0) {
            this._labelStyleOverrides = null;
          }
        }

        // Internal: re-apply current style overrides over preset-driven styles.
        // Called after setColor() so consumer overrides win.
        _applyStyleOverrides() {
          if (!this._styleOverrides) return;
          for (const k of Object.keys(this._styleOverrides)) {
            this._dom.style[k] = this._styleOverrides[k];
          }
        }

        // Internal: re-apply current label style overrides over preset-driven
        // label styles. Called after setColor() so label overrides survive
        // the preset re-paint on group_label / file_label / link_label.
        _applyLabelStyleOverrides() {
          if (!this._labelStyleOverrides) return;
          const el = this._auxLabelEl();
          if (!el) return;
          for (const k of Object.keys(this._labelStyleOverrides)) {
            el.style[k] = this._labelStyleOverrides[k];
          }
        }

        // Internal: apply canvas-wide default style overrides to this node.
        // Called from the constructor and from setColor() / style() clear
        // paths. Sits between preset colors and per-node _styleOverrides in
        // the layering order, so callers must apply _styleOverrides after
        // this when both need to be reasserted.
        //
        // Also snapshots the pre-default inline value of each defaulted key,
        // the first time we touch it on this node. The snapshot is what
        // defaultNodeStyle(null) restores to, so nodes constructed *after*
        // defaultNodeStyle was set get their constructor baseline preserved
        // through later clears - same contract existing nodes have.
        _applyCanvasDefaultStyle() {
          const c = this.parent;
          if (!c || (!c._defaultNodeStyle && !c._defaultNodeStyleFn)) return;
          if (!c._defaultNodeStyleSnapshot) c._defaultNodeStyleSnapshot = new Map();
          let snap = c._defaultNodeStyleSnapshot.get(this);
          if (!snap) {
            snap = {};
            c._defaultNodeStyleSnapshot.set(this, snap);
          }
          // 1) Static defaults (object form).
          if (c._defaultNodeStyle) {
            const d = c._defaultNodeStyle;
            for (const k of Object.keys(d)) {
              if (!(k in snap)) snap[k] = this._dom.style[k] || '';
              this._dom.style[k] = d[k];
            }
          }
          // 2) Function-form defaults, merged on top of the object form.
          //    Delegated so the same wipe-and-re-apply logic is used for
          //    new nodes here, for defaultNodeStyle(fn) re-evals, and for
          //    object-form changes that need to keep fn-values on top.
          if (c._defaultNodeStyleFn) c._applyDefaultNodeStyleFnTo(this);
        }

        // Query the node's live rendered DOM for an element matching `selector`.
        // Useful for patching small parts of node content (e.g. live metric
        // values) without re-stringifying the whole node body via setText().
        //
        // Returns Element | null. Searches inside the node's container (which
        // includes the rendered text/file/link content plus any badges or
        // attached widgets).
        //
        // Important: mutations made via the returned element are purely
        // visual. They do NOT update node.text, do NOT mark the canvas dirty,
        // do NOT emit events, and will be lost the next time setText() (or
        // any other content-rebuilding setter) is called on the node, or
        // when the canvas reloads from saved data. For persistent edits, use
        // setText() / update() instead.
        querySelector(selector) {
          if (!this._dom || !selector) return null;
          return this._dom.querySelector(selector);
        }

        // Plural form. Returns Element[] (a defensive array, not a live
        // NodeList) so callers can use array methods directly.
        querySelectorAll(selector) {
          if (!this._dom || !selector) return [];
          return Array.from(this._dom.querySelectorAll(selector));
        }

        // ---- CSS class management ----
        // Stable hooks for declarative state→style mapping via a stylesheet
        // the host controls. The library reserves classes prefixed with
        // `canvas-` (base classes like `canvas-node`) and `tc-` (everything
        // else the library owns - UI chrome and runtime effect classes like
        // `tc-pulse`/`tc-flash`/`tc-highlight`).
        // Don't add or remove those - use class names of your own choosing.
        //
        // Like `style()`, class mutations are purely visual: no dirty mark,
        // no events, no history, no serialization. They survive setColor()
        // and other state changes (we never touch consumer-added classes).
        addClass(...names) {
          if (this._dom) for (const n of names) if (n) this._dom.classList.add(n);
          return this;
        }
        removeClass(...names) {
          if (this._dom) for (const n of names) if (n) this._dom.classList.remove(n);
          return this;
        }
        toggleClass(name, force) {
          if (!this._dom || !name) return this;
          if (arguments.length >= 2) this._dom.classList.toggle(name, !!force);
          else this._dom.classList.toggle(name);
          return this;
        }
        hasClass(name) {
          if (!this._dom || !name) return false;
          return this._dom.classList.contains(name);
        }

        setFile(path) {
          if (this.type !== "file") return this;
          const next = path == null ? "" : String(path);
          if (next === this.file) return this;
          const prev = this.file;
          this.file = next;
          this._renderFile();
          if (this.file_label) {
            this.file_label.textContent = String(this.file).split("/").pop();
          }
          this._applyA11y();
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('nodeUpdate', { node: this, kind: 'file', file: next, prevFile: prev });
          return this;
        }

        // Enter in-place text edit on this node. Same flow as a user
        // double-click: contenteditable on, text selected, Enter commits,
        // Escape cancels, blur commits. Returns true if edit mode was
        // entered, false if blocked (wrong node type, another node already
        // editing, missing permission, per-node edit lock, or a
        // beforeNodeEditStart listener vetoed).
        beginEdit() {
          if (this.type !== "text") return false;
          if (this.parent.editing) return false;
          this.parent._enterEdit(this);
          return this.parent.editing === this;
        }

        // Programmatically exit edit mode on this node. commit=true keeps
        // the current edited text, commit=false reverts to the pre-edit
        // value. Returns true if this node was editing, false otherwise.
        endEdit(commit = true) {
          if (this.parent.editing !== this) return false;
          this.parent._exitEdit(!!commit);
          return true;
        }

        // Apply a partial property patch by routing to the appropriate
        // setX setters inside a single batch() - so the operation lands as
        // one history step and one dataChange. Recognized keys: x, y,
        // width, height, text, color, file, url. Unknown keys are ignored.
        update(patch) {
          if (!patch || typeof patch !== "object") return this;
          this.parent.batch(() => {
            if (patch.x !== undefined || patch.y !== undefined) {
              this.setPosition(
                patch.x !== undefined ? patch.x : this.x,
                patch.y !== undefined ? patch.y : this.y
              );
            }
            if (patch.width !== undefined || patch.height !== undefined) {
              this.setSize(
                patch.width  !== undefined ? patch.width  : this.width,
                patch.height !== undefined ? patch.height : this.height
              );
            }
            if (patch.text  !== undefined) this.setText(patch.text);
            if (patch.color !== undefined) this.setColor(patch.color);
            if (patch.file  !== undefined) this.setFile(patch.file);
            if (patch.url   !== undefined) this.setUrl(patch.url);
          });
          return this;
        }

        // ---- effects ----
        // Shared tween driver for animateTo/animateSize/animateBounds.
        // `slots` is one or two cancellation-slot names on this node;
        // animateBounds drives both so either slot's supersede cancels it.
        // `step(e)` interpolates given an eased 0..1 progress; `finalize()`
        // snaps to the end state on the last frame; `immediate()` is the
        // short-circuit when duration <= 0 (uses the public setter so
        // events still fire).
        _tween(slots, opts, step, finalize, immediate) {
          const duration = opts.duration != null ? opts.duration : DEFAULT_ANIM_MS;
          const easeFn = resolveEasing(opts.easing);
          // Cancel any in-flight animation occupying any of our slots.
          // animateBounds shares a single anim across both slots, so the
          // _owesFastDraw flag releases the fast-draw counter exactly once.
          for (const slot of slots) {
            const prev = this[slot];
            if (!prev) continue;
            prev.cancelled = true;
            if (prev._owesFastDraw) {
              prev._owesFastDraw = false;
              this.parent._endFastDraw();
            }
            const r = prev.resolve;
            // Clear every slot the previous anim was occupying (otherwise
            // animateBounds' anim object lingers in the slot we didn't visit).
            if (this._activeAnim === prev) this._activeAnim = null;
            if (this._activeSizeAnim === prev) this._activeSizeAnim = null;
            if (r) r();
          }
          if (duration <= 0) {
            immediate();
            return Promise.resolve();
          }
          const t0 = performance.now();
          this.parent._beginFastDraw();
          return new Promise(resolve => {
            const anim = { cancelled: false, resolve, _owesFastDraw: true };
            const tick = (now) => {
              if (anim.cancelled) return;
              const t = Math.min(1, (now - t0) / duration);
              step(easeFn(t));
              this.parent.requestDraw();
              if (t < 1) requestAnimationFrame(tick);
              else {
                finalize();
                if (anim._owesFastDraw) {
                  anim._owesFastDraw = false;
                  this.parent._endFastDraw();
                }
                this.parent._markDirty();
                this.parent.requestDraw();
                for (const slot of slots) this[slot] = null;
                resolve();
              }
            };
            for (const slot of slots) this[slot] = anim;
            requestAnimationFrame(tick);
          });
        }

        // Tween x/y. Returns a Promise that resolves when complete (or
        // immediately when cancelled by a newer animateTo on the same node).
        animateTo(x, y, opts = {}) {
          const startX = this.x, startY = this.y;
          return this._tween(
            ['_activeAnim'], opts,
            (e) => this._positionAt(startX + (x - startX) * e, startY + (y - startY) * e),
            () => this._positionAt(x, y),
            () => this.setPosition(x, y),
          );
        }

        // Tween width/height. Runs on its own cancellation slot
        // (_activeSizeAnim) so it can play in parallel with animateTo.
        // Returns a Promise that resolves when complete (or immediately when
        // cancelled by a newer animateSize/animateBounds on the same node).
        animateSize(w, h, opts = {}) {
          const startW = this.width, startH = this.height;
          return this._tween(
            ['_activeSizeAnim'], opts,
            (e) => this._sizeAt(startW + (w - startW) * e, startH + (h - startH) * e),
            () => this._sizeAt(w, h),
            () => this.setSize(w, h),
          );
        }

        // Tween position and size together on a single eased clock. Use when
        // the top-left moves with the size (e.g. growing from a top/left
        // edge) - keeps the moving edge visually rigid. Cancels any
        // in-flight animateTo or animateSize on this node.
        animateBounds(x, y, w, h, opts = {}) {
          const startX = this.x, startY = this.y;
          const startW = this.width, startH = this.height;
          return this._tween(
            ['_activeAnim', '_activeSizeAnim'], opts,
            (e) => {
              this._positionAt(startX + (x - startX) * e, startY + (y - startY) * e);
              this._sizeAt(startW + (w - startW) * e, startH + (h - startH) * e);
            },
            () => { this._positionAt(x, y); this._sizeAt(w, h); },
            () => { this._positionAt(x, y); this.setSize(w, h); },
          );
        }

        // Pulse: a glowing ring expands and fades. Pure visual; no _markDirty.
        pulse(opts = {}) {
          const duration = opts.duration != null ? opts.duration : PULSE_DEFAULT_MS;
          const count    = opts.count    != null ? opts.count    : 1;
          const dom = this._dom;
          // Setting --tc-fx-color overrides the CSS layered fallback
          // (--tc-pulse-color -> hardcoded). When no opts.color is given,
          // clear the inline value so a previous colored pulse doesn't
          // shadow the host's --tc-pulse-color theming.
          if (opts.color) dom.style.setProperty('--tc-fx-color', opts.color);
          else dom.style.removeProperty('--tc-fx-color');
          dom.style.setProperty('--tc-fx-duration', duration + "ms");
          dom.style.setProperty('--tc-fx-count',    String(count));
          // Re-trigger by removing and re-adding the class in the next frame.
          dom.classList.remove('tc-pulse');
          // Force reflow so the animation restarts even if pulse is called
          // again before the previous one finished.
          void dom.offsetWidth;
          dom.classList.add('tc-pulse');
          if (this._pulseTimer) clearTimeout(this._pulseTimer);
          this._pulseTimer = setTimeout(() => {
            dom.classList.remove('tc-pulse');
            this._pulseTimer = null;
          }, duration * count + 50);
          return this;
        }

        // Flash: temporarily tints the background, then fades back.
        flash(color, opts = {}) {
          const duration = opts.duration != null ? opts.duration : 400;
          const dom = this._dom;
          const baseline = dom.style.backgroundColor;
          dom.style.setProperty('--tc-fx-duration', duration + "ms");
          dom.classList.add('tc-flash');
          dom.style.backgroundColor = color;
          if (this._flashTimer) clearTimeout(this._flashTimer);
          this._flashTimer = setTimeout(() => {
            dom.style.backgroundColor = baseline;
            this._flashTimer = setTimeout(() => {
              dom.classList.remove('tc-flash');
              this._flashTimer = null;
            }, duration);
          }, duration);
          return this;
        }

        // Highlight: persistent glow until highlight(false).
        highlight(on, opts = {}) {
          const dom = this._dom;
          if (on === false) {
            dom.classList.remove('tc-highlight');
            dom.style.removeProperty('--tc-fx-color');
            return this;
          }
          // Clear when no opts.color so a prior colored highlight does not
          // shadow the host's --tc-highlight-color theming via the inline
          // --tc-fx-color override.
          if (opts.color) dom.style.setProperty('--tc-fx-color', opts.color);
          else dom.style.removeProperty('--tc-fx-color');
          dom.classList.add('tc-highlight');
          return this;
        }

        // Lock prevents the named gestures on this node. Pass an object with
        // any of {move, resize, edit}; omitted keys are unchanged. unlock()
        // clears all locks.
        lock(opts = {}) {
          if (!this._lock) this._lock = { move: false, resize: false, edit: false };
          if (opts.move   !== undefined) this._lock.move   = !!opts.move;
          if (opts.resize !== undefined) this._lock.resize = !!opts.resize;
          if (opts.edit   !== undefined) this._lock.edit   = !!opts.edit;
          return this;
        }

        unlock() {
          this._lock = null;
          return this;
        }

        // Badge: small overlay tag pinned to the node's top-right corner.
        // Pass null/empty to clear. Accepts a string or { html: "..." }.
        // Lives in drawing_container (sibling of the node), not inside _dom,
        // so it can overhang the node's border without being clipped by
        // overflow/border-radius. Position is kept in sync via _refreshBadgePosition.
        setBadge(value) {
          if (value == null || value === "") {
            if (this._badge && this._badge.parentNode) this._badge.remove();
            this._badge = null;
            return this;
          }
          if (!this._badge) {
            this._badge = document.createElement("div");
            this._badge.className = "tc-node-badge";
            this.parent.drawing_container.appendChild(this._badge);
            this._refreshBadgePosition();
          }
          if (typeof value === "object" && value && value.html != null) {
            renderUntrustedHTML(this._badge, value.html, this.parent.sanitize);
          } else {
            this._badge.textContent = String(value);
          }
          return this;
        }

        _refreshBadgePosition() {
          if (!this._badge) return;
          this._badge.style.left = (this.x + this.width) + "px";
          this._badge.style.top  = this.y + "px";
        }

        // Multi-position decoration API. Generalisation of setBadge: each
        // decoration is identified by an `id` (for later update/removal),
        // pinned to one of the node's nine anchor points (corners +
        // mid-edges + center), and optionally offset by [dx, dy] world units.
        //
        //   node.addDecoration({
        //     id:       'status',
        //     position: 'tr',                                   // see positions below
        //     content:  '⬤',                                    // string | HTMLElement
        //     style:    { color: '#0f0', fontSize: '12px' },    // optional
        //     offset:   [4, -4]                                 // optional [dx,dy]
        //   });
        //
        //   node.removeDecoration('status');   // by id
        //   node.removeDecoration();           // remove all
        //   node.decorations();                // {id, el, position, offset}[]
        //
        // Re-calling addDecoration() with an existing id replaces that
        // decoration's element/content/style/position. Returns the
        // decoration's DOM element so the host can mutate it directly
        // (e.g. flip a color on a status change without going through
        // addDecoration again).
        //
        // Positions accepted: 'tl' | 'tr' | 'bl' | 'br' | 'top' | 'bottom' |
        // 'left' | 'right' | 'center'. 'top-left', 'topleft', etc. are
        // accepted as friendly aliases. Default is 'tr'.
        //
        // Decorations are siblings of the node DOM in drawing_container so
        // they can overhang the node's borders without being clipped, and
        // so decorations on group nodes paint above edges (which is what
        // you want for washi-tape / status-dot use cases).
        addDecoration(opts) {
          if (!opts || typeof opts !== 'object') return null;
          const id = (opts.id != null) ? String(opts.id) : ('d' + Math.random().toString(36).slice(2, 9));
          const position = this._normalizeDecorationPosition(opts.position);
          const offset = Array.isArray(opts.offset) ? opts.offset : [0, 0];

          if (!this._decorations) this._decorations = new Map();
          // Replace existing decoration with the same id.
          const prev = this._decorations.get(id);
          let el;
          if (prev) {
            el = prev.el;
            // Clear prior content and inline style so the new opts fully
            // define the visual; positioning styles are re-asserted below.
            el.textContent = '';
            el.removeAttribute('style');
          } else {
            el = document.createElement('div');
            el.className = 'tc-node-decoration';
            el.dataset.decorationId = id;
            this.parent.drawing_container.appendChild(el);
          }

          // Positioning skeleton - the rest of the inline style (color,
          // font, background, etc.) comes from opts.style.
          el.style.position = 'absolute';
          el.style.pointerEvents = 'none';
          el.style.transform = 'translate(-50%, -50%)';
          el.style.zIndex = '150';

          // Host style overrides (after positioning so the host can add
          // pointer-events:auto or change zIndex if they really want).
          if (opts.style && typeof opts.style === 'object') {
            for (const k of Object.keys(opts.style)) el.style[k] = opts.style[k];
          }

          // Content: string -> textContent, HTMLElement -> appendChild.
          if (opts.content && opts.content.nodeType === 1) {
            el.appendChild(opts.content);
          } else if (opts.content != null) {
            el.textContent = String(opts.content);
          }

          this._decorations.set(id, { id, el, position, offset });
          this._refreshDecorationPositions();
          return el;
        }

        removeDecoration(id) {
          if (!this._decorations) return this;
          if (id == null) {
            // Remove all.
            for (const d of this._decorations.values()) {
              if (d.el && d.el.parentNode) d.el.remove();
            }
            this._decorations.clear();
            return this;
          }
          const key = String(id);
          const d = this._decorations.get(key);
          if (!d) return this;
          if (d.el && d.el.parentNode) d.el.remove();
          this._decorations.delete(key);
          return this;
        }

        decorations() {
          if (!this._decorations || this._decorations.size === 0) return [];
          // Return a shallow snapshot the caller can iterate / inspect
          // without risking mutation of internal state.
          return [...this._decorations.values()].map(d => ({
            id: d.id, el: d.el, position: d.position, offset: d.offset.slice()
          }));
        }

        // Normalize friendly aliases ('top-left', 'topleft', 'tl') to the
        // canonical 8-position (+ center) shortform.
        _normalizeDecorationPosition(p) {
          if (typeof p !== 'string') return 'tr';
          const s = p.toLowerCase().replace(/[\s_-]/g, '');
          const map = {
            tl: 'tl', topleft: 'tl', lefttop: 'tl',
            tr: 'tr', topright: 'tr', righttop: 'tr',
            bl: 'bl', bottomleft: 'bl', leftbottom: 'bl',
            br: 'br', bottomright: 'br', rightbottom: 'br',
            top: 'top', t: 'top',
            bottom: 'bottom', b: 'bottom',
            left: 'left', l: 'left',
            right: 'right', r: 'right',
            center: 'center', c: 'center', middle: 'center'
          };
          return map[s] || 'tr';
        }

        _refreshDecorationPositions() {
          if (!this._decorations || this._decorations.size === 0) return;
          const x = this.x, y = this.y, w = this.width, h = this.height;
          const cx = x + w / 2, cy = y + h / 2, rx = x + w, by = y + h;
          for (const d of this._decorations.values()) {
            let ax = rx, ay = y;
            switch (d.position) {
              case 'tl':     ax = x;  ay = y;  break;
              case 'tr':     ax = rx; ay = y;  break;
              case 'bl':     ax = x;  ay = by; break;
              case 'br':     ax = rx; ay = by; break;
              case 'top':    ax = cx; ay = y;  break;
              case 'bottom': ax = cx; ay = by; break;
              case 'left':   ax = x;  ay = cy; break;
              case 'right':  ax = rx; ay = cy; break;
              case 'center': ax = cx; ay = cy; break;
            }
            d.el.style.left = (ax + (d.offset[0] || 0)) + 'px';
            d.el.style.top  = (ay + (d.offset[1] || 0)) + 'px';
          }
        }

        // Re-pin every drawing_container child that follows this node
        // (badge + decorations). Callers used to pair the two refreshes by
        // hand at every move/resize site; this is the single hook to use.
        _refreshAttached() {
          this._refreshBadgePosition();
          this._refreshDecorationPositions();
        }

        // Host-provided DOM widget. Replaces any prior widget on this node.
        attachWidget(htmlEl) {
          if (!htmlEl || htmlEl.nodeType !== 1) return this;
          this.detachWidget();
          this._widgetSlot = document.createElement("div");
          this._widgetSlot.className = "tc-node-widget";
          this._widgetSlot.appendChild(htmlEl);
          this._dom.appendChild(this._widgetSlot);
          return this;
        }

        detachWidget() {
          if (this._widgetSlot && this._widgetSlot.parentNode) this._widgetSlot.remove();
          this._widgetSlot = null;
          return this;
        }

        // Move this node to the top of the z-order. Forces the reorder even
        // for link nodes (which the internal helper skips by default to avoid
        // iframe-reload churn during interactive promotion).
        bringToFront() {
          this.parent._bringNodeToFront(this, { force: true });
          this.parent._markDirty();
          this.parent.requestDraw();
          return this;
        }

        // Move this node to the bottom of the z-order. Symmetric with
        // bringToFront(). For groups this reorders within the group_container
        // layer only, so content nodes and edges still paint on top.
        sendToBack() {
          this.parent._sendNodeToBack(this, { force: true });
          this.parent._markDirty();
          this.parent.requestDraw();
          return this;
        }

        // ---- queries ----
        edges(opts) { return this.parent.graph.edgesOf(this, opts); }
        neighbors(opts) { return this.parent.graph.neighbors(this, opts); }

        // World-space bounding rect, using measured DOM size when available
        // (so width/height reflect group label / file label overhang etc.).
        bounds() {
          const w = (this._dom && this._dom.offsetWidth)  || this.width;
          const h = (this._dom && this._dom.offsetHeight) || this.height;
          return { x: this.x, y: this.y, width: w, height: h };
        }

        // World-space center, derived from bounds() so DOM-measured overhang
        // (group labels, file labels) is reflected.
        center() {
          const b = this.bounds();
          return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
        }

        isSelected() { return this.parent.selectedNodes.includes(this); }

        select({ additive = false } = {}) {
          const c = this.parent;
          const next = additive ? c.selectedNodes.slice() : [];
          if (!next.includes(this)) next.push(this);
          c._setSelection(next, c.selectedEdge);
          return this;
        }

        deselect() {
          const c = this.parent;
          if (!c.selectedNodes.includes(this)) return this;
          c._setSelection(c.selectedNodes.filter(n => n !== this), c.selectedEdge);
          return this;
        }

        // Clone this node with an offset. Returns the new node. The clone
        // gets a fresh id; extraData and color are preserved. Goes through
        // createNode so 'nodeCreate' fires and history captures it.
        duplicate(opts = {}) {
          const dx = (opts.offset && opts.offset.x != null) ? opts.offset.x : 20;
          const dy = (opts.offset && opts.offset.y != null) ? opts.offset.y : 20;
          const data = {
            ...(this._extraData || {}),
            type: this.type,
            x: this.x + dx,
            y: this.y + dy,
            width: this.width,
            height: this.height,
          };
          if (this.text)          data.text  = this.text;
          if (this.label != null) data.label = this.label;
          if (this.file != null)  data.file  = this.file;
          if (this.type === "link"  && this.url != null) data.url = this.url;
          if (this.type === "group" && this.background) {
            data.background      = this.background;
            data.backgroundStyle = this.backgroundStyle || "cover";
          }
          if (this.color && this.color !== "default") data.color = this.color;
          // Carry authored accessibility data to the clone; hints
          // re-run on the new node so context-derived values refresh.
          if (this._a11y_authored && Object.keys(this._a11y_authored).length > 0) {
            data.accessibility = { ...this._a11y_authored };
          }
          return this.parent.createNode(data);
        }

        delete() {
          return this.parent.deleteNode(this);
        }

      } // CanvasNode

      const EDGE_KNOWN_KEYS = new Set([
        "id","fromNode","toNode","fromSide","toSide",
        "fromEnd","toEnd","color","label",
        "accessibility"
      ]);

      // Valid anchor sides (used by setFromSide/setToSide).
      const EDGE_SIDES = new Set(["left", "right", "top", "bottom"]);

      //////////////////////////////////////////////////////////////////////////////////
      // CanvasEdge ///////////////////////////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////
      class CanvasEdge {
        constructor(parent, edge_data) {
          this.parent = parent;
          this.edge_data = edge_data;
          this.id = edge_data.id;
          this.fromNode = edge_data.fromNode;
          this.toNode = edge_data.toNode;

          this._extraData = {};
          for (const k of Object.keys(edge_data)) {
            if (!EDGE_KNOWN_KEYS.has(k)) this._extraData[k] = edge_data[k];
          }

          this._a11y_authored = (edge_data.accessibility && typeof edge_data.accessibility === "object")
            ? { ...edge_data.accessibility }
            : null;
          this._a11y_hints = undefined;

          this.fromSide = edge_data.fromSide;
          this.toSide = edge_data.toSide;

          //arrow or none
          this.fromEnd = edge_data.fromEnd ?? "none";  
          this.toEnd = edge_data.toEnd ?? "arrow";


          var color = edge_data.color ?? "default";
          var colors = parent._resolveColor(color, "edge");

          this.color =  color;
          this.backgroundColor = colors.bgcolor;
          this.borderColor = colors.borderColor;

          this.edge_label = edge_data.label ?? "";


          const { from: n1, to: n2 } = this.endpoints();

          var ctx = this.parent.ctx ;

          var from_point = rectBorderPoint(n1, n2);
          var to_point = rectBorderPoint(n2, n1);

          var startx = from_point.x;
          var starty = from_point.y;
          var endx = to_point.x;
          var endy = to_point.y;
          
        
          this.selected = false;
          this.hovered = false;

          this.pathX = []; /* approximate path for hit detection */
          this.pathY = [];
          this.segmentLocations = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0];
          this.boundingBox = {};
          this.hitRadius = 10;

          // Visual-only style overrides applied during drawSmartBezier.
          // See style() for accepted keys.
          this._styleOverrides = null;

          // this edge's <li> in the canvas-wide hidden mirror.
          // _applyA11y writes its label/aria-hidden; _destroyA11yMirror
          // strips it. Created here so load + createEdge + paste +
          // duplicate + edge-draft commit all converge on one path.
          this._setupA11yMirror();
        }


        rectEdgePoint(n, which_side = "from") {
          /*
            returns the midpoint of a connecting nodes side, which indicates
            where this edge connects to, and then an endpoint of the edge path
          */

          const cx = n.x + n.width / 2, cy = n.y + n.height / 2;

          if (which_side == "from") {
            var edge_side = this.fromSide;
          } else {
            var edge_side = this.toSide;
          }

          var dx = edge_side === "left" ? -1 : edge_side === "right" ? 1 : 0;
          var dy = edge_side === "top" ? -1 : edge_side === "bottom" ? 1 : 0;

          // var dx = n.fromSide === 'left' ? -n.width / 2 : n.fromSide === 'right' ? n.width / 2 : 0;
          // var dy = n.fromSide === 'top' ? -n.height / 2 : n.fromSide === 'bottom' ? n.height / 2 : 0;


          // const dx = target.x - cx, dy = target.y - cy;
          // const absDx = Math.abs(dx), absDy = Math.abs(dy);
          // const hw = rect.width / 2, hh = rect.height / 2;
          // if (absDx / hw > absDy / hh) return { x: cx + (dx > 0 ? hw : -hw), y: cy + dy * hw / absDx };
          return { x: cx + dx * n.width / 2, y: cy  + dy * n.height / 2 , dx:dx, dy:dy, w: n.width, h: n.height};
        }

        hitEdge(x,y){
          if (this.boundingBox) {         
            
            //bounding box check
            const bb = this.boundingBox;
            if ( (bb.left <= x) && (x <= bb.right) && (bb.top <= y) && (y <= bb.bottom) ){
              // then can do a segment check. 
              const C = {x: x, y: y};
              const R = this.hitRadius;
     
              for (let i = 0; i < this.pathX.length - 1; i++) {
                const P = {x: this.pathX[i], y:this.pathY[i]}
                const Q = {x: this.pathX[i+1], y:this.pathY[i+1]}
                if (this.__lineCircleCollision(R, C, P, Q)) {
                  return this
                }
              }


 //             return this; 
            }
            return null;


          }
        }


   
        //  /////////////////////////////

         __lineCircleCollision(R, C, A, B) {
            var distAB = Math.sqrt( (A.x-B.x) * (A.x-B.x) + (A.y-B.y) * (A.y-B.y))
            var u = {x: (B.x - A.x) / distAB, y: (B.y - A.y) / distAB}
            var vecAC = {x: C.x - A.x, y: C.y - A.y};
            var vecACdotu = vecAC.x * u.x + vecAC.y * u.y;
            var vecAD = {x: vecACdotu * u.x, y: vecACdotu * u.y };
            var D = {x: A.x + vecAD.x,  y: A.y + vecAD.y};

            var distAC = Math.sqrt( (A.x - C.x) * (A.x - C.x) + (A.y - C.y) * (A.y - C.y));
            var distBC = Math.sqrt( (B.x - C.x) * (B.x - C.x) + (B.y - C.y) * (B.y - C.y));
            if ( (distAC < R) || (distBC < R)  ) {

              return true;
            }
            var distAD = Math.sqrt( (A.x - D.x) * (A.x - D.x) + (A.y - D.y) * (A.y - D.y));
            var distBD = Math.sqrt( (B.x - D.x) * (B.x - D.x) + (B.y - D.y) * (B.y - D.y));
            if ( distAD + distBD <= (distAB * 1.001 ) ) {
              var distCD = Math.sqrt( (C.x - D.x) * (C.x - D.x) + (C.y - D.y) * (C.y - D.y));
              if (distCD < R) {
                return true;
              } else { return false };
            }
            return false;
         }

         _bez(t, start, ctrl1, ctrl2, end) {
            // bezier curve
            var b = [];
            t.forEach(s => {
              b.push(
                  (1 - s) * (1 - s) * (1-s) * start
                + 3 * (1 - s) * (1-s)  * s * ctrl1
                + 3 * (1 - s) * s * s  * ctrl2
                + s * s * s * end
              );
            });
            return b;
         }

         _dbez(t, start, ctrl1, ctrl2, end) {
            // d/dt of bezier
            var b = [];
            t.forEach(s => {
              b.push(
               3 * (1 - s) * (1-s) * (ctrl1- start)
              + 6 * (1-s) * s * (ctrl2 - ctrl1)
              + 3 * s * s * (end - ctrl2)
              );
            });
            return b;
         }


        // Returns the cubic-bezier control points the edge renderer uses, in
        // world coordinates. Recomputed from current node positions on every
        // call, so callers driving per-frame animation (camera fly-along, etc.)
        // see the live geometry. Shared with drawSmartBezier so the rendered
        // curve and the queried curve cannot drift.
        _computeBezierPath() {
          const { from: fromNode, to: toNode } = this.endpoints();
          const start = this.rectEdgePoint(fromNode, "from");
          const end   = this.rectEdgePoint(toNode,   "to");
          const O = this._styleOverrides;
          const curvature = (O && O.curvature != null) ? O.curvature : 1;
          const { ctrl1, ctrl2 } = CanvasEdge._bezierControls(start, end, curvature);
          return { start, end, ctrl1, ctrl2 };
        }

        // Shrink the control-point offset for short edges so the two control
        // points cannot cross each other (which produces a visible zig-zag).
        // Clamped to (1, CONTROL_STRENGTH].
        static _controlStrengthFor(start, end) {
          const dist = Math.hypot(end.x - start.x, end.y - start.y);
          return Math.max(2, Math.min(CanvasEdge.CONTROL_STRENGTH, dist / 2));
        }

        // Shared bezier control-point geometry. Inputs are anchor points
        // {x, y, dx, dy} where dx/dy is the outward normal at that anchor.
        // All edge-curve renderers route through this so they cannot drift.
        // `curvature` is a multiplier on the auto-computed control strength;
        // 1 = library default, 0 = control points snap to endpoints (the
        // curve degenerates to a visual straight line), >1 exaggerates.
        static _bezierControls(start, end, curvature = 1) {
          const K = CanvasEdge._controlStrengthFor(start, end) * curvature;
          return {
            ctrl1: { x: start.x + start.dx * K, y: start.y + start.dy * K },
            ctrl2: { x: end.x   + end.dx   * K, y: end.y   + end.dy   * K },
          };
        }

        // Public bezier accessor: P0/P3 are the side-anchor endpoints,
        // P1/P2 are the two control points.
        getBezierPath() {
          const { start, end, ctrl1, ctrl2 } = this._computeBezierPath();
          return {
            P0: { x: start.x, y: start.y },
            P1: { x: ctrl1.x, y: ctrl1.y },
            P2: { x: ctrl2.x, y: ctrl2.y },
            P3: { x: end.x,   y: end.y   },
          };
        }

        // Sample the curve at parameter t ∈ [0, 1]. Scalar inline form so
        // per-frame callers don't allocate an array (vs. _bez which takes a
        // t-array).
        pointAt(t) {
          const { start, end, ctrl1, ctrl2 } = this._computeBezierPath();
          const m = 1 - t;
          return {
            x: m*m*m*start.x + 3*m*m*t*ctrl1.x + 3*m*t*t*ctrl2.x + t*t*t*end.x,
            y: m*m*m*start.y + 3*m*m*t*ctrl1.y + 3*m*t*t*ctrl2.y + t*t*t*end.y,
          };
        }

        // Unnormalized tangent (dP/dt) at parameter t.
        tangentAt(t) {
          const { start, end, ctrl1, ctrl2 } = this._computeBezierPath();
          const m = 1 - t;
          return {
            x: 3*m*m*(ctrl1.x - start.x) + 6*m*t*(ctrl2.x - ctrl1.x) + 3*t*t*(end.x - ctrl2.x),
            y: 3*m*m*(ctrl1.y - start.y) + 6*m*t*(ctrl2.y - ctrl1.y) + 3*t*t*(end.y - ctrl2.y),
          };
        }

        ///////////////////////////////////
        ////// BEZIER NODES //////////////
        /////////////////////////////////
        drawSmartBezier() {
            /*
            This is essentially the edges' local .drawSelf() function.

            lets use this to calcualte a 2D path and store that locally
            for hit testing.
            */

            // Defensive: a node deleted out from under a live edge (deletion
            // race) would make endpoints() return undefined and crash the
            // render loop. Load-time normalization drops orphan edges, but a
            // runtime gap should silently no-op this frame, not blank the
            // canvas.
            const { from: _fromNode, to: _toNode } = this.endpoints();
            if (!_fromNode || !_toNode) return;

            var ctx = this.parent.ctx ;
            var scale = this.parent.scale / 2;
            var arrow_s = 8.0;

            ctx.save()

            const { start, end, ctrl1, ctrl2 } = this._computeBezierPath();
            const O = this._styleOverrides || null;


                // Shadow blur on every stroked bezier is the dominant
                // CanvasRenderer cost on Firefox during drag. Skip it while a
                // drag is active; event_mouseup triggers a final requestDraw()
                // that paints shadows back in. The same applies to programmatic
                // animations (animateTo/Size/Bounds, layout tweens, camera
                // tweens) - _fastDrawCount is a refcount they bump for the
                // duration of the tween and release before the final paint.
                const skipShadow = this.parent.dragging || this.parent.edgeDraft || this.parent._fastDrawCount > 0;

                ctx.strokeStyle = this.borderColor ?? "#00FF00";

                // Width: O.width is a multiplier (1 = library default).
                // Selected edges keep their 2x emphasis on top of that.
                const baseW = (O && O.width != null) ? O.width : 1;
                ctx.lineWidth = (this.selected ? baseW * 2 : baseW) / scale;

                if (O && O.cap) ctx.lineCap = O.cap;
                if (O && Array.isArray(O.dash)) {
                  ctx.setLineDash(O.dash.map(d => d / scale));
                  if (O.dashOffset != null) ctx.lineDashOffset = O.dashOffset / scale;
                }

                if (!skipShadow) {
                  if (O && O.glow) {
                    ctx.shadowColor = O.glow.color;
                    ctx.shadowBlur  = ((O.glow.blur != null ? O.glow.blur : 12)) / scale;
                  } else if (this.hovered && !this.selected) {
                    ctx.shadowColor = this.borderColor;
                    ctx.shadowBlur = 10 / scale;
                  } else if (this.selected) {
                    ctx.shadowColor = this.borderColor;
                    ctx.shadowBlur = 8 / scale;
                  }
                }

            ctx.beginPath();
            ctx.moveTo(start.x, start.y);
            ctx.bezierCurveTo(ctrl1.x, ctrl1.y, ctrl2.x, ctrl2.y, end.x, end.y);
            ctx.stroke();

              ctx.shadowOffsetX = 0;
              ctx.shadowOffsetY = 0;
              ctx.shadowBlur = 0;
              // Arrowheads draw unbroken regardless of stroke dash.
              if (O && Array.isArray(O.dash)) ctx.setLineDash([]);

            this.pathX = (this._bez(this.segmentLocations, start.x, ctrl1.x, ctrl2.x, end.x));
            this.pathY = (this._bez(this.segmentLocations, start.y, ctrl1.y, ctrl2.y, end.y));

            const bbTop = Math.min(...this.pathY) - this.hitRadius;
            const bbBot = Math.max(...this.pathY) + this.hitRadius;
            const bbLef = Math.min(...this.pathX) - this.hitRadius;
            const bbRig = Math.max(...this.pathX) + this.hitRadius;


            this.boundingBox = {left: bbLef, right:bbRig, top:bbTop, bottom: bbBot, width:bbRig-bbLef, height:bbBot-bbTop};


            // ctx.save();
            // ctx.strokeStyle = "#88FF33";
            // ctx.moveTo(this.parent.mouseX, this.parent.mouseY);
            // ctx.beginPath();
            // ctx.arc(this.parent.mouseX, this.parent.mouseY, this.hitRadius, 0, 2 * Math.PI);
            // ctx.stroke()
            // ctx.restore();


            if (this.fromEnd == "arrow") {
              const bx = (this._bez([0.00], start.x, ctrl1.x, ctrl2.x, end.x))[0];
              const by = (this._bez([0.00], start.y, ctrl1.y, ctrl2.y, end.y))[0];
              const dxA = (this._dbez([0.02], start.x, ctrl1.x, ctrl2.x, end.x))[0];
              const dyA = (this._dbez([0.02], start.y, ctrl1.y, ctrl2.y, end.y))[0];
              // +PI flips the head so it points back along the curve toward
              // the fromNode (the arrow sits at t=0, pointing outward).
              const angle = Math.atan2(dyA, dxA) + Math.PI;
              this._drawArrowHead(ctx, bx, by, angle, scale);
            }

            if (this.toEnd == "arrow") {
              const bx = (this._bez([1.0], start.x, ctrl1.x, ctrl2.x, end.x))[0];
              const by = (this._bez([1.0], start.y, ctrl1.y, ctrl2.y, end.y))[0];
              const dxA = (this._dbez([0.98], start.x, ctrl1.x, ctrl2.x, end.x))[0];
              const dyA = (this._dbez([0.98], start.y, ctrl1.y, ctrl2.y, end.y))[0];
              const angle = Math.atan2(dyA, dxA);
              this._drawArrowHead(ctx, bx, by, angle, scale);
            }
            
            // Always cache the midpoint so the floating edge toolbar can
            // position itself even when there's no label yet.
            {
              var tMid = 0.5;
              this._labelX = (this._bez([tMid], start.x, ctrl1.x, ctrl2.x, end.x))[0];
              this._labelY = (this._bez([tMid], start.y, ctrl1.y, ctrl2.y, end.y))[0];
            }

            if (this.edge_label) {
              const labelO = (O && O.label) || null;
              const textColor = (labelO && labelO.color) || this.parent._palette.arrowFill;

              ctx.font = (labelO && labelO.font) || '16px sans-serif';

              const tm = ctx.measureText(this.edge_label);
              // measureText returns the actual ascent/descent on modern
              // browsers; fontBoundingBox* are wider fallbacks. The hardcoded
              // 12/4 is a last-ditch fallback (matches a typical 16px font).
              const ascent  = tm.actualBoundingBoxAscent  || tm.fontBoundingBoxAscent  || 12;
              const descent = tm.actualBoundingBoxDescent || tm.fontBoundingBoxDescent || 4;
              const x = this._labelX - tm.width / 2;
              const y = this._labelY;

              if (labelO && labelO.background) {
                const pad    = labelO.padding != null ? labelO.padding : 0;
                const radius = labelO.borderRadius != null ? labelO.borderRadius : 0;
                const bgX = x - pad;
                const bgY = y - ascent - pad;
                const bgW = tm.width + 2 * pad;
                const bgH = ascent + descent + 2 * pad;
                ctx.fillStyle = labelO.background;
                if (radius > 0 && typeof ctx.roundRect === 'function') {
                  ctx.beginPath();
                  ctx.roundRect(bgX, bgY, bgW, bgH, radius);
                  ctx.fill();
                } else {
                  ctx.fillRect(bgX, bgY, bgW, bgH);
                }
              }

              ctx.fillStyle = textColor;
              ctx.fillText(this.edge_label, x, y);
            }

            ctx.restore()

        }

        // Draws an arrowhead at (bx, by) pointing along `angle`. Reads the
        // edge's style overrides for arrowSize / arrowStyle. Selected edges
        // bump head size by 1.3x to mirror the line-width emphasis.
        _drawArrowHead(ctx, bx, by, angle, scale) {
          const O = this._styleOverrides || null;
          const baseSize = (O && O.arrowSize != null) ? O.arrowSize : 8;
          const head = (this.selected ? baseSize * 1.3 : baseSize) / scale;
          const style = (O && O.arrowStyle) || 'arrow';

          if (style === 'dot') {
            ctx.beginPath();
            ctx.arc(bx, by, head * 0.5, 0, Math.PI * 2);
            ctx.fillStyle = ctx.strokeStyle;
            ctx.fill();
            return;
          }

          const wingMinusX = bx - head * Math.cos(angle - Math.PI / 6);
          const wingMinusY = by - head * Math.sin(angle - Math.PI / 6);
          const wingPlusX  = bx - head * Math.cos(angle + Math.PI / 6);
          const wingPlusY  = by - head * Math.sin(angle + Math.PI / 6);

          if (style === 'diamond') {
            // Mirror the wings across (bx, by) to get a kite. Back vertex
            // sits at 2*head along the reverse angle.
            const backX = bx - 2 * head * Math.cos(angle);
            const backY = by - 2 * head * Math.sin(angle);
            ctx.beginPath();
            ctx.moveTo(bx, by);
            ctx.lineTo(wingMinusX, wingMinusY);
            ctx.lineTo(backX, backY);
            ctx.lineTo(wingPlusX, wingPlusY);
            ctx.closePath();
            ctx.fillStyle = ctx.strokeStyle;
            ctx.fill();
            return;
          }

          if (style === 'half') {
            // Just the upper wing - a feathered arrow used in some diagram
            // conventions (e.g. open/inheritance markers).
            ctx.beginPath();
            ctx.moveTo(bx, by);
            ctx.lineTo(wingMinusX, wingMinusY);
            ctx.stroke();
            return;
          }

          // 'arrow' (default) - filled triangle.
          ctx.beginPath();
          ctx.moveTo(bx, by);
          ctx.lineTo(wingMinusX, wingMinusY);
          ctx.lineTo(wingPlusX,  wingPlusY);
          ctx.closePath();
          ctx.fillStyle = ctx.strokeStyle;
          ctx.fill();
        }

        // ============================================================
        // Public Edge API: setters, queries, lifecycle
        // ============================================================
        setColor(key) {
          const next = key == null ? "default" : key;
          if (next === this.color) return this;
          const prev = this.color;
          const colors = this.parent._resolveColor(next, "edge");
          this.color = next;
          this.backgroundColor = colors.bgcolor;
          this.borderColor     = colors.borderColor;
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('edgeUpdate', { edge: this, kind: 'color', color: next, prevColor: prev });
          return this;
        }

        setLabel(text) {
          const next = text == null ? "" : String(text);
          if (next === this.edge_label) return this;
          const prev = this.edge_label;
          this.edge_label = next;
          this._applyA11y();
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('edgeUpdate', { edge: this, kind: 'label', label: next, prevLabel: prev });
          return this;
        }

        // ---- accessibility API ----
        _deriveA11y() {
          const c = this.parent;
          const src = c.getNode(this.fromNode);
          const tgt = c.getNode(this.toNode);
          // Use endpoints' *effective* labels (host overrides win)
          // so the mirror text matches what a focused node announces.
          const srcLabel = (src && src._effectiveA11y) ? src._effectiveA11y().label : this.fromNode;
          const tgtLabel = (tgt && tgt._effectiveA11y) ? tgt._effectiveA11y().label : this.toNode;
          const base = "Edge from " + srcLabel + " to " + tgtLabel;
          const label = this.edge_label
            ? base + ", labeled \"" + String(this.edge_label).replace(/\s+/g, " ").trim() + "\""
            : base;
          return { label };
        }

        _effectiveA11y() {
          const derived = this._deriveA11y();
          if (this._a11y_hints === undefined) {
            const cb = this.parent && this.parent._accessibilityHints;
            if (typeof cb === "function") {
              try {
                const r = cb(this);
                this._a11y_hints = (r && typeof r === "object") ? { ...r } : null;
              } catch (_) {
                this._a11y_hints = null;
              }
            } else {
              this._a11y_hints = null;
            }
          }
          return Object.assign({}, derived, this._a11y_hints || {}, this._a11y_authored || {});
        }

        getAccessibility() {
          return this._effectiveA11y();
        }

        setAccessibility(partial) {
          if (partial === null) {
            if (!this._a11y_authored) return this;
            this._a11y_authored = null;
          } else if (partial && typeof partial === "object") {
            const next = this._a11y_authored ? { ...this._a11y_authored } : {};
            for (const k of Object.keys(partial)) {
              const v = partial[k];
              if (v == null) delete next[k];
              else next[k] = v;
            }
            this._a11y_authored = (Object.keys(next).length > 0) ? next : null;
          } else {
            return this;
          }
          // hidden state may have flipped - re-apply and refresh both
          // endpoint summaries (a now-hidden edge drops out of them).
          this._applyA11y();
          if (this.parent._refreshA11yNodeSummary) {
            const { from, to } = this.endpoints();
            this.parent._refreshA11yNodeSummary(from);
            this.parent._refreshA11yNodeSummary(to);
          }
          this.parent._markDirty();
          return this;
        }

        // Create the hidden <li> mirror for this edge. Called from the
        // constructor; the parent canvas's _a11yEdgesList must exist.
        // Endpoint node summaries are NOT refreshed here - at
        // constructor time the edge isn't yet in canvas._edges, so a
        // refresh would miss its own contribution. The callers that
        // push to _edges trigger the refresh (or createNodesAndEdges
        // does one bulk refresh once every edge is registered).
        _setupA11yMirror() {
          const c = this.parent;
          if (!c || !c._a11yEdgesList) return;
          this._a11yLi = document.createElement("li");
          this._a11yLi.id = c._instanceId + "-edge-" + this.id;
          this._a11yLi.setAttribute("role", "listitem");
          c._a11yEdgesList.appendChild(this._a11yLi);
          this._applyA11y();
        }

        _destroyA11yMirror() {
          if (this._a11yLi && this._a11yLi.parentNode) {
            this._a11yLi.parentNode.removeChild(this._a11yLi);
          }
          this._a11yLi = null;
        }

        // Reflect this edge's effective accessibility into its <li>.
        // hidden=true detaches the <li> from the list so the edge is
        // entirely absent from the mirror.
        // Recreated on the next non-hidden _applyA11y call.
        _applyA11y() {
          const c = this.parent;
          if (!c || !c._a11yEdgesList) return;
          const eff = this._effectiveA11y();
          if (eff.hidden === true) {
            if (this._a11yLi && this._a11yLi.parentNode) {
              this._a11yLi.parentNode.removeChild(this._a11yLi);
            }
            return;
          }
          if (!this._a11yLi) {
            this._a11yLi = document.createElement("li");
            this._a11yLi.id = c._instanceId + "-edge-" + this.id;
            this._a11yLi.setAttribute("role", "listitem");
          }
          if (!this._a11yLi.parentNode) {
            c._a11yEdgesList.appendChild(this._a11yLi);
          }
          this._a11yLi.textContent = eff.label
            + (eff.description ? ". " + String(eff.description) : "");
        }

        // Visual-only style overrides applied during drawSmartBezier.
        // Mirrors node.style() semantics: overrides persist across setColor(),
        // do not mark the canvas dirty for serialization, and are not
        // round-tripped to JSON.
        //
        // Accepted keys (all optional, all per-edge):
        //   width      - multiplier on default stroke width (1 = library
        //                default ≈ 2 screen px; selection still emphasises 2x).
        //   dash       - array of segment lengths (screen-px scaled) for
        //                ctx.setLineDash. Pass [] or null to clear.
        //   dashOffset - number; offset (screen-px scaled) into the dash
        //                pattern. Drive externally for marching ants.
        //   glow       - { color, blur } object enabling a shadow pass
        //                under the stroke. Set null to clear.
        //   cap        - 'butt' | 'round' | 'square' for ctx.lineCap.
        //   arrowSize  - arrowhead size in screen-px (default 8).
        //   arrowStyle - 'arrow' | 'diamond' | 'dot' | 'half' (default 'arrow').
        //   curvature  - multiplier on auto control-point strength.
        //                1 = default, 0 = straight line (control points
        //                snap to endpoints), >1 exaggerates the bend.
        //
        // Usage:
        //   edge.style({ width: 3, dash: [8, 4] })   - set one or more
        //   edge.style({ dash: null })               - clear one override
        //   edge.style(null)                         - clear all
        //   edge.style()                             - return a copy
        style(overrides) {
          if (arguments.length === 0) {
            return this._styleOverrides ? Object.assign({}, this._styleOverrides) : {};
          }
          if (overrides === null) {
            this._styleOverrides = null;
            this.parent._markDirty();
            this.parent.requestDraw();
            return this;
          }
          if (typeof overrides !== 'object') return this;
          if (!this._styleOverrides) this._styleOverrides = {};
          for (const k of Object.keys(overrides)) {
            const v = overrides[k];
            if (v == null) delete this._styleOverrides[k];
            else this._styleOverrides[k] = v;
          }
          if (Object.keys(this._styleOverrides).length === 0) this._styleOverrides = null;
          this.parent._markDirty();
          this.parent.requestDraw();
          return this;
        }

        // Update arrowhead markers. Pass an object with any of {fromEnd, toEnd};
        // each value must be "none" or "arrow". Omitted keys are unchanged.
        setEnds({ fromEnd, toEnd } = {}) {
          const prev = { fromEnd: this.fromEnd, toEnd: this.toEnd };
          let changed = false;
          if (fromEnd !== undefined) {
            const v = fromEnd === "arrow" ? "arrow" : "none";
            if (v !== this.fromEnd) { this.fromEnd = v; changed = true; }
          }
          if (toEnd !== undefined) {
            const v = toEnd === "arrow" ? "arrow" : "none";
            if (v !== this.toEnd) { this.toEnd = v; changed = true; }
          }
          if (changed) {
            this.parent._markDirty();
            this.parent.requestDraw();
            this.parent._emit('edgeUpdate', { edge: this, kind: 'ends', fromEnd: this.fromEnd, toEnd: this.toEnd, prevFromEnd: prev.fromEnd, prevToEnd: prev.toEnd });
          }
          return this;
        }

        setFromSide(side) {
          if (!EDGE_SIDES.has(side)) return this;
          if (side === this.fromSide) return this;
          const prev = this.fromSide;
          this.fromSide = side;
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('edgeUpdate', { edge: this, kind: 'fromSide', fromSide: side, prevFromSide: prev });
          return this;
        }

        setToSide(side) {
          if (!EDGE_SIDES.has(side)) return this;
          if (side === this.toSide) return this;
          const prev = this.toSide;
          this.toSide = side;
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('edgeUpdate', { edge: this, kind: 'toSide', toSide: side, prevToSide: prev });
          return this;
        }

        // Swap from/to. fromEnd/toEnd stay tied to their from/to slots, so
        // an arrow visually flips to the opposite physical endpoint - i.e.
        // the edge's direction is genuinely reversed.
        reverse() {
          const fn = this.fromNode, fs = this.fromSide;
          this.fromNode = this.toNode;
          this.fromSide = this.toSide;
          this.toNode = fn;
          this.toSide = fs;
          // the edge's derived label is "Edge from <src> to <tgt>";
          // swapping flips it. Endpoint summaries also swap roles
          // (the same node moves between "Connected to" and "Connected
          // from" buckets) and need a refresh.
          this._applyA11y();
          if (typeof this.parent._refreshA11yNodeSummary === "function") {
            const { from, to } = this.endpoints();
            this.parent._refreshA11yNodeSummary(from);
            this.parent._refreshA11yNodeSummary(to);
          }
          this.parent._markDirty();
          this.parent.requestDraw();
          this.parent._emit('edgeUpdate', { edge: this, kind: 'reverse' });
          return this;
        }

        // Re-route this edge atomically. All fields optional. Node values may
        // be an id string or a CanvasNode instance. Unknown nodes / invalid
        // sides are silently ignored. Lands as one history step regardless of
        // how many fields change.
        setEndpoints({ fromNode, toNode, fromSide, toSide } = {}) {
          const prev = { fromNode: this.fromNode, toNode: this.toNode, fromSide: this.fromSide, toSide: this.toSide };
          let changed = false;
          if (fromNode !== undefined) {
            const id = (fromNode && typeof fromNode === "object") ? fromNode.id : fromNode;
            if (id != null && this.parent.getNode(id) && id !== this.fromNode) {
              this.fromNode = id;
              changed = true;
            }
          }
          if (toNode !== undefined) {
            const id = (toNode && typeof toNode === "object") ? toNode.id : toNode;
            if (id != null && this.parent.getNode(id) && id !== this.toNode) {
              this.toNode = id;
              changed = true;
            }
          }
          if (fromSide !== undefined && EDGE_SIDES.has(fromSide) && fromSide !== this.fromSide) {
            this.fromSide = fromSide;
            changed = true;
          }
          if (toSide !== undefined && EDGE_SIDES.has(toSide) && toSide !== this.toSide) {
            this.toSide = toSide;
            changed = true;
          }
          if (changed) {
            // endpoints changed - old endpoints lose this edge from
            // their summary, new endpoints gain it. Refresh the union.
            this._applyA11y();
            if (typeof this.parent._refreshA11yNodeSummary === "function") {
              const ids = new Set([
                prev.fromNode, prev.toNode, this.fromNode, this.toNode,
              ]);
              for (const id of ids) {
                this.parent._refreshA11yNodeSummary(this.parent.getNode(id));
              }
            }
            this.parent._markDirty();
            this.parent.requestDraw();
            this.parent._emit('edgeUpdate', { edge: this, kind: 'endpoints', prev });
          }
          return this;
        }

        // World-space bounding rect of the bezier curve. Uses the cached
        // boundingBox populated by drawSmartBezier; falls back to a rough
        // endpoint-to-endpoint rect if the edge has never been drawn.
        bounds() {
          const bb = this.boundingBox;
          if (bb && bb.width != null) {
            return { x: bb.left, y: bb.top, width: bb.width, height: bb.height };
          }
          const { from: a, to: b } = this.endpoints();
          if (!a || !b) return null;
          const p = this.rectEdgePoint(a, "from");
          const q = this.rectEdgePoint(b, "to");
          const x = Math.min(p.x, q.x);
          const y = Math.min(p.y, q.y);
          return { x, y, width: Math.abs(q.x - p.x), height: Math.abs(q.y - p.y) };
        }

        // Resolve both endpoint nodes. Either may be null if the corresponding
        // id no longer exists in the canvas (e.g. mid-delete reattach).
        endpoints() {
          return {
            from: this.parent.getNode(this.fromNode),
            to:   this.parent.getNode(this.toNode),
          };
        }

        // Given one endpoint (id or instance), return the CanvasNode at the
        // other end. Returns null if the argument isn't part of this edge.
        otherEnd(nodeOrId) {
          const id = (nodeOrId && typeof nodeOrId === "object") ? nodeOrId.id : nodeOrId;
          if (id === this.fromNode) return this.parent.getNode(this.toNode) || null;
          if (id === this.toNode)   return this.parent.getNode(this.fromNode) || null;
          return null;
        }

        isSelected() { return this.parent.selectedEdge === this; }

        select() {
          const c = this.parent;
          if (c.selectedEdge === this && c.selectedNodes.length === 0) return this;
          c._setSelection([], this);
          return this;
        }

        deselect() {
          const c = this.parent;
          if (c.selectedEdge !== this) return this;
          c._setSelection(c.selectedNodes, null);
          return this;
        }

        // Wrapper over the internal hit-test for public use. Returns boolean.
        hitTest(x, y) { return !!this.hitEdge(x, y); }

        delete() {
          return this.parent.deleteEdge(this);
        }
      }

      // Offset (world units) from each anchor along its outward normal to the
      // bezier control point. Lifted from the previous inline literal in
      // drawSmartBezier so getBezierPath/pointAt/tangentAt stay in lockstep.
      CanvasEdge.CONTROL_STRENGTH = 160;

      //////////////////////////////////////////////////////////////////////////////////////
      // CameraAPI - canvas.camera namespace //////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class CameraAPI {
        constructor(canvas) {
          this._c = canvas;
          this._activeAnim = null;
        }

        getViewport() {
          const c = this._c;
          return { pan: { x: c.panX, y: c.panY }, scale: c.scale };
        }

        // Internal: defer fn() until the canvas's container has been laid out
        // at least once, so getBoundingClientRect() gives correct dimensions.
        // If we're already past first paint, run synchronously.
        _whenReady(fn) {
          const c = this._c;
          if (c._readyResolved) return fn();
          return c._readyPromise.then(fn);
        }

        // Instant viewport replacement; cancels any in-flight tween.
        setViewport({ pan, scale } = {}) {
          this._cancelActive();
          const c = this._c;
          if (pan) {
            if (pan.x != null) c.panX = pan.x;
            if (pan.y != null) c.panY = pan.y;
          }
          if (scale != null) c.scale = this._clampScale(scale);
          c.updateTransform();
          c.requestDraw();
          c._emitViewportChange();
          return this;
        }

        focusNode(idOrNode, opts = {}) {
          return this._whenReady(() => this._focusNodeNow(idOrNode, opts));
        }
        _focusNodeNow(idOrNode, opts = {}) {
          const c = this._c;
          const node = (idOrNode && typeof idOrNode === "object") ? idOrNode : c.getNode(idOrNode);
          if (!node) return Promise.resolve();
          const rect = c.container.getBoundingClientRect();
          let targetScale;
          if (opts.zoom != null) {
            targetScale = this._clampScale(opts.zoom);
          } else if (opts.padding != null) {
            // Padding implies "fit this node with this much breathing room."
            const padding = opts.padding;
            const availW = Math.max(1, rect.width  - 2 * padding);
            const availH = Math.max(1, rect.height - 2 * padding);
            targetScale = this._clampScale(Math.min(availW / Math.max(1, node.width), availH / Math.max(1, node.height)));
          } else {
            targetScale = c.scale;
          }
          const cx = node.x + node.width / 2;
          const cy = node.y + node.height / 2;
          const targetPanX = rect.width  / 2 - cx * targetScale;
          const targetPanY = rect.height / 2 - cy * targetScale;
          return this._tweenTo(targetPanX, targetPanY, targetScale, opts);
        }

        fitAll(opts = {}) {
          return this._whenReady(() => this._fitNodes(this._c._nodes, opts));
        }

        fitToSelection(opts = {}) {
          return this._whenReady(() => this._fitNodes(this._c.selectedNodes, opts));
        }

        // "Frame what the user cares about." Fits the current selection if
        // there is one (nodes preferred, otherwise the selected edge's
        // bounding rect), and falls back to fitAll() when nothing is selected.
        // This is the most common framing operation a host UI needs, so it
        // gets its own method to spare every consumer the same three-branch
        // boilerplate.
        fitSelectionOrAll(opts = {}) {
          return this._whenReady(() => {
            const c = this._c;
            if (c.selectedNodes && c.selectedNodes.length) {
              return this._fitNodes(c.selectedNodes, opts);
            }
            if (c.selectedEdge) {
              const b = c.selectedEdge.bounds();
              if (b) return this._fitToRectNow(b, opts);
            }
            return this._fitNodes(c._nodes, opts);
          });
        }

        // Fit to an arbitrary subset. Accepts ids, node instances, or a mix;
        // unknown ids are silently dropped. Resolves with no-op when the
        // resolved set is empty.
        fitToNodes(idsOrNodes, opts = {}) {
          return this._whenReady(() => {
            const c = this._c;
            if (!Array.isArray(idsOrNodes)) idsOrNodes = [idsOrNodes];
            const nodes = [];
            for (const item of idsOrNodes) {
              if (!item) continue;
              if (typeof item === "object" && c._nodes.includes(item)) { nodes.push(item); continue; }
              const id = (typeof item === "object" && item.id) ? item.id : item;
              const n = c.getNode(id);
              if (n) nodes.push(n);
            }
            return this._fitNodes(nodes, opts);
          });
        }

        // Fit to an arbitrary world rect ({x, y, width, height}). Padding is
        // in screen pixels.
        fitToRect(rect, opts = {}) {
          if (!rect) return Promise.resolve();
          return this._whenReady(() => this._fitToRectNow(rect, opts));
        }
        _fitToRectNow(rect, opts = {}) {
          const c = this._c;
          const padding = opts.padding != null ? opts.padding : 40;
          const w = Math.max(1, rect.width);
          const h = Math.max(1, rect.height);
          const view = c.container.getBoundingClientRect();
          const availW = Math.max(1, view.width  - 2 * padding);
          const availH = Math.max(1, view.height - 2 * padding);
          const targetScale = this._clampScale(Math.min(availW / w, availH / h));
          const cx = rect.x + w / 2;
          const cy = rect.y + h / 2;
          const targetPanX = view.width  / 2 - cx * targetScale;
          const targetPanY = view.height / 2 - cy * targetScale;
          return this._tweenTo(targetPanX, targetPanY, targetScale, opts);
        }

        // Pan to center a world point on screen without changing zoom.
        centerOn(x, y, opts = {}) {
          return this._whenReady(() => {
            const c = this._c;
            const view = c.container.getBoundingClientRect();
            const targetPanX = view.width  / 2 - x * c.scale;
            const targetPanY = view.height / 2 - y * c.scale;
            return this._tweenTo(targetPanX, targetPanY, c.scale, opts);
          });
        }

        // World → screen. Inverse of Canvas.toWorld(x, y).
        toScreen(x, y) {
          const c = this._c;
          return { x: x * c.scale + c.panX, y: y * c.scale + c.panY };
        }

        // World rect → screen rect.
        rectToScreen(r) {
          const c = this._c;
          return {
            x: r.x * c.scale + c.panX,
            y: r.y * c.scale + c.panY,
            width:  r.width  * c.scale,
            height: r.height * c.scale,
          };
        }

        panTo(x, y, opts = {}) {
          return this._tweenTo(x, y, this._c.scale, opts);
        }

        zoomTo(scale, opts = {}) {
          return this._whenReady(() => {
            const c = this._c;
            const targetScale = this._clampScale(scale);
            const rect = c.container.getBoundingClientRect();
            const anchor = opts.anchor || { x: rect.width / 2, y: rect.height / 2 };
            // Keep anchor fixed in screen space: pan' = anchor - (anchor - pan) * (target/current)
            const ratio = targetScale / c.scale;
            const targetPanX = anchor.x - (anchor.x - c.panX) * ratio;
            const targetPanY = anchor.y - (anchor.y - c.panY) * ratio;
            return this._tweenTo(targetPanX, targetPanY, targetScale, opts);
          });
        }

        // Public viewport tween. Two modes:
        //   - No onFrame: interpolates pan + scale linearly to {pan, scale}.
        //     Pass through to the shared _tweenTo path used by focusNode/fitAll.
        //   - With onFrame(rawT, easedT): bypasses built-in pan/scale interp
        //     and lets the caller drive the viewport each RAF tick. Used by
        //     flyAlongBezier and any consumer that needs a non-linear path.
        // Returns a Promise resolved on completion (or on cancellation by a
        // newer animateTo/_tweenTo).
        animateTo(opts = {}) {
          const c = this._c;
          const onFrame = typeof opts.onFrame === 'function' ? opts.onFrame : null;
          const onDone  = typeof opts.onDone  === 'function' ? opts.onDone  : null;

          if (!onFrame) {
            const pan = opts.pan || {};
            const targetPanX = pan.x != null ? pan.x : c.panX;
            const targetPanY = pan.y != null ? pan.y : c.panY;
            const targetScale = opts.scale != null ? this._clampScale(opts.scale) : c.scale;
            const p = this._tweenTo(targetPanX, targetPanY, targetScale, opts);
            return onDone ? p.then(onDone) : p;
          }

          this._cancelActive();
          const duration = opts.duration != null ? opts.duration : DEFAULT_ANIM_MS;
          const easeFn = this._easingFn(opts.easing || 'ease-out');
          if (duration <= 0) {
            onFrame(1, 1);
            if (onDone) onDone();
            return Promise.resolve();
          }
          const t0 = performance.now();
          c._beginFastDraw();
          return new Promise(resolve => {
            const anim = { rafId: 0, cancelled: false, resolve, _owesFastDraw: true };
            const step = (now) => {
              if (anim.cancelled) return;
              const t = Math.min(1, (now - t0) / duration);
              const e = easeFn(t);
              // Reentrancy guard: a consumer calling camera.setViewport from
              // inside onFrame would otherwise hit _cancelActive and kill the
              // animation we're currently stepping.
              this._steppingAnim = anim;
              try { onFrame(t, e); } finally { this._steppingAnim = null; }
              if (anim.cancelled) return;
              if (t < 1) {
                anim.rafId = requestAnimationFrame(step);
              } else {
                if (anim._owesFastDraw) {
                  anim._owesFastDraw = false;
                  c._endFastDraw();
                }
                c.requestDraw();
                this._activeAnim = null;
                if (onDone) onDone();
                resolve();
              }
            };
            anim.rafId = requestAnimationFrame(step);
            this._activeAnim = anim;
          });
        }

        // Sugar: tween the camera along a cubic bezier in world space while
        // interpolating scale, keeping (x(u), y(u)) centered in the viewport.
        // P0..P3 mirror the layout of CanvasEdge.getBezierPath(). Snapshots
        // the container rect at start, matching focusNode/_fitNodes behavior.
        flyAlongBezier(opts = {}) {
          const { P0, P1, P2, P3 } = opts;
          if (!P0 || !P1 || !P2 || !P3) return Promise.resolve();
          return this._whenReady(() => this._flyAlongBezierNow(opts));
        }
        _flyAlongBezierNow(opts = {}) {
          const { P0, P1, P2, P3 } = opts;
          const c = this._c;
          const sA = this._clampScale(opts.startScale != null ? opts.startScale : c.scale);
          const sB = this._clampScale(opts.endScale   != null ? opts.endScale   : c.scale);
          const view = c.container.getBoundingClientRect();
          return this.animateTo({
            duration: opts.duration,
            easing:   opts.easing,
            onDone:   opts.onDone,
            onFrame: (_t, u) => {
              const m = 1 - u;
              const x = m*m*m*P0.x + 3*m*m*u*P1.x + 3*m*u*u*P2.x + u*u*u*P3.x;
              const y = m*m*m*P0.y + 3*m*m*u*P1.y + 3*m*u*u*P2.y + u*u*u*P3.y;
              const s = sA + (sB - sA) * u;
              c.panX  = view.width  / 2 - x * s;
              c.panY  = view.height / 2 - y * s;
              c.scale = s;
              c.updateTransform();
              c.requestDraw();
              c._emitViewportChange();
            },
          });
        }

        // Fly the camera along an existing edge's rendered curve. The edge's
        // P0/P3 sit on the node side-anchors (not centers), so we rebuild
        // the endpoints from node.center() - otherwise the camera lands on
        // a node's border instead of the node itself. Reversing swaps the
        // control handles so the first handle stays anchored near the
        // starting node.
        flyAlongEdge(edgeOrId, opts = {}) {
          return this._whenReady(() => this._flyAlongEdgeNow(edgeOrId, opts));
        }
        _flyAlongEdgeNow(edgeOrId, opts = {}) {
          const c = this._c;
          const edge = (typeof edgeOrId === 'string') ? c.getEdge(edgeOrId) : edgeOrId;
          if (!edge) return Promise.resolve();
          const fromNode = c.getNode(edge.fromNode);
          const toNode   = c.getNode(edge.toNode);
          if (!fromNode || !toNode) return Promise.resolve();

          const reversed = !!opts.reversed;
          const a = reversed ? toNode   : fromNode;
          const b = reversed ? fromNode : toNode;
          const { P1, P2 } = edge.getBezierPath();
          const padding = opts.padding != null ? opts.padding : 40;
          return this.flyAlongBezier({
            P0: a.center(),
            P1: reversed ? P2 : P1,
            P2: reversed ? P1 : P2,
            P3: b.center(),
            startScale: opts.startScale != null ? opts.startScale : this._scaleToFit(a, padding),
            endScale:   opts.endScale   != null ? opts.endScale   : this._scaleToFit(b, padding),
            duration: opts.duration,
            easing:   opts.easing,
            onDone:   opts.onDone,
          });
        }

        // Fly between two nodes. With useEdge (default), reuses an existing
        // edge in either direction via graph.findEdge - orientation handled
        // automatically. Without an edge (or useEdge:false), synthesizes a
        // virtual bezier whose curve mirrors what a rendered edge between
        // these two nodes would look like.
        flyBetween(fromNode, toNode, opts = {}) {
          return this._whenReady(() => this._flyBetweenNow(fromNode, toNode, opts));
        }
        _flyBetweenNow(fromNode, toNode, opts = {}) {
          const c = this._c;
          const from = (typeof fromNode === 'string') ? c.getNode(fromNode) : fromNode;
          const to   = (typeof toNode   === 'string') ? c.getNode(toNode)   : toNode;
          if (!from || !to) return Promise.resolve();

          if (opts.useEdge !== false) {
            const edge = c.graph.findEdge(from.id, to.id);
            if (edge) {
              return this.flyAlongEdge(edge, {
                ...opts,
                reversed: edge.fromNode !== from.id,
              });
            }
          }

          const virt = this._computeVirtualBezier(from, to);
          const padding = opts.padding != null ? opts.padding : 40;
          return this.flyAlongBezier({
            P0: from.center(),
            P1: virt.ctrl1,
            P2: virt.ctrl2,
            P3: to.center(),
            startScale: opts.startScale != null ? opts.startScale : this._scaleToFit(from, padding),
            endScale:   opts.endScale   != null ? opts.endScale   : this._scaleToFit(to,   padding),
            duration: opts.duration,
            easing:   opts.easing,
            onDone:   opts.onDone,
          });
        }

        // Pick natural-facing sides via the same axis-dominance test as
        // rectBorderPoint(), then offset control points by
        // CanvasEdge.CONTROL_STRENGTH so a virtual flight curve has the same
        // shape character as a real edge would.
        _computeVirtualBezier(fromNode, toNode) {
          const pickSide = (a, b) => {
            const acx = a.x + a.width / 2, acy = a.y + a.height / 2;
            const bcx = b.x + b.width / 2, bcy = b.y + b.height / 2;
            const dx = bcx - acx, dy = bcy - acy;
            const hw = Math.max(1, a.width  / 2);
            const hh = Math.max(1, a.height / 2);
            if (Math.abs(dx) / hw > Math.abs(dy) / hh) return dx > 0 ? 'right' : 'left';
            return dy > 0 ? 'bottom' : 'top';
          };
          const sideVec = (side) => ({
            dx: side === 'left' ? -1 : side === 'right' ? 1 : 0,
            dy: side === 'top'  ? -1 : side === 'bottom' ? 1 : 0,
          });

          const f = sideVec(pickSide(fromNode, toNode));
          const t = sideVec(pickSide(toNode,   fromNode));
          const fc = fromNode.center(), tc = toNode.center();
          const start = { x: fc.x + f.dx * fromNode.width / 2, y: fc.y + f.dy * fromNode.height / 2, dx: f.dx, dy: f.dy };
          const end   = { x: tc.x + t.dx * toNode.width   / 2, y: tc.y + t.dy * toNode.height   / 2, dx: t.dx, dy: t.dy };
          return CanvasEdge._bezierControls(start, end);
        }

        _scaleToFit(node, padding) {
          const view = this._c.container.getBoundingClientRect();
          const availW = Math.max(1, view.width  - 2 * padding);
          const availH = Math.max(1, view.height - 2 * padding);
          const w = Math.max(1, node.width);
          const h = Math.max(1, node.height);
          return this._clampScale(Math.min(availW / w, availH / h));
        }

        _fitNodes(nodes, opts) {
          const c = this._c;
          if (!nodes || !nodes.length) return Promise.resolve();
          const padding = opts && opts.padding != null ? opts.padding : 40;
          let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
          for (const n of nodes) {
            if (n.x < minX) minX = n.x;
            if (n.y < minY) minY = n.y;
            if (n.x + n.width  > maxX) maxX = n.x + n.width;
            if (n.y + n.height > maxY) maxY = n.y + n.height;
          }
          const bbW = Math.max(1, maxX - minX);
          const bbH = Math.max(1, maxY - minY);
          const rect = c.container.getBoundingClientRect();
          const availW = Math.max(1, rect.width  - 2 * padding);
          const availH = Math.max(1, rect.height - 2 * padding);
          const targetScale = this._clampScale(Math.min(availW / bbW, availH / bbH));
          const cx = (minX + maxX) / 2;
          const cy = (minY + maxY) / 2;
          const targetPanX = rect.width  / 2 - cx * targetScale;
          const targetPanY = rect.height / 2 - cy * targetScale;
          return this._tweenTo(targetPanX, targetPanY, targetScale, opts || {});
        }

        _tweenTo(targetPanX, targetPanY, targetScale, opts) {
          const c = this._c;
          this._cancelActive();
          const duration = opts.duration != null ? opts.duration : DEFAULT_ANIM_MS;
          const easeFn = this._easingFn(opts.easing || 'ease-out');
          if (duration <= 0) {
            c.panX = targetPanX; c.panY = targetPanY; c.scale = targetScale;
            c.updateTransform();
            c.requestDraw();
            c._emitViewportChange();
            return Promise.resolve();
          }
          const startPanX = c.panX, startPanY = c.panY, startScale = c.scale;
          const t0 = performance.now();
          c._beginFastDraw();
          return new Promise(resolve => {
            const anim = { rafId: 0, cancelled: false, resolve, _owesFastDraw: true };
            const step = (now) => {
              if (anim.cancelled) return;
              const t = Math.min(1, (now - t0) / duration);
              const e = easeFn(t);
              c.panX  = startPanX + (targetPanX  - startPanX) * e;
              c.panY  = startPanY + (targetPanY  - startPanY) * e;
              c.scale = startScale + (targetScale - startScale) * e;
              c.updateTransform();
              c.requestDraw();
              c._emitViewportChange();
              if (t < 1) {
                anim.rafId = requestAnimationFrame(step);
              } else {
                if (anim._owesFastDraw) {
                  anim._owesFastDraw = false;
                  c._endFastDraw();
                }
                c.requestDraw();
                this._activeAnim = null;
                resolve();
              }
            };
            anim.rafId = requestAnimationFrame(step);
            this._activeAnim = anim;
          });
        }

        _cancelActive() {
          const a = this._activeAnim;
          if (!a) return;
          // Called from inside our own onFrame (e.g. consumer ran
          // camera.setViewport): keep the animation alive.
          if (a === this._steppingAnim) return;
          a.cancelled = true;
          if (a.rafId) cancelAnimationFrame(a.rafId);
          if (a._owesFastDraw) {
            a._owesFastDraw = false;
            this._c._endFastDraw();
          }
          this._activeAnim = null;
          a.resolve();
        }

        // Public wrapper for callers that need to mirror the camera's own
        // min/max bounds (e.g. computing a "fit" scale outside the library
        // and feeding it back into setViewport).
        clampScale(s) { return this._clampScale(s); }

        _clampScale(s) { return Math.min(Math.max(s, 0.2), 5); }

        _easingFn(name) {
          return resolveEasing(name);
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // GraphAPI - canvas.graph namespace ////////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class GraphAPI {
        constructor(canvas) {
          this._c = canvas;
        }

        // Live-instance accessors. Defensive copy of the array (not the elements),
        // so callers can iterate/mutate the result without disturbing the canvas.
        allNodes() { return this._c._nodes.slice(); }
        allEdges() { return this._c._edges.slice(); }

        findNodes(predicate) {
          if (typeof predicate !== "function") return [];
          return this._c._nodes.filter(predicate);
        }

        findEdges(predicate) {
          if (typeof predicate !== "function") return [];
          return this._c._edges.filter(predicate);
        }

        edgesOf(idOrNode, opts = {}) {
          const id = this._idOf(idOrNode);
          if (id == null) return [];
          const dir = opts.direction || 'both';
          return this._c._edges.filter(e => {
            if (dir === 'out')  return e.fromNode === id;
            if (dir === 'in')   return e.toNode   === id;
            return e.fromNode === id || e.toNode === id;
          });
        }

        // Find an edge between two nodes regardless of direction. Returns the
        // edge instance or null. Callers derive orientation via edge.fromNode.
        findEdge(a, b) {
          const aId = this._idOf(a);
          const bId = this._idOf(b);
          if (aId == null || bId == null) return null;
          for (const e of this._c._edges) {
            if ((e.fromNode === aId && e.toNode === bId) ||
                (e.fromNode === bId && e.toNode === aId)) return e;
          }
          return null;
        }

        neighbors(idOrNode, opts = {}) {
          const id = this._idOf(idOrNode);
          if (id == null) return [];
          const dir = opts.direction || 'both';
          const nodeMap = this._nodeMap();
          const out = [];
          const seen = new Set();
          for (const e of this._c._edges) {
            let otherId = null;
            if (e.fromNode === id && (dir === 'out' || dir === 'both')) otherId = e.toNode;
            else if (e.toNode === id && (dir === 'in' || dir === 'both')) otherId = e.fromNode;
            if (otherId != null && otherId !== id && !seen.has(otherId)) {
              const n = nodeMap.get(otherId);
              if (n) { out.push(n); seen.add(otherId); }
            }
          }
          return out;
        }

        // BFS over edges. By default edges are treated as undirected; pass
        // {direction:'out'} or {direction:'in'} for a directed search.
        shortestPath(aId, bId, opts = {}) {
          const a = this._idOf(aId);
          const b = this._idOf(bId);
          if (a == null || b == null) return null;
          const nodeMap = this._nodeMap();
          if (!nodeMap.has(a) || !nodeMap.has(b)) return null;
          if (a === b) { const n = nodeMap.get(a); return n ? [n] : null; }

          const dir = opts.direction || 'both';
          const adj = this._adjacency(dir);
          const prev = new Map();
          const queue = [a];
          prev.set(a, null);
          while (queue.length) {
            const cur = queue.shift();
            if (cur === b) break;
            const nbrs = adj.get(cur) || [];
            for (const next of nbrs) {
              if (prev.has(next)) continue;
              prev.set(next, cur);
              queue.push(next);
            }
          }
          if (!prev.has(b)) return null;
          const path = [];
          let cur = b;
          while (cur != null) { path.unshift(nodeMap.get(cur)); cur = prev.get(cur); }
          return path;
        }

        // Text search across configurable node fields. `query` may be a
        // string (case-insensitive substring by default) or a RegExp.
        // opts.fields defaults to ['text','label','file','url']. opts.caseSensitive
        // makes a string match exact-case. Returns matching node instances.
        search(query, opts = {}) {
          if (query == null || query === "") return [];
          const fields = opts.fields || ['text', 'label', 'file', 'url'];
          const caseSensitive = !!opts.caseSensitive;
          let test;
          if (query instanceof RegExp) {
            test = (s) => query.test(s);
          } else {
            const q = caseSensitive ? String(query) : String(query).toLowerCase();
            test = (s) => (caseSensitive ? s : s.toLowerCase()).indexOf(q) !== -1;
          }
          return this._c._nodes.filter(n => {
            for (const f of fields) {
              const v = n[f];
              if (v != null && test(String(v))) return true;
            }
            return false;
          });
        }

        // Transitive predecessors (nodes that can reach idOrNode via outgoing
        // edges, walked backwards). opts.depth caps the walk (default Infinity).
        // Self is excluded.
        ancestors(idOrNode, opts = {}) {
          return this._transitive(idOrNode, 'in', opts);
        }

        // Transitive successors (nodes reachable from idOrNode via outgoing
        // edges). opts.depth caps the walk (default Infinity). Self is excluded.
        descendants(idOrNode, opts = {}) {
          return this._transitive(idOrNode, 'out', opts);
        }

        // Nodes with no incoming edges.
        roots() {
          const has = new Set();
          for (const e of this._c._edges) has.add(e.toNode);
          return this._c._nodes.filter(n => !has.has(n.id));
        }

        // Nodes with no outgoing edges.
        leaves() {
          const has = new Set();
          for (const e of this._c._edges) has.add(e.fromNode);
          return this._c._nodes.filter(n => !has.has(n.id));
        }

        // Edge count incident to a node. opts.direction is 'in'|'out'|'both'
        // (default 'both'). Self-loops count once per side under 'both'.
        degree(idOrNode, opts = {}) {
          const id = this._idOf(idOrNode);
          if (id == null) return 0;
          const dir = opts.direction || 'both';
          let n = 0;
          for (const e of this._c._edges) {
            if ((dir === 'out' || dir === 'both') && e.fromNode === id) n++;
            if ((dir === 'in'  || dir === 'both') && e.toNode   === id) n++;
          }
          return n;
        }

        // Partition the graph into undirected connected components. Returns
        // an array of node-instance arrays. Isolated nodes form their own
        // 1-element component.
        connectedComponents() {
          const adj = this._adjacency('both');
          const nodeMap = this._nodeMap();
          const seen = new Set();
          const components = [];
          for (const start of this._c._nodes) {
            if (seen.has(start.id)) continue;
            const comp = [];
            const queue = [start.id];
            seen.add(start.id);
            while (queue.length) {
              const cur = queue.shift();
              const n = nodeMap.get(cur);
              if (n) comp.push(n);
              for (const next of adj.get(cur) || []) {
                if (seen.has(next)) continue;
                seen.add(next);
                queue.push(next);
              }
            }
            components.push(comp);
          }
          return components;
        }

        // Kahn's algorithm - returns an ordering where every edge u→v has
        // u appearing before v. Returns null if the graph has a cycle.
        topologicalSort() {
          const c = this._c;
          const nodeMap = this._nodeMap();
          const inDeg = new Map();
          for (const n of c._nodes) inDeg.set(n.id, 0);
          for (const e of c._edges) {
            if (inDeg.has(e.toNode)) inDeg.set(e.toNode, inDeg.get(e.toNode) + 1);
          }
          const queue = [];
          for (const [id, d] of inDeg) if (d === 0) queue.push(id);
          const adj = this._adjacency('out');
          const out = [];
          while (queue.length) {
            const cur = queue.shift();
            const n = nodeMap.get(cur);
            if (n) out.push(n);
            for (const next of adj.get(cur) || []) {
              const d = inDeg.get(next) - 1;
              inDeg.set(next, d);
              if (d === 0) queue.push(next);
            }
          }
          return out.length === c._nodes.length ? out : null;
        }

        // True if directed cycle exists.
        hasCycle() {
          return this.topologicalSort() === null;
        }

        // BFS to `depth` hops from root, returning the induced subgraph
        // (nodes within range + edges that connect two in-range nodes).
        subgraph(rootId, depth = 1, opts = {}) {
          const r = this._idOf(rootId);
          if (r == null) return { nodes: [], edges: [] };
          const nodeMap = this._nodeMap();
          if (!nodeMap.has(r)) return { nodes: [], edges: [] };

          const dir = opts.direction || 'both';
          const adj = this._adjacency(dir);
          const reached = new Map();
          reached.set(r, 0);
          const queue = [r];
          while (queue.length) {
            const cur = queue.shift();
            const d = reached.get(cur);
            if (d >= depth) continue;
            const nbrs = adj.get(cur) || [];
            for (const next of nbrs) {
              if (reached.has(next)) continue;
              reached.set(next, d + 1);
              queue.push(next);
            }
          }
          const nodes = [];
          for (const id of reached.keys()) {
            const n = nodeMap.get(id);
            if (n) nodes.push(n);
          }
          const edges = this._c._edges.filter(e => reached.has(e.fromNode) && reached.has(e.toNode));
          return { nodes, edges };
        }

        // ---- internals ----
        // Shared engine for ancestors/descendants. `direction` is 'in' or 'out'.
        _transitive(idOrNode, direction, opts = {}) {
          const id = this._idOf(idOrNode);
          if (id == null) return [];
          const nodeMap = this._nodeMap();
          if (!nodeMap.has(id)) return [];
          const maxDepth = opts.depth != null ? opts.depth : Infinity;
          const adj = this._adjacency(direction);
          const seen = new Set([id]);
          const queue = [{ id, d: 0 }];
          const out = [];
          while (queue.length) {
            const { id: cur, d } = queue.shift();
            if (d >= maxDepth) continue;
            for (const next of adj.get(cur) || []) {
              if (seen.has(next)) continue;
              seen.add(next);
              const n = nodeMap.get(next);
              if (n) out.push(n);
              queue.push({ id: next, d: d + 1 });
            }
          }
          return out;
        }

        _idOf(idOrNode) {
          if (idOrNode == null) return null;
          if (typeof idOrNode === "object") return idOrNode.id != null ? idOrNode.id : null;
          return idOrNode;
        }

        _nodeMap() {
          const m = new Map();
          for (const n of this._c._nodes) m.set(n.id, n);
          return m;
        }

        // One-shot adjacency list keyed by node id. Direction-aware:
        //   'out'  → only outgoing edges (fromNode → toNode)
        //   'in'   → only incoming edges (toNode → fromNode, reversed walk)
        //   'both' → undirected (edges traversable in either direction)
        _adjacency(direction) {
          const adj = new Map();
          for (const n of this._c._nodes) adj.set(n.id, []);
          for (const e of this._c._edges) {
            if (direction === 'out' || direction === 'both') {
              const a = adj.get(e.fromNode); if (a) a.push(e.toNode);
            }
            if (direction === 'in' || direction === 'both') {
              const a = adj.get(e.toNode); if (a) a.push(e.fromNode);
            }
          }
          return adj;
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // LayoutAPI - canvas.layout namespace //////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class LayoutAPI {
        constructor(canvas) { this._c = canvas; }

        // auto(algorithm, opts) - apply a built-in layout.
        //   algorithm: 'grid' | 'tree' | 'force'
        //   opts: per-algorithm. Common: duration, x, y, nodes (subset).
        auto(algorithm, opts = {}) {
          switch (algorithm) {
            case 'grid':  return this._grid(opts);
            case 'tree':  return this._tree(opts);
            case 'force': return this._force(opts);
            default:
              throw new Error("canvas.layout.auto: unknown algorithm '" + algorithm + "'");
          }
        }

        // ---- grid ----
        // Options: nodes (default all), cols (default ceil(sqrt(n))), gap,
        // cellWidth/cellHeight (default = largest node bbox), x, y, sortBy.
        _grid(opts) {
          const c = this._c;
          const nodes = opts.nodes ? c._resolveNodes(opts.nodes) : c._nodes.slice();
          if (!nodes.length) return Promise.resolve();
          const gap = opts.gap != null ? opts.gap : 30;
          let cellW = opts.cellWidth, cellH = opts.cellHeight;
          if (cellW == null || cellH == null) {
            let mw = 0, mh = 0;
            for (const n of nodes) {
              if (n.width  > mw) mw = n.width;
              if (n.height > mh) mh = n.height;
            }
            if (cellW == null) cellW = mw;
            if (cellH == null) cellH = mh;
          }
          const cols = opts.cols != null ? Math.max(1, opts.cols | 0) : Math.ceil(Math.sqrt(nodes.length));
          const startX = opts.x != null ? opts.x : 0;
          const startY = opts.y != null ? opts.y : 0;
          const ordered = opts.sortBy ? nodes.slice().sort((a, b) => {
            const va = typeof opts.sortBy === 'function' ? opts.sortBy(a) : a[opts.sortBy];
            const vb = typeof opts.sortBy === 'function' ? opts.sortBy(b) : b[opts.sortBy];
            return va < vb ? -1 : va > vb ? 1 : 0;
          }) : nodes;
          const targets = ordered.map((_, i) => {
            const col = i % cols, row = Math.floor(i / cols);
            return { x: startX + col * (cellW + gap), y: startY + row * (cellH + gap) };
          });
          return this._animate(ordered, targets, opts);
        }

        // ---- tree (tidy layered) ----
        // Options: root (id or instance), direction ('out'|'in'), hGap, vGap,
        // x, y. Cycles get spanning-tree treatment; disconnected nodes are
        // arranged in a trailing row.
        _tree(opts) {
          const c = this._c;
          const allNodes = c._nodes;
          if (!allNodes.length) return Promise.resolve();
          const hGap   = opts.hGap   != null ? opts.hGap   : 40;
          const vGap   = opts.vGap   != null ? opts.vGap   : 80;
          const startX = opts.x      != null ? opts.x      : 0;
          const startY = opts.y      != null ? opts.y      : 0;
          const direction = opts.direction || 'out';

          // Adjacency (parent → children).
          const childMap = new Map();
          for (const n of allNodes) childMap.set(n.id, []);
          for (const e of c._edges) {
            if (direction === 'out') {
              const a = childMap.get(e.fromNode); if (a) a.push(e.toNode);
            } else {
              const a = childMap.get(e.toNode);   if (a) a.push(e.fromNode);
            }
          }

          // Roots.
          let roots;
          if (opts.root) {
            const root = (typeof opts.root === 'object') ? opts.root : c.getNode(opts.root);
            roots = root ? [root] : [];
          } else {
            const incoming = new Set();
            for (const e of c._edges) {
              incoming.add(direction === 'out' ? e.toNode : e.fromNode);
            }
            roots = allNodes.filter(n => !incoming.has(n.id));
            if (!roots.length) roots = [allNodes[0]];
          }

          const targets = new Map();
          const visited = new Set();
          const xCursor = { v: startX };

          // Tidy-tree recursion: place subtree, return center-x of this node.
          const layout = (nodeId, depth) => {
            if (visited.has(nodeId)) return null;
            visited.add(nodeId);
            const node = c.getNode(nodeId);
            if (!node) return null;
            const y = startY + depth * (node.height + vGap);
            const childIds = (childMap.get(nodeId) || []).filter(id => !visited.has(id));
            if (!childIds.length) {
              const x = xCursor.v;
              xCursor.v += node.width + hGap;
              targets.set(nodeId, { x, y });
              return x + node.width / 2;
            }
            const childCenters = [];
            for (const cid of childIds) {
              const cc = layout(cid, depth + 1);
              if (cc != null) childCenters.push(cc);
            }
            if (!childCenters.length) {
              const x = xCursor.v;
              xCursor.v += node.width + hGap;
              targets.set(nodeId, { x, y });
              return x + node.width / 2;
            }
            const left  = Math.min(...childCenters);
            const right = Math.max(...childCenters);
            const x = (left + right) / 2 - node.width / 2;
            targets.set(nodeId, { x, y });
            return x + node.width / 2;
          };

          for (const r of roots) {
            layout(r.id, 0);
            xCursor.v += hGap;  // gap between forests
          }

          // Disconnected / unreachable nodes: trailing row.
          for (const n of allNodes) {
            if (visited.has(n.id)) continue;
            targets.set(n.id, { x: xCursor.v, y: startY });
            xCursor.v += n.width + hGap;
          }

          const ordered = [];
          const positions = [];
          for (const n of allNodes) {
            const t = targets.get(n.id);
            if (t) { ordered.push(n); positions.push(t); }
          }
          return this._animate(ordered, positions, opts);
        }

        // ---- force (spring-directed) ----
        // Iterative spring simulation. Group nodes don't move during the sim
        // (they act as fixed anchors at their center for edges that touch
        // them); after the sim settles, each group's bbox is repositioned to
        // wrap its original (snapshot-time) members with padding.
        //
        // Membership is snapshot at t=0 by bbox-inside-bbox (same rule as
        // _computeGroupDragExtras). Two derived forces use that snapshot:
        //   - co-member "hidden spring": weak attraction between every pair
        //     of bodies that share at least one group.
        //   - soft containment: inward force if a body drifts outside the
        //     frozen bbox of a group it was initially inside.
        // Exclusion of non-members entering foreign groups is intentionally
        // deferred - the snapshot record (`membership`, `groupSnap`) is the
        // architectural seed for that and other future passes.
        //
        // Options: nodes (default all), duration, iterations, minKE, seed,
        // repulsion, springLength, springStrength, gravity, damping,
        // groupSpringStrength, groupSpringLength, containmentStrength,
        // groupPadding, x, y.
        _force(opts = {}) {
          const c = this._c;
          const allNodes = c._nodes;
          if (!allNodes.length) return Promise.resolve();

          // All inter-body distances are measured as the gap between
          // bounding boxes (max of axis-wise edge-to-edge separation), not
          // center-to-center, so node width/height matter. springLength and
          // groupSpringLength therefore mean "target gap" in pixels.
          const iterations          = opts.iterations          != null ? opts.iterations          : 300;
          const minKE               = opts.minKE               != null ? opts.minKE               : 0.05;
          const repulsion           = opts.repulsion           != null ? opts.repulsion           : 1500;
          const springLength        = opts.springLength        != null ? opts.springLength        : 220;
          const springStrength      = opts.springStrength      != null ? opts.springStrength      : 0.04;
          const groupSpringLength   = opts.groupSpringLength   != null ? opts.groupSpringLength   : 160;
          const groupSpringStrength = opts.groupSpringStrength != null ? opts.groupSpringStrength : 0.015;
          const containmentStrength = opts.containmentStrength != null ? opts.containmentStrength : 0.08;
          const overlapResolve      = opts.overlapResolve      != null ? opts.overlapResolve      : 0.5;
          const gravity             = opts.gravity             != null ? opts.gravity             : 0.005;
          const damping             = opts.damping             != null ? opts.damping             : 0.85;
          const groupPadding        = opts.groupPadding        != null ? opts.groupPadding        : 30;
          const maxStep             = opts.maxStep             != null ? opts.maxStep             : 30;
          const liveGroupBounds     = opts.liveGroupBounds     != null ? opts.liveGroupBounds     : true;
          const groupForeignRepulsion = opts.groupForeignRepulsion != null ? opts.groupForeignRepulsion : 3000;
          const groupSeparationStrength = opts.groupSeparationStrength != null ? opts.groupSeparationStrength : 0.25;
          const dt = 1.0;

          // ---- Phase A: snapshot ----
          // Resolve participant set. Groups in this set will be repositioned
          // by the post-pass; non-group entries become free bodies.
          const requested = opts.nodes ? c._resolveNodes(opts.nodes) : allNodes.slice();
          const bodies = [];
          const participantGroups = [];
          for (const n of requested) {
            if (n.type === 'group') {
              participantGroups.push(n);
            } else {
              bodies.push({
                node: n,
                cx: n.x + n.width / 2,
                cy: n.y + n.height / 2,
                vx: 0, vy: 0,
                fx: 0, fy: 0,
                w: n.width, h: n.height,
              });
            }
          }
          if (!bodies.length) return Promise.resolve();

          // All groups (even out-of-scope) snapshot - needed as edge anchors
          // and for membership detection of in-scope bodies.
          const allGroups = allNodes.filter(n => n.type === 'group');
          const groupSnap = new Map();
          for (const g of allGroups) {
            groupSnap.set(g.id, {
              x: g.x, y: g.y, w: g.width, h: g.height,
              cx: g.x + g.width / 2, cy: g.y + g.height / 2,
              memberIds: new Set(),
              // liveBox: per-iteration bbox wrapping current members + padding.
              // For non-participant groups this stays at the t=0 values.
              liveBox: {
                cx: g.x + g.width / 2, cy: g.y + g.height / 2,
                w: g.width, h: g.height,
              },
            });
          }
          const participantGroupIds = new Set();
          for (const g of participantGroups) participantGroupIds.add(g.id);

          // Membership: groups whose initial bbox contains each body.
          // Same predicate as _computeGroupDragExtras.
          const bodyById = new Map();
          for (const b of bodies) bodyById.set(b.node.id, b);
          const membership = new Map();
          for (const b of bodies) {
            const bx = b.cx - b.w / 2, by = b.cy - b.h / 2;
            const memberOf = new Set();
            for (const g of allGroups) {
              const gs = groupSnap.get(g.id);
              if (bx >= gs.x && by >= gs.y &&
                  bx + b.w <= gs.x + gs.w &&
                  by + b.h <= gs.y + gs.h) {
                memberOf.add(g.id);
                gs.memberIds.add(b.node.id);
              }
            }
            membership.set(b.node.id, memberOf);
          }

          // Co-member pairs: every (i,j) of bodies sharing at least one group.
          const coMemberPairs = [];
          for (let i = 0; i < bodies.length; i++) {
            const mi = membership.get(bodies[i].node.id);
            if (!mi.size) continue;
            for (let j = i + 1; j < bodies.length; j++) {
              const mj = membership.get(bodies[j].node.id);
              if (!mj.size) continue;
              let shared = false;
              for (const g of mi) { if (mj.has(g)) { shared = true; break; } }
              if (shared) coMemberPairs.push([i, j]);
            }
          }

          // Edge springs: body-body (bidirectional) or body-group (anchored).
          // Group-group edges are dropped (both endpoints fixed).
          const edgeSprings = [];
          for (const e of c._edges) {
            const fromBody = bodyById.get(e.fromNode);
            const toBody   = bodyById.get(e.toNode);
            const fromGroup = groupSnap.get(e.fromNode);
            const toGroup   = groupSnap.get(e.toNode);
            if (fromBody && toBody) {
              edgeSprings.push({ kind: 'bb', a: fromBody, b: toBody });
            } else if (fromBody && toGroup) {
              edgeSprings.push({ kind: 'bg', body: fromBody, gid: e.toNode,
                gx: toGroup.cx, gy: toGroup.cy, gw: toGroup.w, gh: toGroup.h });
            } else if (toBody && fromGroup) {
              edgeSprings.push({ kind: 'bg', body: toBody, gid: e.fromNode,
                gx: fromGroup.cx, gy: fromGroup.cy, gw: fromGroup.w, gh: fromGroup.h });
            }
          }

          // Gravity center: per-axis override, else centroid of bodies on
          // that axis. Passing only x or only y pins one axis and lets the
          // other auto-center.
          let sx = 0, sy = 0;
          for (const b of bodies) { sx += b.cx; sy += b.cy; }
          const centerX = opts.x != null ? opts.x : sx / bodies.length;
          const centerY = opts.y != null ? opts.y : sy / bodies.length;

          // ---- Phase B: force loop ----
          for (let iter = 0; iter < iterations; iter++) {
            for (const b of bodies) { b.fx = 0; b.fy = 0; }

            // Recompute live bboxes for participating groups from current
            // member positions + groupPadding. Lets foreign-repulsion,
            // group-vs-group separation, and bg edge springs react as the
            // group's footprint shifts during the sim - so the final Phase C
            // resize doesn't end up enclosing nodes that drifted in.
            if (liveGroupBounds) {
              for (const g of participantGroups) {
                const gs = groupSnap.get(g.id);
                if (!gs || !gs.memberIds.size) continue;
                let minX = Infinity, minY = Infinity;
                let maxX = -Infinity, maxY = -Infinity;
                for (const mid of gs.memberIds) {
                  const mb = bodyById.get(mid);
                  if (!mb) continue;
                  const bx = mb.cx - mb.w / 2, by = mb.cy - mb.h / 2;
                  if (bx < minX) minX = bx;
                  if (by < minY) minY = by;
                  if (bx + mb.w > maxX) maxX = bx + mb.w;
                  if (by + mb.h > maxY) maxY = by + mb.h;
                }
                if (minX === Infinity) continue;
                const lw = (maxX - minX) + groupPadding * 2;
                const lh = (maxY - minY) + groupPadding * 2;
                gs.liveBox.cx = minX - groupPadding + lw / 2;
                gs.liveBox.cy = minY - groupPadding + lh / 2;
                gs.liveBox.w  = lw;
                gs.liveBox.h  = lh;
              }
            }

            // Repulsion (Coulomb-like, O(n²)) - gap-based so node sizes
            // matter. gap = max(|dx|-(wA+wB)/2, |dy|-(hA+hB)/2). When gap<0
            // the bboxes overlap and we apply a strong linear separation
            // proportional to the overlap. When gap>=0, Coulomb on the gap.
            // Continuous at gap=0: both branches give `repulsion` there.
            // Deterministic angular nudge handles fully-coincident pairs.
            for (let i = 0; i < bodies.length; i++) {
              for (let j = i + 1; j < bodies.length; j++) {
                const a = bodies[i], bb = bodies[j];
                let dx = a.cx - bb.cx;
                let dy = a.cy - bb.cy;
                let dist2 = dx * dx + dy * dy;
                if (dist2 < 1) {
                  const ang = ((i * 31 + j) % 360) * (Math.PI / 180);
                  dx = Math.cos(ang); dy = Math.sin(ang);
                  dist2 = 1;
                }
                const dist = Math.sqrt(dist2);
                const ux = dx / dist, uy = dy / dist;
                const gapX = Math.abs(dx) - (a.w + bb.w) / 2;
                const gapY = Math.abs(dy) - (a.h + bb.h) / 2;
                const gap  = Math.max(gapX, gapY);
                let f;
                if (gap < 0) {
                  f = (-gap) * overlapResolve + repulsion;
                } else {
                  const r = gap + 1;
                  f = repulsion / (r * r);
                }
                a.fx  += f * ux; a.fy  += f * uy;
                bb.fx -= f * ux; bb.fy -= f * uy;
              }
            }

            // Foreign-body-vs-group repulsion - push any body that isn't a
            // member of group g out of g's live bbox. Uses the same
            // continuous-at-gap=0 formulation as body-body repulsion.
            if (liveGroupBounds) {
              for (const g of participantGroups) {
                const gs = groupSnap.get(g.id);
                if (!gs || !gs.memberIds.size) continue;
                const lb = gs.liveBox;
                const ghw = lb.w / 2, ghh = lb.h / 2;
                for (const b of bodies) {
                  if (gs.memberIds.has(b.node.id)) continue;
                  let dx = b.cx - lb.cx;
                  let dy = b.cy - lb.cy;
                  let dist2 = dx * dx + dy * dy;
                  if (dist2 < 1) {
                    // Body sits exactly on the group center - pick an
                    // arbitrary direction to break the symmetry.
                    dx = 1; dy = 0;
                    dist2 = 1;
                  }
                  const dist = Math.sqrt(dist2);
                  const gapX = Math.abs(dx) - (b.w / 2 + ghw);
                  const gapY = Math.abs(dy) - (b.h / 2 + ghh);
                  const gap  = Math.max(gapX, gapY);
                  let f;
                  if (gap < 0) {
                    f = (-gap) * overlapResolve + groupForeignRepulsion;
                  } else {
                    const r = gap + 1;
                    f = groupForeignRepulsion / (r * r);
                  }
                  // Only the body moves; the group has no velocity. The group
                  // follows along through its members on the next iteration's
                  // live-bbox recompute.
                  b.fx += f * (dx / dist);
                  b.fy += f * (dy / dist);
                }
              }
            }

            // Edge springs - gap-based. f = springStrength*(gap - springLength);
            // negative pushes apart (too close), positive pulls together.
            for (const s of edgeSprings) {
              if (s.kind === 'bb') {
                const a = s.a, b = s.b;
                const dx = b.cx - a.cx, dy = b.cy - a.cy;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const gapX = Math.abs(dx) - (a.w + b.w) / 2;
                const gapY = Math.abs(dy) - (a.h + b.h) / 2;
                const gap  = Math.max(gapX, gapY);
                const f = springStrength * (gap - springLength);
                const ux = dx / dist, uy = dy / dist;
                a.fx += f * ux; a.fy += f * uy;
                b.fx -= f * ux; b.fy -= f * uy;
              } else {
                const b = s.body;
                // Anchor on the live bbox when the group is participating -
                // so edges drag with the group as its footprint shifts.
                let gx = s.gx, gy = s.gy, gw = s.gw, gh = s.gh;
                if (liveGroupBounds && s.gid && participantGroupIds.has(s.gid)) {
                  const lb = groupSnap.get(s.gid).liveBox;
                  gx = lb.cx; gy = lb.cy; gw = lb.w; gh = lb.h;
                }
                const dx = gx - b.cx, dy = gy - b.cy;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const gapX = Math.abs(dx) - (b.w + gw) / 2;
                const gapY = Math.abs(dy) - (b.h + gh) / 2;
                const gap  = Math.max(gapX, gapY);
                const f = springStrength * (gap - springLength);
                b.fx += f * (dx / dist);
                b.fy += f * (dy / dist);
              }
            }

            // Co-member hidden springs - gap-based.
            for (const pair of coMemberPairs) {
              const a = bodies[pair[0]], bb = bodies[pair[1]];
              const dx = bb.cx - a.cx, dy = bb.cy - a.cy;
              const dist = Math.sqrt(dx * dx + dy * dy) || 1;
              const gapX = Math.abs(dx) - (a.w + bb.w) / 2;
              const gapY = Math.abs(dy) - (a.h + bb.h) / 2;
              const gap  = Math.max(gapX, gapY);
              const f = groupSpringStrength * (gap - groupSpringLength);
              const ux = dx / dist, uy = dy / dist;
              a.fx  += f * ux; a.fy  += f * uy;
              bb.fx -= f * ux; bb.fy -= f * uy;
            }

            // Group-vs-group separation - when two participant groups' live
            // bboxes overlap, push them apart along the smaller-overlap axis.
            // Implemented as opposing uniform forces on every member body, so
            // each group translates as a cluster and intra-group layout is
            // preserved.
            if (liveGroupBounds && participantGroups.length > 1) {
              for (let i = 0; i < participantGroups.length; i++) {
                const gsA = groupSnap.get(participantGroups[i].id);
                if (!gsA || !gsA.memberIds.size) continue;
                const lbA = gsA.liveBox;
                for (let j = i + 1; j < participantGroups.length; j++) {
                  const gsB = groupSnap.get(participantGroups[j].id);
                  if (!gsB || !gsB.memberIds.size) continue;
                  const lbB = gsB.liveBox;
                  const dx = lbA.cx - lbB.cx;
                  const dy = lbA.cy - lbB.cy;
                  const overlapX = (lbA.w + lbB.w) / 2 - Math.abs(dx);
                  const overlapY = (lbA.h + lbB.h) / 2 - Math.abs(dy);
                  if (overlapX <= 0 || overlapY <= 0) continue;
                  let fxA = 0, fyA = 0;
                  if (overlapX < overlapY) {
                    const dir = dx >= 0 ? 1 : -1;
                    const f = overlapX * groupSeparationStrength;
                    fxA = f * dir;
                  } else {
                    const dir = dy >= 0 ? 1 : -1;
                    const f = overlapY * groupSeparationStrength;
                    fyA = f * dir;
                  }
                  // Split the impulse: half on each group, applied uniformly
                  // to all members so the group translates as a unit.
                  const halfA = 0.5, halfB = 0.5;
                  for (const mid of gsA.memberIds) {
                    const mb = bodyById.get(mid);
                    if (!mb) continue;
                    mb.fx += fxA * halfA;
                    mb.fy += fyA * halfA;
                  }
                  for (const mid of gsB.memberIds) {
                    const mb = bodyById.get(mid);
                    if (!mb) continue;
                    mb.fx -= fxA * halfB;
                    mb.fy -= fyA * halfB;
                  }
                }
              }
            }

            // Soft containment: keep bodies inside the frozen bbox of each
            // group they originally belonged to.
            for (const b of bodies) {
              const memberOf = membership.get(b.node.id);
              if (!memberOf.size) continue;
              const hw = b.w / 2, hh = b.h / 2;
              for (const gid of memberOf) {
                const g = groupSnap.get(gid);
                const minX = g.x + hw, maxX = g.x + g.w - hw;
                const minY = g.y + hh, maxY = g.y + g.h - hh;
                if (b.cx < minX) b.fx += containmentStrength * (minX - b.cx);
                if (b.cx > maxX) b.fx += containmentStrength * (maxX - b.cx);
                if (b.cy < minY) b.fy += containmentStrength * (minY - b.cy);
                if (b.cy > maxY) b.fy += containmentStrength * (maxY - b.cy);
              }
            }

            // Gravity toward center keeps disconnected components in frame.
            for (const b of bodies) {
              b.fx += gravity * (centerX - b.cx);
              b.fy += gravity * (centerY - b.cy);
            }

            // Integrate + accumulate KE for convergence check. Velocity is
            // capped at maxStep so overlap-resolution spikes can't fling a
            // body across the canvas in a single step.
            let ke = 0;
            for (const b of bodies) {
              b.vx = (b.vx + b.fx * dt) * damping;
              b.vy = (b.vy + b.fy * dt) * damping;
              const sp2 = b.vx * b.vx + b.vy * b.vy;
              if (sp2 > maxStep * maxStep) {
                const k = maxStep / Math.sqrt(sp2);
                b.vx *= k; b.vy *= k;
              }
              b.cx += b.vx * dt;
              b.cy += b.vy * dt;
              ke += b.vx * b.vx + b.vy * b.vy;
            }
            if (iter > 20 && ke < minKE) break;
          }

          // Remove net translation: foreign-group repulsion, bg edge springs,
          // and soft containment are one-sided forces (no Newton reaction on
          // the group anchor), so they inject net momentum and the whole
          // cluster drifts. Gravity is too weak to fully counteract it. Snap
          // the body centroid back to the gravity center so successive force
          // runs don't accumulate drift off the canvas.
          let fsx = 0, fsy = 0;
          for (const b of bodies) { fsx += b.cx; fsy += b.cy; }
          const dxShift = centerX - fsx / bodies.length;
          const dyShift = centerY - fsy / bodies.length;
          if (dxShift || dyShift) {
            for (const b of bodies) { b.cx += dxShift; b.cy += dyShift; }
          }

          // ---- Phase C: post-pass - wrap each participating group around
          // its original members' final positions.
          const targetNodes = [];
          const targetPositions = [];
          const targetSizes = new Map();
          for (const b of bodies) {
            targetNodes.push(b.node);
            targetPositions.push({ x: b.cx - b.w / 2, y: b.cy - b.h / 2 });
          }
          for (const g of participantGroups) {
            const gs = groupSnap.get(g.id);
            if (!gs || !gs.memberIds.size) continue;
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (const mid of gs.memberIds) {
              const b = bodyById.get(mid);
              if (!b) continue;
              const bx = b.cx - b.w / 2, by = b.cy - b.h / 2;
              if (bx < minX) minX = bx;
              if (by < minY) minY = by;
              if (bx + b.w > maxX) maxX = bx + b.w;
              if (by + b.h > maxY) maxY = by + b.h;
            }
            if (minX === Infinity) continue;
            targetNodes.push(g);
            targetPositions.push({ x: minX - groupPadding, y: minY - groupPadding });
            targetSizes.set(g, {
              w: (maxX - minX) + groupPadding * 2,
              h: (maxY - minY) + groupPadding * 2,
            });
          }

          // ---- Phase D: tween, with group resize folded into the same
          // undo step via the finalize callback.
          const finalize = () => {
            for (const [g, sz] of targetSizes) {
              if (g.width === sz.w && g.height === sz.h) continue;
              g.width = sz.w;
              g.height = sz.h;
              g._dom.style.width  = sz.w + "px";
              g._dom.style.height = sz.h + "px";
              if (g._refreshAttached) g._refreshAttached();
            }
          };
          return this._animate(targetNodes, targetPositions, opts, finalize);
        }

        // ---- tweener shared by all algorithms ----
        // Animates each node from its current (x,y) to its target. One
        // _markDirty fires at the end, so the whole layout becomes a single
        // undo step. Optional `finalize` runs after the last _positionAt and
        // before _markDirty, so callers (e.g. force) can stage extra state
        // (group resize) inside the same undo step.
        //
        // Optional `opts.fit` runs a parallel camera tween onto the bounding
        // rect of the target positions, so layout + zoom finish together
        // without the host having to chain or guess a delay. Forms:
        //   fit: true                     - default padding/easing, same duration
        //   fit: { padding, easing,
        //          duration }             - override any/all of those
        _animate(nodes, positions, opts, finalize, sizes) {
          const c = this._c;
          const duration = opts.duration != null ? opts.duration : 400;
          let owesFastDraw = false;

          if (opts.fit && nodes.length) {
            const fitOpts = (typeof opts.fit === 'object') ? opts.fit : {};
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (let i = 0; i < nodes.length; i++) {
              const p = positions[i];
              const w = (sizes && sizes[i]) ? sizes[i].w : nodes[i].width;
              const h = (sizes && sizes[i]) ? sizes[i].h : nodes[i].height;
              if (p.x < minX) minX = p.x;
              if (p.y < minY) minY = p.y;
              if (p.x + w > maxX) maxX = p.x + w;
              if (p.y + h > maxY) maxY = p.y + h;
            }
            if (minX !== Infinity) {
              c.camera.fitToRect(
                { x: minX, y: minY, width: maxX - minX, height: maxY - minY },
                {
                  duration: fitOpts.duration != null ? fitOpts.duration : duration,
                  easing:   fitOpts.easing,
                  padding:  fitOpts.padding,
                }
              );
            }
          }

          const applyFinal = () => {
            for (let i = 0; i < nodes.length; i++) {
              nodes[i]._positionAt(positions[i].x, positions[i].y);
              if (sizes && sizes[i]) {
                nodes[i]._sizeAt(sizes[i].w, sizes[i].h);
              }
            }
            if (finalize) finalize();
            if (owesFastDraw) { owesFastDraw = false; c._endFastDraw(); }
            c.requestDraw();
            c._markDirty();
          };

          if (duration <= 0) {
            applyFinal();
            return Promise.resolve();
          }

          const startPos = nodes.map(n => ({ x: n.x, y: n.y }));
          const startSize = sizes ? nodes.map(n => ({ w: n.width, h: n.height })) : null;
          const t0 = performance.now();
          const easeFn = resolveEasing(opts.easing);
          c._beginFastDraw();
          owesFastDraw = true;
          return new Promise(resolve => {
            const step = (now) => {
              const t = Math.min(1, (now - t0) / duration);
              const e = easeFn(t);
              for (let i = 0; i < nodes.length; i++) {
                const s = startPos[i], p = positions[i];
                nodes[i]._positionAt(s.x + (p.x - s.x) * e, s.y + (p.y - s.y) * e);
                if (sizes && sizes[i]) {
                  const ss = startSize[i], ps = sizes[i];
                  nodes[i]._sizeAt(ss.w + (ps.w - ss.w) * e, ss.h + (ps.h - ss.h) * e);
                }
              }
              c.requestDraw();
              if (t < 1) requestAnimationFrame(step);
              else { applyFinal(); resolve(); }
            };
            requestAnimationFrame(step);
          });
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // SelectionAPI - canvas.selection namespace ////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class SelectionAPI {
        constructor(canvas) { this._c = canvas; }

        get()       { return this._c.getSelection(); }
        set(spec)   { this._c.setSelection(spec); return this; }
        clear()     { this._c.clearSelection(); return this; }

        // Select every node. selectedEdge is singular so we don't try to
        // "select all edges"; pass an explicit edge via set({edges:[...]}) if
        // you need one.
        selectAll() {
          const c = this._c;
          c.setSelection({ nodes: c._nodes.map(n => n.id), edge: null });
          return this;
        }

        // Flip node selection. Edge selection is left untouched.
        invert() {
          const c = this._c;
          const selected = new Set(c.selectedNodes);
          c.setSelection({ nodes: c._nodes.filter(n => !selected.has(n)).map(n => n.id) });
          return this;
        }

        // Clone the currently selected nodes plus any edges connecting them,
        // batched as a single history step. Returns the new node instances.
        // After: selection switches to the duplicates so the caller can chain.
        duplicate(opts = {}) {
          const c = this._c;
          const dx = (opts.offset && opts.offset.x != null) ? opts.offset.x : 20;
          const dy = (opts.offset && opts.offset.y != null) ? opts.offset.y : 20;
          const sources = c.selectedNodes.slice();
          if (!sources.length) return [];
          const idMap = new Map();
          const newNodes = [];
          c.batch(() => {
            for (const n of sources) {
              const dup = n.duplicate({ offset: { x: dx, y: dy } });
              idMap.set(n.id, dup.id);
              newNodes.push(dup);
            }
            // Edges entirely inside the duplicated set get cloned too.
            for (const e of c._edges.slice()) {
              if (idMap.has(e.fromNode) && idMap.has(e.toNode)) {
                const edgeData = {
                  ...(e._extraData || {}),
                  fromNode: idMap.get(e.fromNode),
                  toNode:   idMap.get(e.toNode),
                  fromSide: e.fromSide,
                  toSide:   e.toSide,
                  fromEnd:  e.fromEnd,
                  toEnd:    e.toEnd,
                  label:    e.edge_label || undefined,
                  color:    (e.color && e.color !== "default") ? e.color : undefined,
                };
                if (e._a11y_authored && Object.keys(e._a11y_authored).length > 0) {
                  edgeData.accessibility = { ...e._a11y_authored };
                }
                c.createEdge(edgeData);
              }
            }
            c.setSelection({ nodes: newNodes.map(n => n.id), edge: null });
          });
          return newNodes;
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // IoAPI - canvas.io namespace //////////////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class IoAPI {
        constructor(canvas) { this._c = canvas; }

        // Serialize one live node back to .canvas node-data. _extraData
        // (unknown passthrough keys) is spread first so it round-trips.
        // Accessibility : write authored a11y fields back as a top-level
        // `accessibility` key. Derived/hint-sourced values are intentionally
        // NOT persisted - they reproduce on next load and shouldn't outlive
        // content changes.
        _nodeToData(n) {
          const out = { ...(n._extraData || {}) };
          out.id = n.id;
          out.x = n.x;
          out.y = n.y;
          out.width = n.width;
          out.height = n.height;
          out.type = n.type;
          if (n.text) out.text = n.text;
          if (n.label != null) out.label = n.label;
          if (n.file != null) out.file = n.file;
          if (n.type === "link" && n.url != null) out.url = n.url;
          if (n.type === "group" && n.background) {
            out.background = n.background;
            out.backgroundStyle = n.backgroundStyle || "cover";
          }
          if (n.color && n.color !== "default") out.color = n.color;
          if (n._a11y_authored && Object.keys(n._a11y_authored).length > 0) {
            out.accessibility = { ...n._a11y_authored };
          }
          return out;
        }

        _edgeToData(e) {
          const out = { ...(e._extraData || {}) };
          out.id = e.id;
          out.fromNode = e.fromNode;
          out.toNode = e.toNode;
          out.fromSide = e.fromSide;
          out.toSide = e.toSide;
          if (e.fromEnd && e.fromEnd !== "none") out.fromEnd = e.fromEnd;
          if (e.toEnd && e.toEnd !== "arrow") out.toEnd = e.toEnd;
          if (e.edge_label) out.label = e.edge_label;
          if (e.color && e.color !== "default") out.color = e.color;
          if (e._a11y_authored && Object.keys(e._a11y_authored).length > 0) {
            out.accessibility = { ...e._a11y_authored };
          }
          return out;
        }

        toData() {
          const c = this._c;
          return {
            nodes: c._nodes.map(n => this._nodeToData(n)),
            edges: c._edges.map(e => this._edgeToData(e)),
          };
        }

        toJSON() { return this.toData(); }

        toJSONString(indent = 2) {
          return JSON.stringify(this.toData(), null, indent);
        }

        // Merge a canvas data fragment ({nodes, edges}) into this canvas.
        // opts.offset {x,y} translates incoming positions.
        // opts.idStrategy: 'remap' (default) gives everything fresh ids and
        // rewrites edge refs; 'preserve' keeps incoming ids and remaps only on
        // collision. Returns { nodes, edges } of the newly created instances.
        mergeData(data, opts = {}) {
          const c = this._c;
          if (!data || (!data.nodes && !data.edges)) return { nodes: [], edges: [] };
          const offset = opts.offset || { x: 0, y: 0 };
          const strategy = opts.idStrategy === 'preserve' ? 'preserve' : 'remap';
          const idMap = new Map();
          const newNodes = [];
          const newEdges = [];
          c.batch(() => {
            for (const nd of (data.nodes || [])) {
              const oldId = nd.id;
              let id;
              if (strategy === 'remap') id = c._newId();
              else id = (oldId && !c.getNode(oldId)) ? oldId : c._newId();
              if (oldId) idMap.set(oldId, id);
              const node = c.createNode({
                ...nd,
                id,
                x: (nd.x || 0) + (offset.x || 0),
                y: (nd.y || 0) + (offset.y || 0),
              });
              newNodes.push(node);
            }
            for (const ed of (data.edges || [])) {
              const fromId = idMap.get(ed.fromNode) || ed.fromNode;
              const toId   = idMap.get(ed.toNode)   || ed.toNode;
              if (!c.getNode(fromId) || !c.getNode(toId)) continue;
              const oldId = ed.id;
              let edgeId;
              if (strategy === 'remap') edgeId = c._newId();
              else edgeId = (oldId && !c._edges.find(e => e.id === oldId)) ? oldId : c._newId();
              const edge = c.createEdge({
                ...ed,
                id: edgeId,
                fromNode: fromId,
                toNode:   toId,
              });
              newEdges.push(edge);
            }
          });
          return { nodes: newNodes, edges: newEdges };
        }

        // Reconcile (Phase 3.3) - make the canvas equal a full desired-state
        // snapshot, minimally. Diffs incoming {nodes, edges} against the live
        // graph by id and applies adds / in-place updates / removes as ONE undo
        // step, preserving viewport, selection, focus, and the DOM of unchanged
        // nodes (no clearCanvas, no recenter, no markdown/file re-fetch).
        //
        // Contrast with the other inbound paths:
        //   canvas.data = …  destructive hard reset (clear + reload + recenter)
        //   io.mergeData()   paste a fragment (always creates new, remaps ids)
        //   io.reconcile()   sync to a complete snapshot in place
        //
        // Pass the COMPLETE desired graph, not a fragment - an edge whose
        // endpoint isn't among the incoming nodes is treated as an orphan and
        // dropped (same gate as load). opts.removeMissing (default true) deletes
        // nodes/edges absent from the incoming data; pass false for additive
        // sync (update + add only). Returns a report and emits a 'reconcile'
        // event with it.
        reconcile(data, opts = {}) {
          const c = this._c;
          const removeMissing = opts.removeMissing !== false;
          const { nodes, edges, report: normalization } = c._normalizeData(data || {});
          const inNodes = new Set(nodes.map(n => n.id));
          const inEdges = new Set(edges.map(e => e.id));
          const report = {
            nodes: { added: [], updated: [], replaced: [], removed: [], editSuperseded: null },
            edges: { added: [], updated: [], removed: [] },
            normalization,
          };

          // Snapshot selection + focus by id up front. Viewport (panX/panY/
          // scale) is never touched, so it needs no snapshot - it survives by
          // construction.
          const sel = c.getSelection();
          const selNodeIds = sel.nodes.map(n => n.id);
          const selEdgeId = sel.edge ? sel.edge.id : null;
          const focusId = c.focusedNode ? c.focusedNode.id : null;

          // An open inline edit on a node this snapshot changes/replaces/removes
          // is resolved BEFORE the batch: commit the user's buffer (don't discard
          // it) so their text survives, then let the batch apply the incoming data
          // on top. Committing outside the batch makes it its own undo step, so one
          // undo returns the user to their mid-edit text. An unchanged node is left
          // alone - its text equals the pre-edit value, so it isn't in the diff and
          // this is a no-op for it.
          const editNode = c.editing;
          if (editNode) {
            const incoming = nodes.find(n => n.id === editNode.id);
            let affected;
            if (incoming) {
              const d = this._diffNode(editNode, incoming);
              affected = d.hard || d.soft.has('text');
            } else {
              affected = removeMissing; // absent from snapshot ⟹ removed iff removeMissing
            }
            if (affected) {
              editNode.endEdit(true); // commit buffer → own undo step (outside batch)
              report.nodes.editSuperseded = editNode.id;
            }
          }

          c.batch(() => {
            // -- node phase ------------------------------------------------
            for (const nd of nodes) {
              const cur = c.getNode(nd.id);
              if (!cur) { c.createNode(nd); report.nodes.added.push(nd.id); continue; }
              const diff = this._diffNode(cur, nd);
              if (!diff.changed) continue;
              if (diff.hard) {
                // type / group label / background / accessibility can't change
                // in place - replace the single node, preserving its id. Its
                // incident edges are dropped by deleteNode and re-created below.
                c.deleteNode(cur);
                c.createNode(nd);
                report.nodes.replaced.push(nd.id);
              } else {
                this._applyNodeUpdate(cur, diff);
                report.nodes.updated.push(nd.id);
              }
            }
            if (removeMissing) {
              for (const n of c._nodes.slice()) {
                if (!inNodes.has(n.id)) { c.deleteNode(n); report.nodes.removed.push(n.id); }
              }
            }
            // -- edge phase (endpoints now exist) --------------------------
            for (const ed of edges) {
              const cur = c.getEdge(ed.id);
              if (!cur) { c.createEdge(ed); report.edges.added.push(ed.id); continue; }
              if (this._applyEdgeUpdate(cur, ed)) report.edges.updated.push(ed.id);
            }
            if (removeMissing) {
              for (const e of c._edges.slice()) {
                if (!inEdges.has(e.id)) { c.deleteEdge(e); report.edges.removed.push(e.id); }
              }
            }
          });

          // Restore selection/focus by id (drops removed ids; re-binds replaced
          // nodes/edges that survive under the same id).
          const survNodes = selNodeIds.filter(id => c.getNode(id));
          const survEdge = (selEdgeId && c.getEdge(selEdgeId)) ? selEdgeId : null;
          c.setSelection({ nodes: survNodes, edge: survEdge });
          if (focusId) {
            const fn = c.getNode(focusId);
            if (fn) c.setFocusedNode(fn, { focus: false });
          }
          c.requestDraw();
          c._emit('reconcile', report);
          return report;
        }

        // Canonical comparable view of a node, from either a live CanvasNode or
        // a raw normalized node-data object. _extraData keys live top-level in
        // the data form; here they're collected under `extra` so a passthrough
        // change is one comparable field. Mirrors NODE_KNOWN_KEYS.
        _nodeCanon(o, extra, a11y) {
          return {
            x: o.x, y: o.y, width: o.width, height: o.height, type: o.type,
            text: o.text || "",
            label: o.label ?? null,
            file: o.file ?? null,
            url: o.url ?? null,
            color: o.color || "default",
            background: o.background ?? null,
            backgroundStyle: o.backgroundStyle ?? null,
            accessibility: (a11y && Object.keys(a11y).length) ? a11y : null,
            extra: extra || {},
          };
        }

        _extractExtra(raw, knownKeys) {
          const extra = {};
          for (const k of Object.keys(raw)) if (!knownKeys.has(k)) extra[k] = raw[k];
          return extra;
        }

        // Diff a live node against incoming node-data. Returns
        // { changed, hard, soft:Set, extraChanged, target } where `target` is
        // the canonical incoming view (concrete defaulted values - never
        // undefined - so node.update() actually applies them). Soft fields
        // update in place via node.update(); a hard field difference forces a
        // node replace.
        _diffNode(cur, nd) {
          const a = this._nodeCanon(cur, cur._extraData, cur._a11y_authored);
          const b = this._nodeCanon(nd, this._extractExtra(nd, NODE_KNOWN_KEYS), nd.accessibility);
          const HARD = ["type", "label", "background", "backgroundStyle", "accessibility"];
          const SOFT = ["x", "y", "width", "height", "text", "color", "file", "url"];
          let hard = false;
          const soft = new Set();
          for (const k of HARD) if (!deepEqual(a[k], b[k])) hard = true;
          for (const k of SOFT) if (!deepEqual(a[k], b[k])) soft.add(k);
          const extraChanged = !deepEqual(a.extra, b.extra);
          return { changed: hard || soft.size > 0 || extraChanged, hard, soft, extraChanged, target: b };
        }

        _applyNodeUpdate(cur, diff) {
          const patch = {};
          for (const k of diff.soft) patch[k] = diff.target[k];
          if (Object.keys(patch).length) cur.update(patch);
          if (diff.extraChanged) {
            // _extraData never touches the DOM - assign directly and mirror the
            // setter contract (markDirty + nodeUpdate) so hosts/history see it.
            cur._extraData = diff.target.extra;
            this._c._markDirty();
            this._c._emit('nodeUpdate', { node: cur, kind: 'extraData' });
          }
        }

        // Update a live edge in place from incoming edge-data. Edges have no
        // DOM, and every field is in-place updatable (setEndpoints reroutes),
        // so edges never need a replace. Returns true if anything changed.
        _applyEdgeUpdate(cur, ed) {
          let changed = false;
          if (cur.fromNode !== ed.fromNode || cur.toNode !== ed.toNode ||
              cur.fromSide !== ed.fromSide || cur.toSide !== ed.toSide) {
            cur.setEndpoints({ fromNode: ed.fromNode, toNode: ed.toNode, fromSide: ed.fromSide, toSide: ed.toSide });
            changed = true;
          }
          const fromEnd = ed.fromEnd ?? "none";
          const toEnd = ed.toEnd ?? "arrow";
          if (cur.fromEnd !== fromEnd || cur.toEnd !== toEnd) {
            cur.setEnds({ fromEnd, toEnd });
            changed = true;
          }
          const color = ed.color || "default";
          if (cur.color !== color) { cur.setColor(color); changed = true; }
          const label = ed.label ?? "";
          if ((cur.edge_label || "") !== label) { cur.setLabel(label); changed = true; }
          const curA11y = (cur._a11y_authored && Object.keys(cur._a11y_authored).length) ? cur._a11y_authored : null;
          const ndA11y = (ed.accessibility && Object.keys(ed.accessibility).length) ? ed.accessibility : null;
          if (!deepEqual(curA11y, ndA11y)) {
            // setAccessibility merges; null-first to make it a clean replace.
            cur.setAccessibility(null);
            if (ndA11y) cur.setAccessibility(ndA11y);
            changed = true;
          }
          const ndExtra = this._extractExtra(ed, EDGE_KNOWN_KEYS);
          if (!deepEqual(cur._extraData || {}, ndExtra)) {
            cur._extraData = ndExtra;
            this._c._markDirty();
            this._c._emit('edgeUpdate', { edge: cur, kind: 'extraData' });
            changed = true;
          }
          return changed;
        }

        // Returns a Promise<Blob> with a PNG raster of the canvas content.
        // opts: rect (world rect to capture; default = bbox of all nodes),
        // padding (default 20), scale (raster scale; default 1),
        // background (CSS color; default transparent).
        //
        // Captures the live rendered DOM (markdown-converted HTML, file
        // images, .md previews, .canvas thumbnails, group backgrounds) by
        // embedding each node into an SVG <foreignObject> with computed
        // styles and images inlined as data URLs. Cross-origin images
        // without CORS headers are omitted with a console.warn.
        async exportPNG(opts = {}) {
          const built = await this._buildSVG(opts);
          const scale = opts.scale != null ? opts.scale : 1;
          const bg = opts.background || null;
          return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => {
              const cnv = document.createElement("canvas");
              cnv.width  = Math.max(1, Math.round(built.width  * scale));
              cnv.height = Math.max(1, Math.round(built.height * scale));
              const ctx = cnv.getContext("2d");
              if (bg) { ctx.fillStyle = bg; ctx.fillRect(0, 0, cnv.width, cnv.height); }
              ctx.drawImage(img, 0, 0, cnv.width, cnv.height);
              try {
                cnv.toBlob(b => b ? resolve(b) : reject(new Error('toBlob returned null')), 'image/png');
              } catch (err) {
                reject(err);
              }
            };
            img.onerror = () => reject(new Error('SVG rasterize failed'));
            img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(built.svgString);
          });
        }

        // Returns a Promise<string> with the canvas content as a
        // self-contained SVG. Each node ships its rendered DOM inside a
        // <foreignObject> with computed styles inlined; <img> and embedded
        // <canvas> contents are inlined as data URLs so the SVG opens
        // standalone. Was a sync method previously - now async because
        // images need fetching.
        async exportSVG(opts = {}) {
          return (await this._buildSVG(opts)).svgString;
        }

        // Fetches a URL and returns a Promise<string|null> with a data URL.
        // urlCache (Map) dedupes refetches across one export. Cross-origin
        // without CORS resolves null with a single console.warn per export.
        _inlineImageURL(url, urlCache, warnedRef) {
          if (!url) return Promise.resolve(null);
          if (urlCache.has(url)) return urlCache.get(url);
          const p = fetch(url, { mode: 'cors' })
            .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
            .then(blob => new Promise((resolve, reject) => {
              const fr = new FileReader();
              fr.onload = () => resolve(fr.result);
              fr.onerror = () => reject(fr.error || new Error('FileReader failed'));
              fr.readAsDataURL(blob);
            }))
            .catch(err => {
              if (!warnedRef.warned) {
                warnedRef.warned = true;
                console.warn('tzara-canvas export: could not inline ' + url + ' (' + err.message + '). Further failures suppressed.');
              }
              return null;
            });
          urlCache.set(url, p);
          return p;
        }

        // Visually-significant CSS properties copied per element into the
        // cloned DOM. Kept small to limit SVG bloat; covers color, layout
        // box, typography, borders, backgrounds, and a few flex/transform
        // basics that markdown-rendered content tends to use.
        static _styleAllowlist() {
          return [
            'color','background-color','background-image','background-size',
            'background-position','background-repeat','background-clip',
            'font-family','font-size','font-weight','font-style','font-variant',
            'line-height','letter-spacing','text-align','text-decoration',
            'text-transform','text-shadow','white-space','word-break','word-wrap',
            'overflow-wrap','tab-size',
            'border','border-top','border-right','border-bottom','border-left',
            'border-color','border-style','border-width','border-radius',
            'padding','padding-top','padding-right','padding-bottom','padding-left',
            'margin','margin-top','margin-right','margin-bottom','margin-left',
            'display','box-sizing','overflow','overflow-x','overflow-y',
            'opacity','visibility',
            'width','height','min-width','min-height','max-width','max-height',
            'position','top','right','bottom','left',
            'flex-direction','justify-content','align-items','gap',
            'list-style','list-style-type','list-style-position',
            'vertical-align','transform','transform-origin',
          ];
        }

        // Clones a node's DOM subtree (in-place mutations on the clone),
        // inlines computed styles, swaps <img> srcs for data URLs, and
        // replaces <canvas> elements with <img> snapshots. Returns the
        // prepared root clone (DOM, not serialized).
        async _prepareDomForExport(originalRoot, urlCache, warnedRef) {
          const clone = originalRoot.cloneNode(true);
          const allowlist = IoAPI._styleAllowlist();

          // Walk in lockstep: original tree provides getComputedStyle()
          // (the clone has no styles because it isn't in the document).
          const origQueue = [originalRoot];
          const cloneQueue = [clone];
          const imgJobs = [];
          const canvasReplacements = [];
          while (origQueue.length) {
            const orig = origQueue.shift();
            const cln = cloneQueue.shift();
            if (cln.nodeType !== 1) continue;

            // Strip script tags and on* handlers for safe SVG embedding.
            if (cln.tagName === 'SCRIPT') {
              cln.remove();
              continue;
            }
            for (const attr of Array.from(cln.attributes)) {
              if (/^on/i.test(attr.name)) cln.removeAttribute(attr.name);
            }

            const cs = window.getComputedStyle(orig);
            if (cs) {
              let inline = '';
              for (const prop of allowlist) {
                const v = cs.getPropertyValue(prop);
                if (v) inline += prop + ':' + v + ';';
              }
              if (inline) cln.setAttribute('style', inline);
            }

            if (cln.tagName === 'IMG') {
              imgJobs.push((async () => {
                const src = orig.currentSrc || orig.src;
                if (!src || src.startsWith('data:')) return;
                const dataUrl = await this._inlineImageURL(src, urlCache, warnedRef);
                if (dataUrl) {
                  cln.setAttribute('src', dataUrl);
                } else {
                  const placeholder = document.createElement('div');
                  placeholder.textContent = orig.alt || '';
                  placeholder.setAttribute('style',
                    'display:flex;align-items:center;justify-content:center;'
                    + 'width:100%;height:100%;color:#888;font-family:sans-serif;'
                    + 'font-size:12px;background:#f4f4f4;');
                  cln.replaceWith(placeholder);
                }
              })());
            } else if (cln.tagName === 'IFRAME') {
              // Iframes don't serialize (content lives in a separate
              // browsing context) and won't load inside an SVG-as-image
              // anyway. Replace with a static URL+favicon card.
              imgJobs.push((async () => {
                const src = orig.src || orig.getAttribute('src') || '';
                let faviconUrl = null;
                if (src) {
                  try {
                    faviconUrl = new URL('/favicon.ico', src).href;
                  } catch (_) { /* relative or malformed src */ }
                }
                const faviconData = faviconUrl
                  ? await this._inlineImageURL(faviconUrl, urlCache, warnedRef)
                  : null;
                const card = document.createElement('div');
                card.setAttribute('style',
                  'display:flex;flex-direction:column;align-items:center;'
                  + 'justify-content:center;width:100%;height:100%;'
                  + 'box-sizing:border-box;padding:1rem;gap:0.75rem;'
                  + 'color:#444;font-family:sans-serif;font-size:13px;'
                  + 'background:#fafafa;border:1px dashed #ccc;'
                  + 'border-radius:6px;text-align:center;word-break:break-all;');
                if (faviconData) {
                  const favImg = document.createElement('img');
                  favImg.setAttribute('src', faviconData);
                  favImg.setAttribute('style',
                    'width:48px;height:48px;display:block;'
                    + 'object-fit:contain;');
                  card.appendChild(favImg);
                }
                const urlText = document.createElement('div');
                urlText.textContent = src || '(no url)';
                urlText.setAttribute('style',
                  'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
                  + 'font-size:12px;max-width:100%;overflow-wrap:anywhere;');
                card.appendChild(urlText);
                if (cln.parentNode) cln.replaceWith(card);
              })());
            } else if (cln.tagName === 'CANVAS') {
              // <canvas> bitmap can't survive XMLSerializer - snapshot
              // via toDataURL and swap in an <img>.
              try {
                const dataUrl = orig.toDataURL('image/png');
                const img = document.createElement('img');
                img.setAttribute('src', dataUrl);
                const w = orig.style.width || (orig.width + 'px');
                const h = orig.style.height || (orig.height + 'px');
                img.setAttribute('style',
                  'display:block;width:' + w + ';height:' + h + ';');
                canvasReplacements.push([cln, img]);
              } catch (err) {
                // Tainted canvas (e.g. cross-origin drawImage) - drop silently.
                canvasReplacements.push([cln, document.createElement('div')]);
              }
            } else {
              // Inline a CSS background-image if one is present, so the
              // rasterizer sees it (otherwise the url() is a cross-context
              // network ref the SVG image loader won't resolve).
              const bgImg = cs && cs.getPropertyValue('background-image');
              if (bgImg && bgImg !== 'none' && bgImg.indexOf('url(') === 0) {
                imgJobs.push((async () => {
                  const m = /url\(([\x27\x22]?)([^\x27\x22)]+)\1\)/.exec(bgImg);
                  if (!m) return;
                  const u = m[2];
                  if (u.startsWith('data:')) return;
                  const dataUrl = await this._inlineImageURL(u, urlCache, warnedRef);
                  if (dataUrl) {
                    const existing = cln.getAttribute('style') || '';
                    cln.setAttribute('style',
                      existing.replace(/background-image\s*:[^;]+;?/i, '')
                      + 'background-image:url(' + dataUrl + ');');
                  }
                })());
              }
            }

            const oChildren = orig.childNodes;
            const cChildren = cln.childNodes;
            const n = Math.min(oChildren.length, cChildren.length);
            for (let i = 0; i < n; i++) {
              origQueue.push(oChildren[i]);
              cloneQueue.push(cChildren[i]);
            }
          }

          await Promise.all(imgJobs);
          for (const [oldEl, newEl] of canvasReplacements) {
            if (oldEl.parentNode) oldEl.replaceWith(newEl);
          }
          clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
          return clone;
        }

        async _buildSVG(opts = {}) {
          const c = this._c;
          const padding = opts.padding != null ? opts.padding : 20;
          let rect = opts.rect;
          if (!rect) {
            if (!c._nodes.length) {
              rect = { x: 0, y: 0, width: 1, height: 1 };
            } else {
              const bb = getBoundingBox(c._nodes);
              rect = {
                x: bb.left - padding,
                y: bb.top - padding,
                width:  (bb.right - bb.left) + 2 * padding,
                height: (bb.bottom - bb.top) + 2 * padding,
              };
            }
          }
          const w = Math.max(1, rect.width);
          const h = Math.max(1, rect.height);
          const esc = (s) => String(s == null ? "" : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\x22/g, '&quot;');

          const urlCache = new Map();
          const warnedRef = { warned: false };
          const serializer = new XMLSerializer();

          // Prepare every node's DOM in parallel so image fetches overlap.
          const nodePrep = await Promise.all(c._nodes.map(async (n) => {
            if (n.type === 'group') {
              let bgDataUrl = null;
              if (n.background && c.resolveFile) {
                bgDataUrl = await this._inlineImageURL(
                  c.resolveFile(n.background), urlCache, warnedRef);
              }
              return { n, kind: 'group', bgDataUrl };
            }
            if (!n._dom) return { n, kind: 'rect' };
            const clone = await this._prepareDomForExport(n._dom, urlCache, warnedRef);
            // Force absolute positioning to (0,0) inside foreignObject so
            // the node's own top/left (which place it on the host canvas)
            // don't shift it.
            const rootStyle = clone.getAttribute('style') || '';
            clone.setAttribute('style',
              rootStyle.replace(/(^|;)\s*(position|top|left|transform)\s*:[^;]+/gi, '')
              + ';position:static;top:auto;left:auto;transform:none;'
              + 'width:' + n.width + 'px;height:' + n.height + 'px;'
              + 'box-sizing:border-box;');
            return { n, kind: 'dom', html: serializer.serializeToString(clone) };
          }));

          const parts = [];
          parts.push('<svg xmlns="http://www.w3.org/2000/svg" '
            + 'xmlns:xlink="http://www.w3.org/1999/xlink" '
            + 'width="' + w + '" height="' + h
            + '" viewBox="' + rect.x + ' ' + rect.y + ' ' + w + ' ' + h + '">');
          if (opts.background) {
            parts.push('<rect x="' + rect.x + '" y="' + rect.y
              + '" width="' + w + '" height="' + h
              + '" fill="' + esc(opts.background) + '"/>');
          }

          for (const entry of nodePrep) {
            const n = entry.n;
            if (entry.kind === 'group') {
              const fill   = n.backgroundColor || '#ffffff';
              const stroke = n.borderColor     || '#333333';
              parts.push('<rect x="' + n.x + '" y="' + n.y + '" width="' + n.width
                + '" height="' + n.height + '" rx="6" ry="6" fill="' + esc(fill)
                + '" fill-opacity="0.4" stroke="' + esc(stroke) + '" stroke-width="3"/>');
              if (entry.bgDataUrl) {
                // 'repeat' is lossy in <image>; falls back to a contain-like fit.
                const style = n.backgroundStyle || 'cover';
                const preserve = style === 'ratio' ? 'xMidYMid meet' : 'xMidYMid slice';
                parts.push('<image x="' + n.x + '" y="' + n.y
                  + '" width="' + n.width + '" height="' + n.height
                  + '" preserveAspectRatio="' + preserve
                  + '" opacity="0.6" href="' + esc(entry.bgDataUrl)
                  + '" xlink:href="' + esc(entry.bgDataUrl) + '"/>');
              }
              if (n.label) {
                parts.push('<text x="' + (n.x + 8) + '" y="' + (n.y + 20)
                  + '" font-family="sans-serif" font-size="14" fill="#222">'
                  + esc(n.label) + '</text>');
              }
              continue;
            }
            if (entry.kind === 'rect' || !entry.html) {
              const fill   = n.backgroundColor || '#ffffff';
              const stroke = n.borderColor     || '#333333';
              parts.push('<rect x="' + n.x + '" y="' + n.y + '" width="' + n.width
                + '" height="' + n.height + '" rx="6" ry="6" fill="' + esc(fill)
                + '" stroke="' + esc(stroke) + '" stroke-width="3"/>');
              continue;
            }
            parts.push('<foreignObject x="' + n.x + '" y="' + n.y
              + '" width="' + n.width + '" height="' + n.height + '">'
              + entry.html
              + '</foreignObject>');
          }

          // Edges: bezier path matching drawSmartBezier's geometry.
          for (const e of c._edges) {
            const fromNode = c.getNode(e.fromNode);
            const toNode   = c.getNode(e.toNode);
            if (!fromNode || !toNode) continue;
            const start = e.rectEdgePoint(fromNode, "from");
            const end   = e.rectEdgePoint(toNode,   "to");
            const { ctrl1: c1, ctrl2: c2 } = CanvasEdge._bezierControls(start, end);
            const stroke = e.borderColor || '#333';
            parts.push('<path d="M ' + start.x + ' ' + start.y
              + ' C ' + c1.x + ' ' + c1.y + ', ' + c2.x + ' ' + c2.y
              + ', ' + end.x + ' ' + end.y + '" fill="none" stroke="' + esc(stroke) + '" stroke-width="1.5"/>');
            if (e.edge_label) {
              const lx = (start.x + end.x) / 2, ly = (start.y + end.y) / 2;
              parts.push('<text x="' + lx + '" y="' + ly
                + '" font-family="sans-serif" font-size="14" fill="' + esc(stroke)
                + '" text-anchor="middle">' + esc(e.edge_label) + '</text>');
            }
          }

          parts.push('</svg>');
          return { svgString: parts.join(''), width: w, height: h };
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // PermissionsAPI - canvas.permissions namespace ////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      // Granular permission gates for user interactions. Each key defaults to
      // true. readOnly is a documented shortcut that bulk-writes the edit-perm
      // subset; non-mutating perms (panX/panY/zoom/select/contextMenu/copy) are
      // untouched by setReadOnly so a locked canvas still pans/zooms/selects/
      // copies.
      const _PERM_EDIT_KEYS = Object.freeze([
        'createNode','createEdge','editText','dragNode','resizeNode','editNodeStyle',
        'deleteNode','group','paste','cut','undo','redo',
      ]);
      const _PERM_NONEDIT_KEYS = Object.freeze([
        'panX','panY','zoom','select','contextMenu','copy',
      ]);
      const _PERM_ALL_KEYS = Object.freeze([..._PERM_NONEDIT_KEYS, ..._PERM_EDIT_KEYS]);

      class PermissionsAPI {
        constructor(canvas) {
          this._c = canvas;
          this._perms = {};
          for (const k of _PERM_ALL_KEYS) this._perms[k] = true;
        }

        // Returns the current value for a permission key. Accepts the
        // pseudo-keys 'pan' (true iff both panX and panY are true) and
        // 'readOnly' (delegates to isReadOnly()).
        get(key) {
          if (key === 'pan')      return this._perms.panX && this._perms.panY;
          if (key === 'readOnly') return this.isReadOnly();
          return Object.prototype.hasOwnProperty.call(this._perms, key) ? this._perms[key] : true;
        }

        // Set a single key. 'pan' expands to panX+panY; 'readOnly' delegates to
        // setReadOnly. Unknown keys are ignored. Emits 'permissionChange' iff
        // the effective value changed.
        set(key, value) {
          const v = value === true;
          if (key === 'readOnly') { this.setReadOnly(value); return this; }
          if (key === 'pan')      { this.setAll({ pan: v });   return this; }
          if (!Object.prototype.hasOwnProperty.call(this._perms, key)) return this;
          if (this._perms[key] === v) return this;
          this._perms[key] = v;
          this._onChange(key, v);
          return this;
        }

        // Bulk-set from a partial map. Honors the 'pan' and 'readOnly' pseudo-
        // keys. Coalesces emission into a single 'permissionChange' event.
        setAll(partial) {
          if (!partial || typeof partial !== 'object') return this;
          let changed = false;
          for (const k of Object.keys(partial)) {
            const v = partial[k] === true;
            if (k === 'readOnly') {
              const target = !v;
              for (const ek of _PERM_EDIT_KEYS) {
                if (this._perms[ek] !== target) { this._perms[ek] = target; changed = true; }
              }
            } else if (k === 'pan') {
              if (this._perms.panX !== v) { this._perms.panX = v; changed = true; }
              if (this._perms.panY !== v) { this._perms.panY = v; changed = true; }
            } else if (Object.prototype.hasOwnProperty.call(this._perms, k)) {
              if (this._perms[k] !== v) { this._perms[k] = v; changed = true; }
            }
          }
          if (changed) this._onChange(null, null);
          return this;
        }

        // Shallow copy of the current permission map. Pseudo-keys ('pan',
        // 'readOnly') are not included.
        all() { return { ...this._perms }; }

        // Documented as a shortcut: setReadOnly(true) writes false into every
        // edit-perm; setReadOnly(false) writes true. Non-mutating perms are
        // untouched. Single coalesced 'permissionChange' emission.
        setReadOnly(bool) {
          const target = !(bool === true);
          let changed = false;
          for (const ek of _PERM_EDIT_KEYS) {
            if (this._perms[ek] !== target) { this._perms[ek] = target; changed = true; }
          }
          if (changed) this._onChange(null, null);
          return this;
        }

        // True iff every edit-perm is false. A canvas with one edit-perm
        // enabled is not considered read-only.
        isReadOnly() {
          for (const ek of _PERM_EDIT_KEYS) {
            if (this._perms[ek]) return false;
          }
          return true;
        }

        _onChange(key, value) {
          const c = this._c;
          // Rebuild toolbar/panels (only after initial UI exists; the
          // constructor applies options before the UI is built).
          if (typeof c._rebuildCanvasUI === 'function' && c.toolbar) c._rebuildCanvasUI();
          if (typeof c._updateToolbars === 'function' && c.nodeToolbar) c._updateToolbars();
          if (typeof c._updateSaveResetButtons === 'function') c._updateSaveResetButtons();
          if (typeof c.requestDraw === 'function') c.requestDraw();
          if (typeof c._emit === 'function') c._emit('permissionChange', { key, value, all: this.all() });
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // ShortcutsAPI - canvas.shortcuts namespace ////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      // Read/write live keyboard bindings, register host actions, and
      // surface the action list to host UIs. The matcher index is kept on
      // the Canvas (canvas._shortcutIndex: Map<canonicalKey, actionName>)
      // so event_keydown can do a single Map lookup per keystroke.
      class ShortcutsAPI {
        constructor(canvas) { this._c = canvas; }

        // Return the current display-form bindings for an action as a
        // fresh array, or null if the action is disabled. Unknown action:
        // returns undefined.
        get(action) {
          const e = this._c._shortcuts.get(action);
          if (!e) return undefined;
          if (e.current == null) return null;
          return e.current.slice();
        }

        // Snapshot of all actions: { actionName: string[]|null }. Disabled
        // actions report null. Display form (round-trips through bind()).
        all() {
          const out = {};
          for (const [name, e] of this._c._shortcuts) {
            out[name] = e.current == null ? null : e.current.slice();
          }
          return out;
        }

        // Metadata listing for host settings UIs. Each item is a snapshot
        // - mutating it won't affect the registry. `current` is null when
        // the action is disabled.
        actions() {
          const list = [];
          for (const [name, e] of this._c._shortcuts) {
            list.push({
              action:      name,
              description: e.description,
              group:       e.group,
              defaults:    e.defaults.slice(),
              current:     e.current == null ? null : e.current.slice(),
              builtin:     !!e.builtin,
            });
          }
          return list;
        }

        // Replace the bindings for an action. `keys` is a string, an array
        // of strings, or null/[] to disable. Returns this for chaining.
        // Unknown action names log a warn and no-op so a stale host config
        // doesn't crash init.
        bind(action, keys) {
          const e = this._c._shortcuts.get(action);
          if (!e) {
            console.warn('TzaraCanvas.shortcuts.bind: unknown action "' + action + '"');
            return this;
          }
          if (keys == null || (Array.isArray(keys) && keys.length === 0)) {
            e.current = null;
            e._canonical = [];
          } else {
            const n = normalizeBinding(keys);
            e.current = n.display.length ? n.display : null;
            e._canonical = n.canonical;
          }
          this._c._rebuildShortcutIndex();
          this._c._refreshHelpDialogIfOpen();
          return this;
        }

        unbind(action) { return this.bind(action, null); }

        // Restore defaults. With no argument, restores every action.
        reset(action) {
          if (action === undefined) {
            for (const [name, e] of this._c._shortcuts) {
              const n = normalizeBinding(e.defaults);
              e.current = n.display.length ? n.display : null;
              e._canonical = n.canonical;
            }
          } else {
            const e = this._c._shortcuts.get(action);
            if (!e) {
              console.warn('TzaraCanvas.shortcuts.reset: unknown action "' + action + '"');
              return this;
            }
            const n = normalizeBinding(e.defaults);
            e.current = n.display.length ? n.display : null;
            e._canonical = n.canonical;
          }
          this._c._rebuildShortcutIndex();
          this._c._refreshHelpDialogIfOpen();
          return this;
        }

        // Register a host action. opts: { description, group?, defaults?, handler }.
        // `defaults` accepts the same shape as bind() (string|string[]|null).
        // The handler is invoked as handler(event, canvas); returning false
        // opts out of the dispatcher's preventDefault. Throws on name
        // collision (built-in or already-registered) and on missing handler.
        register(action, opts) {
          if (typeof action !== "string" || !action) {
            throw new Error("TzaraCanvas.shortcuts.register: action name must be a non-empty string");
          }
          if (this._c._shortcuts.has(action)) {
            throw new Error('TzaraCanvas.shortcuts.register: action "' + action + '" already exists');
          }
          if (!opts || typeof opts.handler !== "function") {
            throw new Error("TzaraCanvas.shortcuts.register: opts.handler must be a function");
          }
          const defaults = opts.defaults == null
            ? []
            : (Array.isArray(opts.defaults) ? opts.defaults.slice() : [opts.defaults]);
          const n = normalizeBinding(defaults);
          this._c._shortcuts.set(action, {
            action,
            description: typeof opts.description === "string" ? opts.description : "",
            group:       typeof opts.group === "string" && opts.group ? opts.group : "Custom",
            defaults,
            current:     n.display.length ? n.display : null,
            _canonical:  n.canonical,
            handler:     opts.handler,
            builtin:     false,
          });
          this._c._rebuildShortcutIndex();
          this._c._refreshHelpDialogIfOpen();
          return this;
        }

        // Remove a host-registered action. Built-ins refuse - use unbind()
        // to silence one of those instead.
        unregister(action) {
          const e = this._c._shortcuts.get(action);
          if (!e) {
            console.warn('TzaraCanvas.shortcuts.unregister: unknown action "' + action + '"');
            return this;
          }
          if (e.builtin) {
            throw new Error('TzaraCanvas.shortcuts.unregister: cannot remove built-in action "' + action + '" (use unbind() to disable it)');
          }
          this._c._shortcuts.delete(action);
          this._c._rebuildShortcutIndex();
          this._c._refreshHelpDialogIfOpen();
          return this;
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // ClipboardAPI - canvas.clipboard namespace ////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class ClipboardAPI {
        constructor(canvas) {
          this._c = canvas;
          this._buffer = null; // { nodes: [...data], edges: [...data] }
        }

        has()   { return !!this._buffer; }
        peek()  { return this._buffer ? JSON.parse(JSON.stringify(this._buffer)) : null; }
        clear() { this._buffer = null; return this; }

        // Snapshot the current selection (nodes + edges between selected
        // nodes + the selected edge if it has both endpoints in the set).
        copy() {
          const c = this._c;
          if (!c._can('copy')) return null;
          const nodes = c.selectedNodes.slice();
          if (!nodes.length) return null;
          const ids = new Set(nodes.map(n => n.id));
          const internalEdges = c._edges.filter(e => ids.has(e.fromNode) && ids.has(e.toNode));
          const fullData = c.io.toData();
          this._buffer = {
            nodes: fullData.nodes.filter(n => ids.has(n.id)),
            edges: fullData.edges.filter(e => internalEdges.some(ie => ie.id === e.id)),
          };
          c._emit('clipboardCopy', this.peek());
          return this.peek();
        }

        // Copy then delete. Emits clipboardCut.
        cut() {
          const c = this._c;
          if (!c._can('cut')) return null;
          const snap = this.copy();
          if (!snap) return null;
          c.batch(() => {
            for (const n of c.selectedNodes.slice()) c.deleteNode(n);
          });
          c._emit('clipboardCut', snap);
          return snap;
        }

        // Paste via io.mergeData. opts.offset translates positions (default
        // {20,20} so the paste is visibly distinct from the source).
        paste(opts = {}) {
          const c = this._c;
          if (!c._can('paste')) return { nodes: [], edges: [] };
          if (!this._buffer) return { nodes: [], edges: [] };
          const offset = opts.offset || { x: 20, y: 20 };
          const result = c.io.mergeData(this._buffer, { offset, idStrategy: 'remap' });
          c.setSelection({ nodes: result.nodes.map(n => n.id), edge: null });
          c._emit('clipboardPaste', result);
          return result;
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // HistoryAPI - canvas.history namespace ////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class HistoryAPI {
        constructor(canvas, { depth = 50 } = {}) {
          this._c = canvas;
          this._depth = Math.max(1, depth | 0);
          this._undo = [];
          this._redo = [];
          this._current = this._capture();
          // While _suspended is true, _recordChange is a no-op. Used during
          // restore/undo/redo so re-applying a snapshot doesn't pollute history.
          this._suspended = false;
        }

        // Take an explicit snapshot. Returned object is opaque; treat as a
        // token to pass back into restore().
        snapshot() { return this._capture(); }

        // Replace the canvas state with the snapshot's contents. Live
        // CanvasNode/CanvasEdge instances captured before this call are
        // invalidated - host code must re-resolve by id.
        restore(snap) {
          if (!snap) return this;
          this._suspended = true;
          try {
            this._apply(snap);
            this._current = this._capture();
          } finally {
            this._suspended = false;
          }
          this._emitChange();
          return this;
        }

        // Reserved for a richer JSON-diff in a later pass.
        diff(_a, _b) { return null; }

        undo() {
          if (!this._c._can('undo')) return false;
          if (!this._undo.length) return false;
          const prev = this._undo.pop();
          this._redo.push(this._current);
          this._current = prev;
          this._suspended = true;
          try { this._apply(prev); } finally { this._suspended = false; }
          this._emitChange();
          return true;
        }

        redo() {
          if (!this._c._can('redo')) return false;
          if (!this._redo.length) return false;
          const next = this._redo.pop();
          this._undo.push(this._current);
          this._current = next;
          this._suspended = true;
          try { this._apply(next); } finally { this._suspended = false; }
          this._emitChange();
          return true;
        }

        canUndo() { return this._undo.length > 0; }
        canRedo() { return this._redo.length > 0; }
        depth()   { return this._undo.length; }

        clear() {
          this._undo = [];
          this._redo = [];
          this._current = this._capture();
          this._emitChange();
          return this;
        }

        // ---- internals ----
        // Called by Canvas._markDirty after each user-driven mutation
        // (skipped while batched or while restore/undo/redo is in flight).
        _recordChange() {
          if (this._suspended) return;
          this._undo.push(this._current);
          while (this._undo.length > this._depth) this._undo.shift();
          this._current = this._capture();
          this._redo = [];
          this._emitChange();
        }

        _capture() {
          const c = this._c;
          return {
            data: c.io.toData(),
            view: { panX: c.panX, panY: c.panY, scale: c.scale },
          };
        }

        _apply(snap) {
          const c = this._c;
          // Flush open edit / edge-draft / drag before the node DOM is rebuilt;
          // an undo/redo/restore while editing must not orphan c.editing.
          c._abortInteraction();
          c._nodes.forEach(n => c._removeNodeDom(n));
          // edge <li>s are canvas-level, not per-node, so the
          // node-DOM loop above doesn't clean them up. Reset the
          // mirror containers before createNodesAndEdges rebuilds them.
          c._resetA11yMirror();
          c._nodes = [];
          c._edges = [];
          c.selectedNodes = [];
          c.selectedEdge = null;
          c._clickedEdge = null;
          c.createNodesAndEdges({
            nodes: snap.data.nodes.map(n => ({ ...n })),
            edges: snap.data.edges.map(e => ({ ...e })),
          });
          if (snap.view) {
            c.panX  = snap.view.panX;
            c.panY  = snap.view.panY;
            c.scale = snap.view.scale;
            c.updateTransform();
            c._emitViewportChange();
          }
          // Selection is wiped by the rebuild; let listeners know.
          c._maybeEmitSelectionChange();
          c.requestDraw();
          c._emit('historyApply', snap);
          c._emit('dataChange', c.io.toData());
        }

        _emitChange() {
          this._c._emit('undoStackChange', {
            canUndo: this.canUndo(),
            canRedo: this.canRedo(),
            depth: this._undo.length,
          });
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // UIAPI - canvas.ui namespace //////////////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      // Handle for one trigger button inside a wired toolbar. Read-through to
      // the live DOM via the toolbar's _triggers map, so the handle stays
      // valid across moves/inserts within the same toolbar.
      class ToolbarButton {
        constructor(controller, key) {
          this._controller = controller;
          this._key = key;
        }
        get key() { return this._key; }
        get element() { return this._controller._tb._triggers[this._key] || null; }
        setEnabled(enabled) {
          const el = this.element;
          if (el) el.disabled = !enabled;
          return this;
        }
        setIcon(spec) {
          const el = this.element;
          if (!el) return this;
          const normalized = (typeof spec === "string") ? { emoji: spec } : (spec || {});
          this._controller._tb._applyIcon(el, normalized);
          return this;
        }
        setTitle(title) {
          const el = this.element;
          if (el) el.title = title || "";
          return this;
        }
        openDrawer() {
          const tb = this._controller._tb;
          const section = tb._sections.find(s => s.key === this._key);
          if (section && section.panel) tb._setActive(this._key);
          return this;
        }
        closeDrawer() {
          const tb = this._controller._tb;
          if (tb._activeSection === this._key) tb._setActive(null);
          return this;
        }
      }

      // Public wrapper for one toolbar (global / node / edge). Surfaces a
      // uniform hide/show/add/remove API; floating-only kinds ignore the
      // canvas-only methods.
      class ToolbarController {
        constructor(canvas, tb, kind) {
          this._c = canvas;
          this._tb = tb;
          this._kind = kind;   // 'canvas' | 'node' | 'edge'
          this._buttons = new Map();
        }

        // Whether the toolbar is user-allowed to render. For floating toolbars
        // (node/edge), actual rendering also depends on selection state.
        get visible() { return !this._tb.classList.contains("tc-toolbar-hidden"); }
        hide()   { this._tb.classList.add("tc-toolbar-hidden");    return this; }
        show()   { this._tb.classList.remove("tc-toolbar-hidden"); return this; }
        toggle() { return this.visible ? this.hide() : this.show(); }

        // Spec mirrors _wireToolbar's section shape:
        //   { key, title, emoji?|icon?|iconUrl?, panel?, onClick?, className?, enabled? }
        // onClick is the public name; section.action stays the internal one.
        addButton(spec, opts = {}) {
          if (!spec || !spec.key) throw new Error("addButton: spec.key is required");
          if (spec.panel && spec.onClick) {
            throw new Error("addButton: spec.panel and spec.onClick are mutually exclusive");
          }
          const section = {
            key: spec.key,
            title: spec.title,
            emoji: spec.emoji,
            icon: spec.icon,
            iconUrl: spec.iconUrl,
            panel: spec.panel,
            className: spec.className,
            enabled: spec.enabled,
            action: spec.onClick
              ? (e) => spec.onClick({ canvas: this._c, button: this.getButton(spec.key), event: e })
              : undefined,
          };
          if (section.panel && !section.panel.classList.contains("tc-panel")) {
            section.panel.classList.add("tc-panel");
          }
          this._tb._addSection(section, opts);
          const handle = new ToolbarButton(this, spec.key);
          this._buttons.set(spec.key, handle);
          // Evaluate a function `enabled` predicate immediately so the new
          // button reflects current state without waiting for the next sync.
          if (typeof section.enabled === "function") this.refresh();
          return handle;
        }

        removeButton(key) {
          const ok = this._tb._removeSection(key);
          if (ok) this._buttons.delete(key);
          return ok;
        }

        getButton(key) {
          if (!this._tb._triggers[key]) return null;
          let b = this._buttons.get(key);
          if (!b) { b = new ToolbarButton(this, key); this._buttons.set(key, b); }
          return b;
        }

        listButtons() { return this._tb._sections.map(s => s.key); }

        closeDrawer() { this._tb._setActive(null); return this; }

        // Re-evaluate function `enabled` predicates on every section in this
        // toolbar. Node/edge toolbars call this automatically from their
        // selection-sync functions; canvas-toolbar consumers call it manually
        // when their predicate's inputs change.
        refresh() {
          const tb = this._tb;
          if (!tb || !tb._sections) return this;
          for (const s of tb._sections) {
            if (typeof s.enabled !== "function") continue;
            const trigger = tb._triggers[s.key];
            if (!trigger) continue;
            let ok = true;
            try { ok = !!s.enabled(this._c); }
            catch (err) { console.error(`toolbar button "${s.key}" enabled() threw:`, err); }
            trigger.disabled = !ok;
          }
          return this;
        }
      }

      // Adds repositioning + reorientation, which only make sense for the
      // global (always-on) toolbar - the floating ones are placed by selection.
      class CanvasToolbarController extends ToolbarController {
        constructor(canvas, tb) {
          super(canvas, tb, "canvas");
          this._position    = "top-left";    // mirrors the initial inline style
          this._orientation = "horizontal";
        }

        get position()    { return this._position; }
        get orientation() { return this._orientation; }

        setPosition(corner) {
          const suffix = { "top-left": "tl", "top-right": "tr", "bottom-left": "bl", "bottom-right": "br" }[corner];
          if (!suffix) throw new Error(`setPosition: unknown corner "${corner}"`);
          const tb = this._tb;
          tb.classList.remove("tc-toolbar-pos-tl", "tc-toolbar-pos-tr", "tc-toolbar-pos-bl", "tc-toolbar-pos-br");
          tb.classList.add(`tc-toolbar-pos-${suffix}`);
          this._position = corner;
          this._reapplyOrientation();   // anchor side depends on which half we're on
          return this;
        }

        setOrientation(orientation) {
          if (orientation !== "horizontal" && orientation !== "vertical") {
            throw new Error(`setOrientation: expected 'horizontal' or 'vertical', got "${orientation}"`);
          }
          this._orientation = orientation;
          this._reapplyOrientation();
          return this;
        }

        // Vertical CSS makes the trigger row stack its buttons and pushes the
        // drawer to one side; the anchor class picks which side. Right-corner
        // toolbars anchor left (drawer opens toward canvas center), and
        // vice-versa. Horizontal mode anchors below.
        _reapplyOrientation() {
          const tb = this._tb;
          tb.classList.remove("tc-toolbar-vertical", "tc-toolbar-anchor-left", "tc-toolbar-anchor-right", "tc-toolbar-anchor-bottom");
          if (this._orientation === "vertical") {
            const anchor = this._position.endsWith("-right") ? "left" : "right";
            tb.classList.add("tc-toolbar-vertical", `tc-toolbar-anchor-${anchor}`);
          } else {
            tb.classList.add("tc-toolbar-anchor-bottom");
          }
        }
      }

      // canvas.ui - top-level UI namespace. Currently only owns .toolbar, but
      // exists as a namespace so future UI surfaces (overlays, status, etc.)
      // can slot in here.
      class UIAPI {
        constructor(canvas) {
          this._c = canvas;
          this.toolbar = {
            canvas: new CanvasToolbarController(canvas, canvas.toolbar),
            node:   new ToolbarController(canvas, canvas.nodeToolbar, "node"),
            edge:   new ToolbarController(canvas, canvas.edgeToolbar, "edge"),
          };
        }
      }

      //////////////////////////////////////////////////////////////////////////////////////
      // Canvas ///////////////////////////////////////////////////////////////////////////
      ////////////////////////////////////////////////////////////////////////////////////
      class Canvas {

        constructor(root, data={nodes: [], edges: []}, options={}) {

          this.resolveFile = options.resolveFile || defaultResolveFile;
          this.convertMarkdown = options.convertMarkdown || defaultConvertMarkdown;
          // Optional host-supplied sanitizer (e.g. DOMPurify) applied to any HTML
          // crossing into the DOM via renderUntrustedHTML. Null = identity (the
          // built-in convertMarkdown escapes; a host converter owns its escaping).
          this.sanitize = (typeof options.sanitize === "function") ? options.sanitize : null;
          // Embed policy (2.2): link nodes load arbitrary attacker-supplied URLs
          // into a sandboxed <iframe>. By default we do NOT embed - link nodes
          // render as an inert clickable card. A host opts in either with
          // allowEmbeds:true (embed every link url verbatim) or, for fine control,
          // resolveEmbed(url) => url|null (return a vetted/rewritten url to embed,
          // or a falsy value to keep the card). resolveEmbed takes precedence.
          this.allowEmbeds = options.allowEmbeds === true;
          this.resolveEmbed = (typeof options.resolveEmbed === "function") ? options.resolveEmbed : null;
          // Inbound data contract (Phase 4). By default the load-time gate
          // (_normalizeData) repairs untrusted .canvas data - coercing bad
          // geometry, dropping orphan edges, deduping/generating ids - and
          // reports what it changed (canvas.lastLoadReport + 'load' event).
          // strict:true instead throws on any normalization issue, with the
          // report attached as err.report, for hosts that prefer to reject bad
          // data loudly rather than silently repair it.
          this._strict = options.strict === true;
          this.listFiles = options.listFiles || null;
          this.listImageFiles = options.listImageFiles || null;
          this.onDataChange   = typeof options.onDataChange   === "function" ? options.onDataChange   : null;
          this.onSaveRequest = typeof options.onSaveRequest === "function" ? options.onSaveRequest : null;
          this.onContextMenu = typeof options.onContextMenu === "function" ? options.onContextMenu : null;
          this.onFileDrop    = typeof options.onFileDrop    === "function" ? options.onFileDrop    : null;
          // Accessibility : host-supplied callback invoked once per
          // node/edge on first _applyA11y(). Return a partial accessibility
          // object (label, description, role, …) to override derived
          // defaults. Persisted authored data overrides this; runtime
          // setAccessibility() overrides both. Errors are swallowed so a
          // buggy host doesn't break load.
          this._accessibilityHints = typeof options.accessibilityHints === "function"
            ? options.accessibilityHints
            : null;

          // Keyboard focus . focusedNode is the single node that
          // owns keyboard focus, distinct from selectedNodes (which is
          // a multi-element set). Roving tabindex: exactly one node has
          // tabindex=0 at a time (the focused one), all others -1, so
          // Tab enters/leaves the canvas in a single hop. Arrow keys
          // move focus when no selection is present (nav mode); when a
          // selection exists, they continue to move selected nodes
          // (existing move mode). M toggles an explicit nav-mode
          // override so users can navigate without losing a selection.
          this.focusedNode = null;
          this._navigationModeOverride = null; // null | 'navigate'
          this._isDirty  = false;
          this._isSaving = false;

          // Event bus. Hosts can subscribe via canvas.on(event, fn) / off / once.
          // onDataChange is auto-subscribed below for back-compat with the
          // constructor-callback pattern. onSaveRequest is NOT auto-subscribed
          // because _handleSaveClick must await the primary handler - instead
          // 'saveRequest' is emitted alongside for additional observers.
          this._emitter = new Map();
          if (this.onDataChange) this.on('dataChange', this.onDataChange);
          if (this.onContextMenu) this.on('contextMenu', this.onContextMenu);
          if (this.onFileDrop) this.on('fileDrop', this.onFileDrop);
          // keep the hidden edge mirror in sync with edge deletions
          // from any path (deleteEdge, deleteNode's incident sweep,
          // _deleteSelection). Bulk wipes (clearCanvas, history._apply)
          // bypass this by short-circuiting _edges directly - they call
          // _resetA11yMirror() themselves.
          this.on('edgeDelete', (edge) => {
            if (typeof edge._destroyA11yMirror === "function") edge._destroyA11yMirror();
            this._refreshA11yNodeSummary(this.getNode(edge.fromNode));
            this._refreshA11yNodeSummary(this.getNode(edge.toNode));
          });
          // Surgical edge-add refresh. Catches createEdge and the two
          // edge-draft commit branches (all three already emit
          // edgeCreate). Initial-load goes through createNodesAndEdges
          // which does its own bulk refresh.
          this.on('edgeCreate', (edge) => {
            this._refreshA11yNodeSummary(this.getNode(edge.fromNode));
            this._refreshA11yNodeSummary(this.getNode(edge.toNode));
          });

          // announcement-pipeline state. _pendingDeleteCounts
          // batches rapid-fire delete events so a multi-select wipe
          // reads as "Deleted 3 nodes" instead of three separate
          // events. _selectionAnnounceTimer debounces selection
          // change announcements so marquee drag (which fires
          // selectionChange per frame) doesn't spam the user.
          this._pendingDeleteCounts = { nodes: 0, edges: 0 };
          this._pendingDeleteTimer = null;
          this._selectionAnnounceTimer = null;

          this.on('selectionChange', (sel) => {
            this._scheduleSelectionAnnounce(sel);
          });
          this.on('nodeDelete', () => {
            this._pendingDeleteCounts.nodes++;
            this._scheduleDeleteAnnounce();
          });
          this.on('edgeDelete', () => {
            this._pendingDeleteCounts.edges++;
            this._scheduleDeleteAnnounce();
          });
          this.on('edgeCreate', (edge) => {
            const src = this.getNode(edge.fromNode);
            const tgt = this.getNode(edge.toNode);
            const srcLabel = src ? src._effectiveA11y().label : edge.fromNode;
            const tgtLabel = tgt ? tgt._effectiveA11y().label : edge.toNode;
            this._announce("Edge created from " + srcLabel + " to " + tgtLabel + ".");
          });
          this.on('nodeCreate', (node) => {
            this._announce("Created " + node._effectiveA11y().label + ".");
          });
          this.on('nodeEditEnd', ({ committed, changed }) => {
            if (committed && changed) this._announce("Edit saved.");
            else if (!committed)       this._announce("Edit cancelled.");
          });

          // Public API namespaces - attached early so hosts can use them
          // immediately. Methods access live Canvas state at call time, not
          // construction time, so ordering with respect to panX/panY/scale
          // initialization (further below) is fine.
          this.camera      = new CameraAPI(this);
          this.graph       = new GraphAPI(this);
          this.layout      = new LayoutAPI(this);
          this.selection   = new SelectionAPI(this);
          this.io          = new IoAPI(this);
          this.clipboard   = new ClipboardAPI(this);
          // Permissions namespace. Apply readOnly first (bulk shortcut), then
          // overlay any explicit permissions map so the latter wins. The
          // _onChange path is a no-op for now because the toolbar UI hasn't
          // been built yet (PermissionsAPI._onChange guards on this.toolbar).
          this.permissions = new PermissionsAPI(this);
          if (options.readOnly === true) this.permissions.setReadOnly(true);
          if (options.permissions && typeof options.permissions === 'object') {
            this.permissions.setAll(options.permissions);
          }

          // Keyboard shortcuts. Build per-instance entries from
          // DEFAULT_SHORTCUTS so each Canvas can be rebound independently
          // without leaking state into the module-level table. Handlers
          // are bound to this instance up front. options.shortcuts is
          // applied after defaults so a host-supplied override wins.
          this._shortcuts = new Map();
          this._shortcutIndex = new Map();
          for (const def of DEFAULT_SHORTCUTS) {
            const handler = this[def.handlerName];
            if (typeof handler !== "function") {
              console.warn('TzaraCanvas: missing handler "' + def.handlerName + '" for action "' + def.action + '"');
              continue;
            }
            const n = normalizeBinding(def.defaults);
            this._shortcuts.set(def.action, {
              action:      def.action,
              description: def.description,
              group:       def.group,
              defaults:    def.defaults.slice(),
              current:     n.display.length ? n.display : null,
              _canonical:  n.canonical,
              handler:     handler.bind(this),
              builtin:     true,
            });
          }
          this._rebuildShortcutIndex();
          this.shortcuts = new ShortcutsAPI(this);
          if (options.shortcuts && typeof options.shortcuts === "object") {
            for (const action of Object.keys(options.shortcuts)) {
              this.shortcuts.bind(action, options.shortcuts[action]);
            }
          }

          this.resizeEdgeSize = options.resizeEdgeSize ?? 8;
          this.outlineExtent = options.outlineExtent ?? 4;
          this._initialSnapToGrid  = options.snapToGrid    !== undefined ? !!options.snapToGrid    : true;
          this._initialSnapToNodes = options.nodeAlignment !== undefined ? !!options.nodeAlignment : true;
          this._initialCreateNodeOnDrop = options.createNodeOnDrop !== undefined ? !!options.createNodeOnDrop : true;

          // Background grid + image. Grid spacing follows this.gridSize so a
          // visible grid lines up exactly with snap-to-grid targets.
          const _gridStyles = { off: 1, lines: 1, dots: 1 };
          this.showGrid = _gridStyles[options.showGrid] ? options.showGrid : 'off';
          this.gridMajorEvery = Number.isFinite(options.gridMajorEvery) ? Math.max(0, options.gridMajorEvery|0) : 5;
          this.backgroundImage    = (typeof options.backgroundImage === "string" && options.backgroundImage) ? options.backgroundImage : null;
          this.backgroundSize     = options.backgroundSize || 'auto';
          this.backgroundOpacity  = Number.isFinite(options.backgroundOpacity) ? Math.min(1, Math.max(0, options.backgroundOpacity)) : 1;
          // Multi-layer background: gradient sits below image, color below
          // both. All three pan/zoom with the world (see background_layer).
          this.backgroundGradient = (typeof options.backgroundGradient === "string" && options.backgroundGradient) ? options.backgroundGradient : null;
          this.backgroundColor    = (typeof options.backgroundColor === "string" && options.backgroundColor) ? options.backgroundColor : null;

          this.MouseStates =  Object.freeze({
            NONE: 0x0,
            DOWN: 0x1,
            MOVE: 0x2,
            UP: 0x4,
            DOUBLE: 0x8, 

            LEFT: 0x10,
            RIGHT: 0x20,

            SHIFT: 0x100,
            CTRL: 0x200,
            ALT: 0x400,
          });

          this.CanvasStates = Object.freeze({
            NONE: 0x0,
            MOVING: 0x1,
            EDITING: 0x2,
            RESIZING: 0x4,
            CONNECTING: 0x8,
            SELECTING: 0x10,
            ZOOMING: 0x20,

          });

          // is this even usefull? 
          this.mouseState = this.MouseStates.NONE;  
          this.canvasState = this.CanvasStates.NONE;
          this.activeElement = null;
  

         /*
            <div id="canvas">    outer_container
                <div>            container, overflow hidden, relative,  (viewport)
                  <div>          drawing_container  (content)
                      <canvas>
                  </div>
                  <div>          hitbox_container 

                  </div>
                </div>
              </div><

              viewport style
                position: relative;
                width: 800px;
                height: 600px;
                overflow: hidden;     
                border: 2px solid #333;
                background: #fafafa;

              content style
                position: absolute;
                top: 0;
                left: 0;
                border:1px solid green;
                transform-origin: 0 0; 

         */


          this.outer_container = (typeof root === "string") ? document.getElementById(root) : root;
          if (!this.outer_container) {
            throw new Error("Canvas: root element not found");
          }
          // Prefix for all element ids this instance writes to the DOM (node
          // ids, a11y description/list ids, help-dialog ids). Always derived
          // from a private generator - never the host element's id - so DOM ids
          // can't be coupled to host-controlled strings or collide with the host
          // page. CSS scoping uses the .tzara-canvas-root class (added below),
          // not this id, so nothing visual depends on it.
          this._instanceId = "tc" + this._newId();

          // Mark the root so CSS variables scoped to .tzara-canvas-root apply
          // to this instance's subtree, and so the runtime-injected scrollbar
          // stylesheet's .tzara-canvas-root-prefixed selectors match.
          this.outer_container.classList.add('tzara-canvas-root');

          // Accessibility . Mark the host element as an application
          // landmark so screen readers pass keystrokes through to our
          // handlers instead of intercepting them for browse-mode
          // navigation - the canvas owns its keyboard surface. The
          // aria-label text is refreshed by _updateWrapperAriaLabel()
          // at end-of-constructor and after each _markDirty(). The
          // hidden <h2> gives a heading-navigation landing point.
          this.outer_container.setAttribute('role', 'application');
          this.outer_container.setAttribute('aria-roledescription', 'canvas graph');
          // tabindex=0 makes the canvas a real keyboard tab stop, so a
          // keyboard-only user can Tab onto it even before any node is
          // focused. Built-in shortcuts are scoped to this element (the
          // keydown listener lives here, not on window - see _setupHandlers),
          // so they only fire when focus is inside the canvas and never
          // hijack host-page keys. Roving tabindex on nodes still drives
          // intra-canvas navigation once focus is inside.
          if (!this.outer_container.hasAttribute('tabindex')) {
            this.outer_container.setAttribute('tabindex', '0');
          }
          this._a11yHeading = document.createElement('h2');
          this._a11yHeading.className = 'tc-sr-only';
          this._a11yHeading.textContent = 'Canvas graph';
          this.outer_container.appendChild(this._a11yHeading);

          // Accessibility : hidden DOM mirror of the edge graph.
          //   _a11yEdgesList - <ul> of edges, navigable as a list
          //     landmark. Each CanvasEdge owns one <li>; the text is
          //     refreshed by edge._applyA11y() from its effective
          //     accessibility data (host-authored label/description
          //     win over the derived "Edge from X to Y" string).
          //   _a11yDescriptions - container for per-node connections
          //     summary <span>s. Each CanvasNode owns one span; its
          //     id is referenced from the node's aria-describedby.
          //     Living outside _dom prevents double-announcement when
          //     a screen reader browses the node content.
          this._a11yEdgesList = document.createElement('ul');
          this._a11yEdgesList.className = 'tc-sr-only';
          this._a11yEdgesList.setAttribute('role', 'list');
          this._a11yEdgesList.setAttribute('aria-label', 'Connections');
          this.outer_container.appendChild(this._a11yEdgesList);

          this._a11yDescriptions = document.createElement('div');
          this._a11yDescriptions.className = 'tc-sr-only';
          this.outer_container.appendChild(this._a11yDescriptions);

          // Accessibility : polite live region for announcing
          // state changes (selection, delete, edit commit, edge
          // create, mode toggle). role="status" implies aria-live=
          // "polite" and aria-atomic="true"; both are set explicitly
          // for AT that don't infer from role alone.
          this._a11yLiveRegion = document.createElement('div');
          this._a11yLiveRegion.className = 'tc-sr-only';
          this._a11yLiveRegion.setAttribute('role', 'status');
          this._a11yLiveRegion.setAttribute('aria-live', 'polite');
          this._a11yLiveRegion.setAttribute('aria-atomic', 'true');
          this.outer_container.appendChild(this._a11yLiveRegion);

          // Per-instance theme cache. Populated from computed CSS variables
          // on the root so a host can override any --tc-* at any ancestor.
          // Refreshed on prefers-color-scheme change; hosts that flip theme
          // programmatically should call this.refreshTheme() afterward.
          this._themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
          this._readTheme = () => {
            const cs = getComputedStyle(this.outer_container);
            const v = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
            return {
              isDark:          this._themeMedia.matches,
              accent:          v('--tc-accent', '#2979ff'),
              // CSS-var-derived default color for the phantom (draft) edge.
              // Renamed from 'edgePreview' so canvas.edgePreviewStyle() / the
              // theme.edgePreviewStyle object can take that slot. The actual
              // preview stroke layers: _edgePreviewStyle.stroke -> this color.
              edgePreviewColor: v('--tc-edge-preview', '#2979ff'),
              marqueeFill:     v('--tc-marquee-fill', '#D2BCE5'),
              marqueeStroke:   v('--tc-marquee-stroke', '#000000'),
              arrowFill:       v('--tc-arrow-fill', '#000000'),
              borderFallback:  v('--tc-border', '#c8ccd4'),
              selectionShadow: v('--tc-selection-shadow', 'rgba(0,0,0,0.53)'),
              hitboxBg:        v('--tc-hitbox-bg', 'rgba(252,252,255,0.13)'),
              guideStroke:     v('--tc-guide-stroke', '#e91e63'),
              surface:         v('--tc-surface', '#ffffff'),
              gridStroke:      v('--tc-grid-stroke', 'rgba(0,0,0,0.08)'),
              gridStrokeMajor: v('--tc-grid-stroke-major', 'rgba(0,0,0,0.18)'),
              gridDot:         v('--tc-grid-dot', 'rgba(0,0,0,0.22)'),
              gridDotMajor:    v('--tc-grid-dot-major', 'rgba(0,0,0,0.45)'),
              // Overrides for the 'default' color preset. Empty string means
              // unset; canvasColor's built-in default applies. Affects
              // nodes/edges whose .color === 'default'. Explicit hex strings
              // are untouched (those are deliberate user choices).
              nodeBgDefault:     v('--tc-node-bg-default',     ''),
              nodeBorderDefault: v('--tc-node-border-default', ''),
              edgeStrokeDefault: v('--tc-edge-stroke-default', ''),
              // Overrides for the 1..6 color presets. Same semantics:
              // unset (empty string) falls through to canvasColor's
              // hardcoded preset. Edges use the matching *Border value as
              // their stroke. theme.palette (set via setTheme) takes
              // precedence over these CSS vars.
              color1Bg:     v('--tc-color-1-bg',     ''),
              color1Border: v('--tc-color-1-border', ''),
              color2Bg:     v('--tc-color-2-bg',     ''),
              color2Border: v('--tc-color-2-border', ''),
              color3Bg:     v('--tc-color-3-bg',     ''),
              color3Border: v('--tc-color-3-border', ''),
              color4Bg:     v('--tc-color-4-bg',     ''),
              color4Border: v('--tc-color-4-border', ''),
              color5Bg:     v('--tc-color-5-bg',     ''),
              color5Border: v('--tc-color-5-border', ''),
              color6Bg:     v('--tc-color-6-bg',     ''),
              color6Border: v('--tc-color-6-border', ''),
            };
          };
          if (!document.getElementById("tzara-canvas-styles")) {
            const themeEl = document.createElement("style");
            themeEl.id = "tzara-canvas-styles";
            themeEl.textContent = _tzaraCanvasStyles;
            document.head.appendChild(themeEl);
          }

          this._palette = this._readTheme();
          this._onThemeChange = () => {
            this._palette = this._readTheme();
            if (typeof this.requestDraw === 'function') this.requestDraw();
          };
          this._themeMedia.addEventListener('change', this._onThemeChange);

          // Effect classes for node API (pulse / flash / highlight / badge / widget slot).
          // Injected once per document; scoped under .tzara-canvas-root so it
          // can't leak into a host page's other DOM.
          if (!document.getElementById("tzara-canvas-node-effects")) {
            const fxEl = document.createElement("style");
            fxEl.id = "tzara-canvas-node-effects";
            fxEl.textContent = `
              @keyframes tc-pulse {
                0%   { box-shadow: 0 0 0 0 var(--tc-fx-color, var(--tc-pulse-color, #2979ff)); }
                70%  { box-shadow: 0 0 0 14px rgba(0,0,0,0); }
                100% { box-shadow: 0 0 0 0 rgba(0,0,0,0); }
              }
              .tzara-canvas-root .canvas-node.tc-pulse {
                animation: tc-pulse var(--tc-fx-duration, 600ms) ease-out var(--tc-fx-count, 1);
              }
              .tzara-canvas-root .canvas-node.tc-flash {
                transition: background-color var(--tc-fx-duration, 400ms) ease-out;
              }
              .tzara-canvas-root .canvas-node.tc-highlight {
                box-shadow: 0 0 0 3px var(--tc-fx-color, var(--tc-highlight-color, #2979ff)),
                            0 0 12px 2px var(--tc-fx-color, var(--tc-highlight-color, #2979ff));
              }
              .tzara-canvas-root .tc-node-badge {
                position: absolute;
                transform: translate(-50%, -50%);
                min-width: 18px;
                height: 18px;
                padding: 0 5px;
                box-sizing: border-box;
                border-radius: 9px;
                background: #d33;
                color: #fff;
                font: 11px/18px system-ui, sans-serif;
                text-align: center;
                pointer-events: none;
                z-index: 250;
                box-shadow: 0 1px 3px rgba(0,0,0,0.4);
              }
              .tzara-canvas-root .tc-node-widget {
                position: relative;
                z-index: 5;
              }
            `;
            document.head.appendChild(fxEl);
          }

          if (!document.getElementById("tzara-canvas-node-styles")) {
            const styleEl = document.createElement("style");
            styleEl.id = "tzara-canvas-node-styles";
            styleEl.textContent = `
              .tzara-canvas-root .canvas-node,
              .tzara-canvas-root .canvas-node-scroll { scrollbar-width: thin; scrollbar-color: var(--tc-scrollbar-thumb, #888) transparent; }
              .tzara-canvas-root .canvas-node::-webkit-scrollbar,
              .tzara-canvas-root .canvas-node-scroll::-webkit-scrollbar { width: 6px; }
              .tzara-canvas-root .canvas-node::-webkit-scrollbar-track,
              .tzara-canvas-root .canvas-node-scroll::-webkit-scrollbar-track { background: transparent; }
              .tzara-canvas-root .canvas-node::-webkit-scrollbar-thumb,
              .tzara-canvas-root .canvas-node-scroll::-webkit-scrollbar-thumb { background: var(--tc-scrollbar-thumb, #888); border-radius: 3px; }
              .tzara-canvas-root .canvas-node::-webkit-scrollbar-thumb:hover,
              .tzara-canvas-root .canvas-node-scroll::-webkit-scrollbar-thumb:hover { background: var(--tc-scrollbar-thumb-hover, #666); }
              .tzara-canvas-root .canvas-node::-webkit-scrollbar-button,
              .tzara-canvas-root .canvas-node-scroll::-webkit-scrollbar-button { display: none; width: 0; height: 0; }
            `;
            document.head.appendChild(styleEl);
          }
          // Count this instance against the shared <head> styles so the last
          // destroy() can reclaim them (see _releaseSharedStyles).
          _tzaraLiveInstances++;
          var viewport = this.outer_container.getBoundingClientRect();
          this.outer_container.style.scale = 1;

          this._handlers = {
            pointermove:    e => this.event_mousemove(e),
            pointerleave:   e => this.event_mouseleave(e),
            pointerdown:    e => this.event_mousedown(e),
            pointerup:      e => this.event_mouseup(e),
            pointercancel:  e => this.event_mouseup(e),
            dblclick:       e => this.event_dblclick(e),
            wheel:          e => this.event_wheel(e),
            contextmenu:    e => this.event_contextmenu(e),
            newNode:        e => this.event_newNode(e),
            newFileNode:    e => this.event_newFileNode(e),
            newLinkNode:    e => this.event_newLinkNode(e),
            zoomIn:         e => this.event_zoomIn(e),
            zoomOut:        e => this.event_zoomOut(e),
            resetZoom:      e => this.event_resetZoom(e),
            keydown:        e => this.event_keydown(e),
            blur:           e => this._cancelEdgeDraft(),
            dragenter:      e => this.event_dragenter(e),
            dragover:       e => this.event_dragover(e),
            dragleave:      e => this.event_dragleave(e),
            drop:           e => this.event_drop(e),
          };

          this.container = document.createElement("div");
          this.container.style.overflow = "hidden";
          this.container.style.position = "relative";
          this.container.style.width = "100%";
          this.container.style.height = "100%";

          this.outer_container.appendChild(this.container);


        // Snap / grid settings - initialized here (before toolbar build) so
        // the settings panel can bind to them when constructed.
        this.gridSize = 20;
        this.snapToGrid = this._initialSnapToGrid;
        this.snapToNodes = this._initialSnapToNodes;
        this.createNodeOnDrop = this._initialCreateNodeOnDrop;
        this.snapThreshold = 6;
        this._activeGuides = [];
        this.dragOriginWorldX = 0;
        this.dragOriginWorldY = 0;

        if (this.onSaveRequest) {
          this._handlers.saveCanvas = () => this._handleSaveClick();
        }
        this._handlers.resetCanvas = () => this._handleResetClick();

        // Build the top-level canvas toolbar and the file / link / settings
        // panels. Extracted into a method so permission changes can rebuild
        // the UI to reflect the new allowed operations.
        this._buildCanvasUI();




          // Viewport-fixed background layer - fills the canvas viewport
          // regardless of pan/zoom, so a solid color or CSS gradient set via
          // setBackground({color, gradient}) acts as true canvas chrome
          // instead of a finite painted square the user can pan past.
          // Image-typed backgrounds stay on background_layer below, which is
          // world-transformed (images have a natural position in world space).
          this.background_fixed_layer = document.createElement("div");
          this.background_fixed_layer.style.position = "absolute";
          this.background_fixed_layer.style.top = "0";
          this.background_fixed_layer.style.left = "0";
          this.background_fixed_layer.style.width = "100%";
          this.background_fixed_layer.style.height = "100%";
          this.background_fixed_layer.style.pointerEvents = "none";

          // Background container - a separate world-transformed layer that
          // sits BELOW the <canvas> in DOM order so bezier curves drawn on
          // the canvas render on top of any background image. (drawing_container
          // is appended AFTER the canvas and would otherwise obscure curves.)
          // Its transform is kept in sync with drawing_container via
          // updateTransform().
          this.background_container = document.createElement("div");
          this.background_container.style.position = "absolute";
          this.background_container.style.top = 0;
          this.background_container.style.left = 0;
          this.background_container.style.transformOrigin = "0 0";
          this.background_container.style.pointerEvents = "none";

          // Background image layer. Lives inside background_container so it
          // inherits the world transform (pans/zooms with content). Opacity
          // is set here rather than on the container so it doesn't bleed
          // into anything else.
          //
          // background_container has no intrinsic size, so the layer is
          // given a fixed large extent centered on world origin and
          // background-position is offset by the same amount so the image's
          // top-left sits at world (0,0).
          this._bgLayerExtent = 50000;
          this.background_layer = document.createElement("div");
          this.background_layer.style.position = "absolute";
          this.background_layer.style.left = (-this._bgLayerExtent) + "px";
          this.background_layer.style.top  = (-this._bgLayerExtent) + "px";
          this.background_layer.style.width  = (2 * this._bgLayerExtent) + "px";
          this.background_layer.style.height = (2 * this._bgLayerExtent) + "px";
          this.background_layer.style.pointerEvents = "none";
          this.background_layer.style.backgroundRepeat = "no-repeat";
          this.background_layer.style.backgroundPosition = this._bgLayerExtent + "px " + this._bgLayerExtent + "px";
          this.background_container.appendChild(this.background_layer);
          this._applyBackground();

          // Group container - a world-transformed layer between the background
          // and the edge <canvas> in DOM order. Group node DOMs are appended
          // here (see Node constructor) instead of drawing_container so edges
          // visually pass *over* group bodies (groups are containers/frames,
          // not content). Content nodes stay in drawing_container, which is
          // above the canvas. Resulting stack: bg -> groups -> edges -> nodes.
          // Transform is kept in sync via updateTransform(). No pointerEvents
          // override: groups must receive clicks for label editing, identical
          // to the previous behavior when they lived in drawing_container.
          // Marquee container - a world-transformed layer that sits between
          // background_container and group_container in DOM order. The
          // marquee_el inside is sized/positioned to the active selection
          // rectangle (in world coords) so it pans/zooms with content while
          // staying visually behind groups. Transform synced via
          // updateTransform().
          this.marquee_container = document.createElement("div");
          this.marquee_container.style.position = "absolute";
          this.marquee_container.style.top = 0;
          this.marquee_container.style.left = 0;
          this.marquee_container.style.transformOrigin = "0 0";
          this.marquee_container.style.pointerEvents = "none";

          this.marquee_el = document.createElement("div");
          this.marquee_el.className = "canvas-marquee";
          this.marquee_container.appendChild(this.marquee_el);

          this.group_container = document.createElement("div");
          this.group_container.style.position = "absolute";
          this.group_container.style.top = 0;
          this.group_container.style.left = 0;
          this.group_container.style.transformOrigin = "0 0";
          // Form a stacking context so the group _dom's explicit zIndex:10
          // (set on every group node at construction) stays *inside* this
          // layer instead of escaping into the parent container's stacking
          // context - where it would paint above the edge <canvas>.
          this.group_container.style.isolation = "isolate";

          this.drawing_container = document.createElement("div");
          this.drawing_container.style.position = "absolute";
          this.drawing_container.style.top = 0;
          this.drawing_container.style.left = 0;
          this.drawing_container.style.transformOrigin = "0 0";
          


          this.hitbox_container = document.createElement("div");
          this.hitbox_container.style.transformOrigin = "0 0";

          this.canvas = document.createElement("canvas");
          this.ctx = this.canvas.getContext("2d");

          this._dpr = window.devicePixelRatio || 1;
          this._sizeCanvas();
          // Must be positioned for stacking to be predictable: background_container
          // is position:absolute, so leaving the <canvas> non-positioned makes it
          // paint *below* the background (Firefox: "z-index has no effect on
          // non-positioned elements"). Positioning it here lets natural DOM order
          // - background_container -> canvas -> drawing_container - stack edges
          // above the background and below node DOM. Do not set an explicit
          // z-index: drawing_container has auto z-index, so any non-auto value
          // here would float edges above nodes.
          this.canvas.style.position = "absolute";
          this.canvas.style.top = "0";
          this.canvas.style.left = "0";
          this.scale = 1.0;

          this.offsetX = 0;
          this.offsetY = 0;
          this.dragging = false;
          // Refcount of in-flight programmatic animations (animateTo/Size/Bounds,
          // LayoutAPI batches, ViewportAPI tweens). The edge renderer skips
          // shadow blur while >0, mirroring the existing dragging shortcut -
          // shadows on every stroked bezier dominate per-frame paint cost.
          this._fastDrawCount = 0;
          this._dragPending = null;
          this.dragStartX = 0;
          this.dragStartY = 0;
        
          this.marqueeStartX = 0;
          this.marqueeStartY = 0;
          this.marqueeActive = false; 

          this.selectedNodes = [];
          this.selectedEdge = null;
          this._clickedEdge = null;
          this.mouseX = 0;
          this.mouseY = 0;
          this.editing = false;
          this.edgeDraft = null;

          this.panX = 0;
          this.panY = 0;
          this.isPanning = false;
          this._panDidMove = false;

          this.resizing = null;
          this.resizeSides = null;

          // Stack (bottom-up):
          //   background_fixed_layer  - viewport-fixed color/gradient
          //   background_container    - world-transformed image
          //   marquee_container       - marquee selection rectangle
          //   group_container         - group node bodies
          //   canvas                  - edge curves
          //   drawing_container       - content node DOMs (appended later)
          this.container.appendChild(this.background_fixed_layer);
          this.container.appendChild(this.background_container);
          this.container.appendChild(this.marquee_container);
          this.container.appendChild(this.group_container);
          this.container.appendChild(this.canvas);

          this._resizeObserver = new ResizeObserver(() => {
            if (!this.outer_container) return;
            // _sizeCanvas() returns false (and does nothing) when the viewport is
            // zero-sized or the backing store already matches, subsuming the old
            // manual guards.
            if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = 0; }
            if (this._sizeCanvas()) this.draw();
          });
          this._resizeObserver.observe(this.outer_container);
          this._watchDpr();

          this.container.appendChild(this.drawing_container);

          this.hitbox_container.style.border = "1px dotted solid";
          this.hitbox_container.style.backgroundColor = "var(--tc-hitbox-bg)"
          this.hitbox_container.style.position = "absolute";
          this.hitbox_container.style.left = "0px";
          this.hitbox_container.style.top = "0px";
          this.hitbox_container.style.width = "100%";
          this.hitbox_container.style.height= "100%";
          this.hitbox_container.style.zIndex = 1000;
          // Disable native touch panning so pointer events reach our handlers,
          // and let pointer capture work for touch/pen as well as mouse.
          this.hitbox_container.style.touchAction = "none";

          this.container.appendChild(this.hitbox_container);

          // Overlay container for floating selection toolbars. Sits above
          // hitbox_container so toolbar controls receive clicks; it doesn't
          // capture events itself (pointer-events: none), only its children do.
          this.toolbar_container = document.createElement("div");
          this.toolbar_container.style.position = "absolute";
          this.toolbar_container.style.left = "0";
          this.toolbar_container.style.top = "0";
          this.toolbar_container.style.width = "100%";
          this.toolbar_container.style.height = "100%";
          this.toolbar_container.style.pointerEvents = "none";
          this.toolbar_container.style.zIndex = 2000;
          this.container.appendChild(this.toolbar_container);

          this._buildNodeToolbar();
          this._buildEdgeToolbar();

          // canvas.ui namespace. Wired after all three toolbars exist so the
          // controllers can wrap the live DOM. Constructor-options for
          // toolbars are applied immediately so `hidden: true` takes effect
          // before the first paint.
          this.ui = new UIAPI(this);
          this._applyToolbarOptions(options.toolbars);

          this.hitbox_container.addEventListener("pointermove",   this._handlers.pointermove, false);
          this.hitbox_container.addEventListener("pointerleave",  this._handlers.pointerleave, false);
          this.hitbox_container.addEventListener("pointerdown",   this._handlers.pointerdown, false);
          this.hitbox_container.addEventListener("pointerup",     this._handlers.pointerup, false);
          this.hitbox_container.addEventListener("pointercancel", this._handlers.pointercancel, false);
          this.hitbox_container.addEventListener("dblclick",      this._handlers.dblclick, false);
          this.hitbox_container.addEventListener("wheel",         this._handlers.wheel, false);
          this.hitbox_container.addEventListener("contextmenu",   this._handlers.contextmenu);
          this.hitbox_container.addEventListener("dragenter",     this._handlers.dragenter, false);
          this.hitbox_container.addEventListener("dragover",      this._handlers.dragover, false);
          this.hitbox_container.addEventListener("dragleave",     this._handlers.dragleave, false);
          this.hitbox_container.addEventListener("drop",          this._handlers.drop, false);
          // Keydown is scoped to outer_container (not window): events only
          // arrive when focus is inside the canvas, so built-in shortcuts
          // can't hijack host-page keys (Delete/Backspace/?/arrows/Escape).
          // outer_container contains the canvas, all node DOM, and the help
          // dialog, so a focused node's keydown bubbles up to here. blur
          // stays on window - it tracks window-level focus loss.
          this.outer_container.addEventListener("keydown", this._handlers.keydown, false);
          window.addEventListener("blur", this._handlers.blur, false);

          this._nodes = [];
          this._edges = [];

          // Canvas-wide default style overrides applied to every node's
          // _dom (camelCase CSS keys, e.g. {borderRadius:'0', fontFamily:'…'}).
          // Layering: preset colors -> canvas defaults -> per-node node.style().
          // null while unset; populated by canvas.defaultNodeStyle({...}).
          this._defaultNodeStyle = null;
          // Optional `(node) => object` form of canvas defaults, applied
          // ON TOP of the static object above so callers can vary defaults
          // per node (e.g. by node.type). Set/cleared via the same
          // defaultNodeStyle() entrypoint - passing a function targets this
          // slot, passing an object targets _defaultNodeStyle. Re-evaluated
          // when set and again whenever a new node is added (via
          // _applyCanvasDefaultStyle).
          this._defaultNodeStyleFn = null;
          // Per-node Set<string> tracking which keys the fn applied, so we
          // can wipe and re-snapshot-restore them cleanly when the fn is
          // replaced or cleared. Lives parallel to _defaultNodeStyleSnapshot.
          this._defaultNodeStyleFnKeys = null;

          // Style overrides for the four "chrome" elements that aren't part
          // of any node/edge but still need themable rendering. All four are
          // null while unset (CSS-var-derived defaults apply) and follow the
          // same merge-with-null-to-clear pattern as _defaultNodeStyle.
          this._marqueeStyle = null;
          this._connectHandleStyle = null;
          this._edgePreviewStyle = null;
          // _selectedNodeDecorator can be an object, a function (node)=>object,
          // or null. The presence of a function vs. object is detected at
          // application time in _updateSelectionStyles().
          this._selectedNodeDecorator = null;
          // _hoveredNodeDecorator has the same shape contract as
          // _selectedNodeDecorator; resolved in _updateHoverStyles(). Null
          // falls back to the built-in ridge-border + drop-shadow look.
          this._hoveredNodeDecorator = null;

          this.raw_data = JSON.parse(JSON.stringify(data));
          this.data = data;

          // History namespace must initialize AFTER nodes/edges exist so the
          // baseline snapshot reflects the loaded state, including the
          // recentering applied by the data setter.
          this.history = new HistoryAPI(this, { depth: options.historyDepth });

          this.requestDraw();

          // Layout-ready signal. The container's getBoundingClientRect() can
          // return zero/wrong dimensions when the canvas is constructed before
          // the browser's first layout pass - this would break framing math in
          // camera.fitAll/focusNode/etc. Resolve after one RAF so consumers
          // (and the camera methods below) can wait for a usable viewport.
          this._readyResolved = false;
          this._readyPromise = new Promise(resolve => {
            if (typeof requestAnimationFrame === 'function') {
              requestAnimationFrame(() => {
                this._readyResolved = true;
                resolve(this);
              });
            } else {
              this._readyResolved = true;
              resolve(this);
            }
          });

          this._updateWrapperAriaLabel();

          // Initial keyboard focus on the first node so Tab into the
          // canvas has somewhere to land. focus:false avoids stealing
          // focus from the rest of the page at construction time.
          if (this._nodes.length > 0) {
            this.setFocusedNode(this._nodes[0], { focus: false });
          }
        }

        // ---- focus management  ----

        // Sets focusedNode and rotates the roving tabindex so the new
        // node is in the Tab order and the old one is out. opts.focus
        // defaults to true - call with focus:false from contexts where
        // the browser is already handling focus (e.g. raw mousedown).
        // No-op (other than ensuring the .focus() call) if the same
        // node is already focused.
        setFocusedNode(node, opts) {
          opts = opts || {};
          const wantFocus = opts.focus !== false;
          const prev = this.focusedNode;
          if (prev && prev !== node && prev._dom) {
            prev._dom.setAttribute("tabindex", "-1");
          }
          if (node && node._dom) {
            node._dom.setAttribute("tabindex", "0");
            this.focusedNode = node;
            if (wantFocus) {
              try { node._dom.focus({ preventScroll: true }); } catch (_) { node._dom.focus(); }
            }
          } else {
            this.focusedNode = null;
          }
        }

        // Returns the effective navigation mode for arrow keys.
        // 'navigate' = arrows move focus between nodes.
        // 'move'     = arrows move selected nodes (existing behavior).
        _arrowMode() {
          if (this._navigationModeOverride === "navigate") return "navigate";
          return this.selectedNodes.length > 0 ? "move" : "navigate";
        }

        // Move focus to the nearest node in the given direction
        // ("left" | "right" | "up" | "down") from the currently focused
        // node. Spatial heuristic: nodes whose center lies in the half-
        // plane on that side, scored by Manhattan distance with a bonus
        // for being roughly axis-aligned. Returns true if focus moved.
        _focusNeighbor(dir) {
          const nodes = this._nodes;
          if (!nodes.length) return false;
          // Anchor: focused node center, or viewport center if none.
          let ax, ay;
          if (this.focusedNode) {
            ax = this.focusedNode.x + this.focusedNode.width  / 2;
            ay = this.focusedNode.y + this.focusedNode.height / 2;
          } else {
            ax = 0; ay = 0;
          }
          let best = null, bestScore = Infinity;
          for (const n of nodes) {
            if (n === this.focusedNode) continue;
            const cx = n.x + n.width  / 2;
            const cy = n.y + n.height / 2;
            const dx = cx - ax, dy = cy - ay;
            // Filter to the directional half-plane (strict).
            if (dir === "left"  && !(dx < -1)) continue;
            if (dir === "right" && !(dx >  1)) continue;
            if (dir === "up"    && !(dy < -1)) continue;
            if (dir === "down"  && !(dy >  1)) continue;
            // Score: axis distance + 2x cross-axis distance - favors
            // neighbors aligned with the chosen axis over diagonal ones.
            let axis, cross;
            if (dir === "left" || dir === "right") { axis = Math.abs(dx); cross = Math.abs(dy); }
            else                                    { axis = Math.abs(dy); cross = Math.abs(dx); }
            const score = axis + cross * 2;
            if (score < bestScore) { bestScore = score; best = n; }
          }
          if (!best) return false;
          this.setFocusedNode(best);
          return true;
        }

        // Document-order first/last node (Home / End).
        _focusFirstNode() {
          if (!this._nodes.length) return false;
          this.setFocusedNode(this._nodes[0]);
          return true;
        }
        _focusLastNode() {
          if (!this._nodes.length) return false;
          this.setFocusedNode(this._nodes[this._nodes.length - 1]);
          return true;
        }

        // Clear focus if it pointed at a node whose DOM was just
        // removed. Falls back to the first still-attached node so the
        // canvas keeps a Tab target. Uses isConnected rather than
        // _nodes membership so multi-delete (which removes all DOMs
        // before filtering the array) doesn't pick a freshly-detached
        // node as the fallback.
        _clearFocusIfRemoved(removedNode) {
          if (this.focusedNode !== removedNode) return;
          this.focusedNode = null;
          const next = this._nodes.find(n =>
            n !== removedNode && n._dom && n._dom.isConnected
          ) || null;
          if (next) this.setFocusedNode(next, { focus: false });
        }

        // ---- a11y mirror plumbing  ----

        // Recompute a single node's connections-summary span content
        // from its current incident edges. Edges with effective
        // accessibility.hidden=true are skipped per the schema. Also
        // prepends the node's own authored description so screen
        // readers announce it via aria-describedby alongside the
        // connections list.
        _refreshA11yNodeSummary(node) {
          if (!node || !node._a11ySummarySpan) return;
          const outgoing = [];
          const incoming = [];
          for (const e of this._edges) {
            const eff = (typeof e._effectiveA11y === "function") ? e._effectiveA11y() : null;
            if (eff && eff.hidden === true) continue;
            if (e.fromNode === node.id) {
              const tgt = this.getNode(e.toNode);
              if (tgt) outgoing.push(tgt._effectiveA11y().label);
            } else if (e.toNode === node.id) {
              const src = this.getNode(e.fromNode);
              if (src) incoming.push(src._effectiveA11y().label);
            }
          }
          const parts = [];
          const ownEff = node._effectiveA11y();
          if (ownEff.description) parts.push(String(ownEff.description));
          if (outgoing.length) parts.push("Connected to: " + outgoing.join(", ") + ".");
          if (incoming.length) parts.push("Connected from: " + incoming.join(", ") + ".");
          node._a11ySummarySpan.textContent = parts.join(" ");
        }

        // Bulk wipe + rebuild of the edge list and per-node summaries.
        // Used by clearCanvas and history._apply, which short-circuit
        // the surgical create/delete listeners by mutating _edges and
        // _nodes directly.
        _resetA11yMirror() {
          if (this._a11yEdgesList) this._a11yEdgesList.innerHTML = "";
          if (this._a11yDescriptions) this._a11yDescriptions.innerHTML = "";
        }

        // ---- shortcut registry plumbing ----

        // Rebuild the canonical-key → action-name map from the per-action
        // _canonical arrays. Cheap (≈14 entries plus host actions) and
        // called from every mutating ShortcutsAPI method. Conflicts
        // (multiple actions on the same canonical key) - the most
        // recently rebuilt entry wins, which in practice means whichever
        // action was bound last.
        _rebuildShortcutIndex() {
          this._shortcutIndex.clear();
          for (const [name, e] of this._shortcuts) {
            if (e.current == null || e._canonical.length === 0) continue;
            for (const c of e._canonical) this._shortcutIndex.set(c, name);
          }
        }

        // If the help dialog is open, re-render it so newly applied
        // bindings show up. Cheap rebuild - the dialog is small.
        _refreshHelpDialogIfOpen() {
          if (!this._helpDialogEl) return;
          this._hideHelpDialog();
          this._showHelpDialog();
        }

        // ---- help dialog  ----

        // Open the keyboard-shortcut help dialog. Renders from HELP_TOPICS
        // for ordering and descriptions; keyboard entries (action-tagged)
        // pull their current key labels from the live shortcut registry,
        // while mouse interactions render their literal keys. Host-
        // registered actions are appended in their declared group (or
        // "Custom"). Disabled actions are skipped. Backdrop click and
        // Escape both close. Focus is moved into the dialog on open and
        // returned on close. Idempotent - calling while open is a no-op.
        _showHelpDialog() {
          if (this._helpDialogEl) return;
          this._helpDialogPrevFocus = document.activeElement;
          const backdrop = document.createElement("div");
          backdrop.className = "tc-help-backdrop";
          backdrop.addEventListener("pointerdown", (e) => {
            if (e.target === backdrop) this._hideHelpDialog();
          });
          const dialog = document.createElement("div");
          dialog.className = "tc-help-dialog";
          dialog.setAttribute("role", "dialog");
          dialog.setAttribute("aria-modal", "true");
          dialog.setAttribute("tabindex", "-1");
          const titleId = this._instanceId + "-help-title";
          dialog.setAttribute("aria-labelledby", titleId);
          const header = document.createElement("header");
          const title = document.createElement("h2");
          title.id = titleId;
          title.textContent = "Keyboard shortcuts";
          const closeBtn = document.createElement("button");
          closeBtn.className = "tc-help-close";
          closeBtn.textContent = "Close";
          closeBtn.setAttribute("aria-label", "Close keyboard shortcut help");
          closeBtn.addEventListener("click", () => this._hideHelpDialog());
          header.appendChild(title);
          header.appendChild(closeBtn);
          dialog.appendChild(header);

          // Collect host-registered actions and merge them into the help
          // template by group. Built-in actions tagged in HELP_TOPICS are
          // resolved from this._shortcuts so their labels track the live
          // binding.
          const customByGroup = new Map();
          for (const [name, e] of this._shortcuts) {
            if (e.builtin) continue;
            const grp = e.group || "Custom";
            if (!customByGroup.has(grp)) customByGroup.set(grp, []);
            customByGroup.get(grp).push({ action: name });
          }
          const renderedCustomGroups = new Set();

          const renderItem = (item) => {
            // Action-tagged: pull keys + description from the registry.
            let keys = item.keys;
            let description = item.description;
            if (item.action) {
              const entry = this._shortcuts.get(item.action);
              if (!entry) return null;            // referenced but unregistered
              if (entry.current == null || entry.current.length === 0) return null; // disabled
              keys = entry.current;
              if (!description) description = entry.description;
            }
            if (!keys || !keys.length) return null;
            const dt = document.createElement("dt");
            keys.forEach((k, i) => {
              if (i > 0) dt.appendChild(document.createTextNode(" or "));
              const kbd = document.createElement("kbd");
              kbd.textContent = k;
              dt.appendChild(kbd);
            });
            const dd = document.createElement("dd");
            dd.textContent = description || "";
            return { dt, dd };
          };

          for (const section of HELP_TOPICS) {
            const items = section.items.slice();
            // Append any host actions that declared this same group so
            // they sit alongside the built-ins they belong with.
            if (customByGroup.has(section.group)) {
              for (const ci of customByGroup.get(section.group)) items.push(ci);
              renderedCustomGroups.add(section.group);
            }
            const rendered = [];
            for (const it of items) {
              const r = renderItem(it);
              if (r) rendered.push(r);
            }
            if (!rendered.length) continue; // skip groups where everything is disabled
            const h3 = document.createElement("h3");
            h3.textContent = section.group;
            dialog.appendChild(h3);
            const dl = document.createElement("dl");
            for (const r of rendered) { dl.appendChild(r.dt); dl.appendChild(r.dd); }
            dialog.appendChild(dl);
          }
          // Remaining host-action groups (no matching HELP_TOPICS entry).
          for (const [grp, items] of customByGroup) {
            if (renderedCustomGroups.has(grp)) continue;
            const rendered = [];
            for (const it of items) {
              const r = renderItem(it);
              if (r) rendered.push(r);
            }
            if (!rendered.length) continue;
            const h3 = document.createElement("h3");
            h3.textContent = grp;
            dialog.appendChild(h3);
            const dl = document.createElement("dl");
            for (const r of rendered) { dl.appendChild(r.dt); dl.appendChild(r.dd); }
            dialog.appendChild(dl);
          }

          backdrop.appendChild(dialog);
          this.container.appendChild(backdrop);
          this._helpDialogEl = backdrop;
          // Defer focus until after the layout pass so screen readers
          // pick up the dialog role + label on entry.
          setTimeout(() => { if (dialog.isConnected) dialog.focus(); }, 0);
        }

        _hideHelpDialog() {
          if (!this._helpDialogEl) return;
          if (this._helpDialogEl.parentNode) {
            this._helpDialogEl.parentNode.removeChild(this._helpDialogEl);
          }
          this._helpDialogEl = null;
          const prev = this._helpDialogPrevFocus;
          this._helpDialogPrevFocus = null;
          if (prev && typeof prev.focus === "function" && prev.isConnected) {
            try { prev.focus({ preventScroll: true }); } catch (_) { prev.focus(); }
          } else if (this.focusedNode && this.focusedNode._dom) {
            try { this.focusedNode._dom.focus({ preventScroll: true }); } catch (_) { this.focusedNode._dom.focus(); }
          }
        }

        // ---- live region announcements  ----

        // Write a single string to the polite aria-live region. Same-
        // text writes get a brief clear-then-rewrite so AT re-announce
        // (some screen readers suppress unchanged textContent). Safe
        // to call before the region exists - silently no-ops.
        _announce(text) {
          if (!this._a11yLiveRegion) return;
          const next = (text == null) ? "" : String(text).trim();
          if (!next) {
            this._a11yLiveRegion.textContent = "";
            this._lastAnnouncement = "";
            return;
          }
          if (next === this._lastAnnouncement) {
            this._a11yLiveRegion.textContent = "";
            const region = this._a11yLiveRegion;
            setTimeout(() => {
              if (region.isConnected) region.textContent = next;
            }, 50);
          } else {
            this._a11yLiveRegion.textContent = next;
          }
          this._lastAnnouncement = next;
        }

        // Coalesce a burst of nodeDelete/edgeDelete events into one
        // announcement. 100ms window - long enough to capture a
        // multi-select wipe (delete fires synchronously per item),
        // short enough that single deletes still feel immediate.
        _scheduleDeleteAnnounce() {
          if (this._pendingDeleteTimer) clearTimeout(this._pendingDeleteTimer);
          this._pendingDeleteTimer = setTimeout(() => {
            this._pendingDeleteTimer = null;
            const { nodes, edges } = this._pendingDeleteCounts;
            this._pendingDeleteCounts = { nodes: 0, edges: 0 };
            const parts = [];
            if (nodes) parts.push("Deleted " + nodes + " node" + (nodes === 1 ? "" : "s"));
            if (edges) parts.push((nodes ? "" : "Deleted ") + edges + " edge" + (edges === 1 ? "" : "s"));
            if (parts.length) this._announce(parts.join(", ") + ".");
          }, 100);
        }

        // Debounce selection-change announcements so marquee drag
        // (which can fire selectionChange every frame as nodes enter
        // / leave the rect) doesn't drown the user in updates. 200ms
        // window - last selection wins, which is what marquee users
        // care about: the final selection on mouseup.
        _scheduleSelectionAnnounce(sel) {
          if (this._selectionAnnounceTimer) clearTimeout(this._selectionAnnounceTimer);
          this._selectionAnnounceTimer = setTimeout(() => {
            this._selectionAnnounceTimer = null;
            const n = sel && sel.nodes ? sel.nodes.length : 0;
            const e = sel && sel.edge ? 1 : 0;
            let text;
            if (n === 0 && e === 0) {
              text = "Selection cleared.";
            } else if (e && !n) {
              text = "Edge selected.";
            } else {
              const parts = [];
              if (n) parts.push(n + " node" + (n === 1 ? "" : "s") + " selected");
              if (e) parts.push("edge selected");
              text = parts.join(", ") + ".";
            }
            this._announce(text);
          }, 200);
        }

        // Refresh the host element's aria-label with current node/edge
        // counts. Cheap (two .length reads) so it can be called from
        // _markDirty after every mutation. P1 ships a count; later phases
        // can layer authored titles on top.
        _updateWrapperAriaLabel() {
          if (!this.outer_container) return;
          const nc = this._nodes ? this._nodes.length : 0;
          const ec = this._edges ? this._edges.length : 0;
          const nWord = nc === 1 ? "node" : "nodes";
          const eWord = ec === 1 ? "edge" : "edges";
          this.outer_container.setAttribute(
            "aria-label",
            "Canvas with " + nc + " " + nWord + " and " + ec + " " + eWord
          );
        }

        // Resolves after the canvas's container has been laid out by the
        // browser at least once, so camera framing math gives correct results.
        // Most consumers don't need this directly - camera.fitAll/focusNode/
        // etc. wait internally when called too early - but it's useful when
        // your own code needs to read container.getBoundingClientRect() or
        // node.bounds() during initialization.
        ready() { return this._readyPromise; }

        toWorld(x, y)  { 
          return { x: (x - this.panX) / this.scale, y: (y - this.panY) / this.scale }; 
        }
        rectToWorld(r) {
          return {
            x: (r.x - this.panX) / this.scale,
            y: (r.y - this.panY) / this.scale,
            width: r.width / this.scale,
            height: r.height / this.scale,

          }
        }

        // World-space hit test for a node's floating label (group / file / link).
        // Used by hitNode for routing and by event_mousemove to choose the grab cursor.
        _hitNodeLabel(n, x, y) {
          const lbl = n.group_label || n.file_label || n.link_label;
          if (!lbl) return false;
          const hb = this.hitbox_container.getBoundingClientRect();
          const r = lbl.getBoundingClientRect();
          const w = this.rectToWorld({ x: r.x - hb.x, y: r.y - hb.y, width: r.width, height: r.height });
          return x >= w.x && x <= w.x + w.width && y >= w.y && y <= w.y + w.height;
        }

        hitNode(x,y) {
          /* This function returns a node if a click at an x,y coordinate would 
          be on top of a node or, in the case o groups, on the group's label which
          is above the group node box.

          This is not a general test of intersection due to group-node behavior.
          */
          for (let i = this._nodes.length - 1; i >= 0; i--) {
              var n = this._nodes[i];

              // Selected nodes draw an outline outside the border; expand the
              // hit bbox by that amount so the visible outline ring is part of
              // the hit area (and falls inside the resize/perimeter strip).
              const ext = this.selectedNodes.includes(n) ? this.outlineExtent : 0;

              if (n.type == "group") {
                // Label is the primary grab zone (moving the group).
                if (this._hitNodeLabel(n, x, y)) return n;

                // Perimeter strip of the group's bbox - enables resize and
                // connection handles on all 4 sides/corners without hijacking
                // the empty interior (children + marquee still pass through).
                // Use DOM's full visual size (content + borders) to match what
                // the user sees - otherwise the right/bottom border forms a
                // dead strip where resize can't trigger.
                const PERIM = 20 + ext;
                const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
                const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
                const x0 = n.x - ext, y0 = n.y - ext;
                const insideBox = x >= x0 && x <= x0 + totalW && y >= y0 && y <= y0 + totalH;
                if (insideBox) {
                  const dL = x - x0, dR = (x0 + totalW) - x;
                  const dT = y - y0, dB = (y0 + totalH) - y;
                  if (Math.min(dL, dR, dT, dB) <= PERIM) return n;
                }
              } else if (n.type == "file") {
                // Floating filename label is a grab/select handle (same role
                // as the link node's URL label).
                if (this._hitNodeLabel(n, x, y)) return n;
                const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
                const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
                const x0 = n.x - ext, y0 = n.y - ext;
                if (x >= x0 && x <= x0 + totalW && y >= y0 && y <= y0 + totalH) {
                  return n;
                }
              } else if (n.type == "link") {
                // Floating URL label is the primary grab/select handle.
                if (this._hitNodeLabel(n, x, y)) return n;
                // Edge strips for resize. Body interior falls through
                // (returns null) so the iframe receives clicks.
                const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
                const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
                const x0 = n.x - ext, y0 = n.y - ext;
                const insideBox = x >= x0 && x <= x0 + totalW && y >= y0 && y <= y0 + totalH;
                if (insideBox) {
                  const EDGE = this.resizeEdgeSize + ext;
                  const dL = x - x0, dR = (x0 + totalW) - x;
                  const dT = y - y0, dB = (y0 + totalH) - y;
                  if (Math.min(dL, dR, dT, dB) <= EDGE) return n;
                }
              } else {
                // Use the DOM's full visual size (content + borders) so the hit
                // zone matches what the user sees - otherwise the right/bottom
                // borders form a dead strip that hitNode never matches.
                // Extend by handlePad so the outer half of the side connector
                // circle (which sits past the border) is still hittable.
                const handlePad = 10;
                const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
                const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
                const x0 = n.x - ext - handlePad, y0 = n.y - ext - handlePad;
                const W = totalW + 2 * handlePad, H = totalH + 2 * handlePad;
                if (x >= x0 && x <= x0 + W && y >= y0 && y <= y0 + H) {
                  return n
                }
              }


            }
          return null;

        }

        hitEdge(x, y) {

            for (const e of this._edges) {
                if (e.hitEdge(x,y)) {
                  return e;
                }
            }
            return null;
        }

        // Continuously gates iframe interactivity for link nodes. The iframe is
        // live only when no canvas gesture is active and the cursor is sitting
        // in the link's body interior (not over the title bar, edge zones, or
        // an overlapping higher-z node). When any iframe is live, the global
        // hitbox layer is set to pointer-events:none so events can reach the
        // iframe; otherwise the hitbox stays interactive for canvas gestures.
        _updateLinkInteractivity() {
          let hasLink = false;
          for (const n of this._nodes) { if (n.type === "link" && n._iframeEl) { hasLink = true; break; } }
          if (!hasLink) return;

          const busy = !!(this.dragging || this.resizing || this.marqueeActive
                         || this._dragPending || this.edgeDraft);

          const x = this.mouseX, y = this.mouseY;
          let topHit = null;
          if (!busy && x != null && y != null) topHit = this.hitNode(x, y);

          let anyOn = false;
          for (const n of this._nodes) {
            if (n.type !== "link") continue;
            if (!n._iframeEl) continue;   // card-mode link: no iframe to gate
            let interactive = false;
            if (!busy && topHit === null && x != null && y != null) {
              const ext = this.selectedNodes.includes(n) ? this.outlineExtent : 0;
              const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
              const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
              const x0 = n.x - ext, y0 = n.y - ext;
              const EDGE = this.resizeEdgeSize + ext;
              const insideBox = x >= x0 && x <= x0 + totalW && y >= y0 && y <= y0 + totalH;
              if (insideBox) {
                const dL = x - x0, dR = (x0 + totalW) - x;
                const dT = y - y0, dB = (y0 + totalH) - y;
                const inEdge = Math.min(dL, dR, dT, dB) <= EDGE;
                if (!inEdge) interactive = true;
              }
            }
            n._setIframeInteractive(interactive);
            if (interactive) anyOn = true;
          }

          this.hitbox_container.style.pointerEvents = anyOn ? "none" : "";
        }

        _computeGroupDragExtras() {
          // Snapshot of nodes contained in any selected group at drag-start,
          // so dragging a group carries its children along. Containment is
          // bbox-inside-bbox, which also pulls in nested groups and their
          // descendants transitively.
          const extras = new Set();
          const sel = new Set(this.selectedNodes);
          const groups = this.selectedNodes.filter(n => n.type === "group");
          if (!groups.length) return [];
          for (const n of this._nodes) {
            if (sel.has(n)) continue;
            for (const g of groups) {
              if (n.x >= g.x && n.y >= g.y &&
                  n.x + n.width  <= g.x + g.width &&
                  n.y + n.height <= g.y + g.height) {
                extras.add(n);
                break;
              }
            }
          }
          return [...extras];
        }



        ////////////////////////////
        ///// MOUSE WHEEL /////////
        //////////////////////////
        updateTransform() {
          const t = `translate(${this.panX}px, ${this.panY}px) scale(${this.scale})`;
          this.drawing_container.style.transform = t;
          if (this.background_container) this.background_container.style.transform = t;
          if (this.marquee_container) this.marquee_container.style.transform = t;
          if (this.group_container) this.group_container.style.transform = t;
          // Host-registered content-space layers track the same world transform.
          if (this._contentLayers) {
            for (const el of this._contentLayers) el.style.transform = t;
          }
        }

        setGrid(style, opts) {
          const allowed = { off: 1, lines: 1, dots: 1 };
          if (!allowed[style]) return this;
          this.showGrid = style;
          if (opts && Number.isFinite(opts.majorEvery)) {
            this.gridMajorEvery = Math.max(0, opts.majorEvery|0);
          }
          if (typeof this.requestDraw === "function") this.requestDraw();
          return this;
        }

        // Canvas-wide node style defaults. Applied to every node's _dom, both
        // existing and future. Two forms, both targeted via the same entry:
        //   canvas.defaultNodeStyle({ borderRadius: '0' })   - set/update object key
        //   canvas.defaultNodeStyle({ borderRadius: null })  - clear one object key
        //   canvas.defaultNodeStyle(null)                    - clear ALL (object + fn)
        //   canvas.defaultNodeStyle()                        - return a copy of object form
        //   canvas.defaultNodeStyle((node) => ({…}))         - set/replace the fn form
        // Layering: preset colors -> object defaults -> fn defaults -> per-node node.style().
        // The fn merges ON TOP of the object form per node, so callers can set
        // a global default and then conditionally override per node.type, etc.
        // The fn is re-evaluated for each existing node when set, and again
        // for each newly-added node via _applyCanvasDefaultStyle.
        // Scope is _dom only; auxiliary labels (group_label, file_label,
        // link_label, edge labels) are not touched.
        // Clearing a key removes the inline value, which also clears any
        // hardcoded constructor default for that key (same caveat as
        // node.style()). To restore a library default after clearing, set
        // it explicitly. To clear just the fn while keeping the object form,
        // pass a fn that returns an empty object: `defaultNodeStyle(() => ({}))`.
        defaultNodeStyle(overrides) {
          if (arguments.length === 0) {
            return this._defaultNodeStyle ? Object.assign({}, this._defaultNodeStyle) : {};
          }

          // Clear-all: walk the union of keys touched by either form on each
          // node and restore from snapshot. Fn-applied keys vary per node, so
          // the union is built per-node. Falling back to '' loses constructor
          // inline values; snapshot restore avoids that.
          if (overrides === null) {
            if (this._defaultNodeStyle || this._defaultNodeStyleFn) {
              const objKeys = this._defaultNodeStyle ? Object.keys(this._defaultNodeStyle) : [];
              for (const n of this._nodes) {
                if (!n._dom) continue;
                const snap = this._defaultNodeStyleSnapshot && this._defaultNodeStyleSnapshot.get(n);
                const fnKeys = this._defaultNodeStyleFnKeys && this._defaultNodeStyleFnKeys.get(n);
                const seen = new Set(objKeys);
                if (fnKeys) for (const k of fnKeys) seen.add(k);
                for (const k of seen) {
                  n._dom.style[k] = (snap && k in snap) ? snap[k] : '';
                }
                // Per-node overrides must continue to win after the restore.
                n._applyStyleOverrides();
              }
              this._defaultNodeStyle = null;
              this._defaultNodeStyleFn = null;
              this._defaultNodeStyleSnapshot = null;
              this._defaultNodeStyleFnKeys = null;
            }
            return this;
          }

          // Function form: targets _defaultNodeStyleFn, leaves object form
          // untouched. The helper handles wiping prev fn keys + re-applying.
          if (typeof overrides === 'function') {
            this._defaultNodeStyleFn = overrides;
            if (!this._defaultNodeStyleSnapshot) this._defaultNodeStyleSnapshot = new Map();
            if (!this._defaultNodeStyleFnKeys) this._defaultNodeStyleFnKeys = new Map();
            for (const n of this._nodes) {
              if (!n._dom) continue;
              this._applyDefaultNodeStyleFnTo(n);
              // Per-node overrides win over fn defaults.
              n._applyStyleOverrides();
            }
            return this;
          }

          if (typeof overrides !== 'object') return this;
          if (!this._defaultNodeStyle) this._defaultNodeStyle = {};
          if (!this._defaultNodeStyleSnapshot) this._defaultNodeStyleSnapshot = new Map();

          // Classify each incoming key: 'set' (add/update default) or 'unset'
          // (per-key removal via null value). 'unset' restores that key's
          // snapshot just like the clear-all path.
          const ops = [];
          for (const k of Object.keys(overrides)) {
            const v = overrides[k];
            if (v == null) {
              if (k in this._defaultNodeStyle) {
                delete this._defaultNodeStyle[k];
                ops.push({ key: k, op: 'unset' });
              }
            } else {
              const isNew = !(k in this._defaultNodeStyle);
              this._defaultNodeStyle[k] = v;
              ops.push({ key: k, op: 'set', value: v, isNew });
            }
          }

          for (const n of this._nodes) {
            if (!n._dom) continue;
            let snap = this._defaultNodeStyleSnapshot.get(n);
            if (!snap) {
              snap = {};
              this._defaultNodeStyleSnapshot.set(n, snap);
            }
            for (const op of ops) {
              if (op.op === 'set') {
                // Snapshot the pre-default inline value the first time we
                // touch this key for this node. Subsequent updates leave
                // the baseline intact so clear restores all the way back.
                if (op.isNew && !(op.key in snap)) {
                  snap[op.key] = n._dom.style[op.key] || '';
                }
                n._dom.style[op.key] = op.value;
              } else {
                // 'unset' - restore this key's snapshot if we have one,
                // otherwise clear (key was never defaulted on this node).
                if (op.key in snap) {
                  n._dom.style[op.key] = snap[op.key];
                  delete snap[op.key];
                } else {
                  n._dom.style[op.key] = '';
                }
              }
            }
            // Re-apply the fn so its values continue to win over the
            // (possibly just-rewritten) object defaults.
            if (this._defaultNodeStyleFn) this._applyDefaultNodeStyleFnTo(n);
            // Re-apply per-node overrides so they win over both layers.
            n._applyStyleOverrides();
          }
          return this;
        }

        // Wipe any keys the previous fn evaluation applied to `node`,
        // restoring each from the object default, the pre-default snapshot,
        // or '' in that priority order. Then evaluate _defaultNodeStyleFn(node)
        // and apply its result, snapshotting fresh baselines as needed and
        // tracking the new key set for the next wipe.
        // No-op if _defaultNodeStyleFn is unset or returns a non-object.
        _applyDefaultNodeStyleFnTo(node) {
          if (!this._defaultNodeStyleFn || !node || !node._dom) return;
          if (!this._defaultNodeStyleSnapshot) this._defaultNodeStyleSnapshot = new Map();
          if (!this._defaultNodeStyleFnKeys) this._defaultNodeStyleFnKeys = new Map();
          const snap = this._defaultNodeStyleSnapshot.get(node);
          const prevKeys = this._defaultNodeStyleFnKeys.get(node);
          if (prevKeys && prevKeys.size) {
            for (const k of prevKeys) {
              if (this._defaultNodeStyle && k in this._defaultNodeStyle) {
                node._dom.style[k] = this._defaultNodeStyle[k];
              } else if (snap && k in snap) {
                node._dom.style[k] = snap[k];
                delete snap[k];
              } else {
                node._dom.style[k] = '';
              }
            }
            prevKeys.clear();
          }
          let fnResult = null;
          try { fnResult = this._defaultNodeStyleFn(node); } catch (e) { fnResult = null; }
          if (!fnResult || typeof fnResult !== 'object') return;
          let nodeSnap = snap;
          if (!nodeSnap) {
            nodeSnap = {};
            this._defaultNodeStyleSnapshot.set(node, nodeSnap);
          }
          let fnKeys = this._defaultNodeStyleFnKeys.get(node);
          if (!fnKeys) {
            fnKeys = new Set();
            this._defaultNodeStyleFnKeys.set(node, fnKeys);
          }
          for (const k of Object.keys(fnResult)) {
            const v = fnResult[k];
            if (v == null) continue;
            if (!(k in nodeSnap)) nodeSnap[k] = node._dom.style[k] || '';
            node._dom.style[k] = v;
            fnKeys.add(k);
          }
        }

        // -----------------------------------------------------------------
        // Chrome styling: marquee, connect handles, edge preview
        // -----------------------------------------------------------------
        // All three follow the standard 4-mode *Style() contract that
        // defaultNodeStyle() / node.style() / edge.style() use:
        //   canvas.xStyle({ key: value })  - set/update one or more
        //   canvas.xStyle({ key: null })   - clear one key
        //   canvas.xStyle(null)            - clear all
        //   canvas.xStyle()                - return a copy of current overrides
        // Per-call overrides win over theme overrides win over CSS vars win
        // over hardcoded defaults. Pass null on the whole arg to drop back to
        // the CSS-var-driven defaults.
        //
        // (Selection and hover styling is below, under a different contract:
        // selectedNodeDecorator() / hoveredNodeDecorator(). Those accept
        // `object | (node) => object | null` and do NOT support per-key
        // clears - the rename is the marker that they're not *Style methods.)

        // Marquee (rubber-band selection) styling. Writes inline styles on
        // marquee_el so CSS-var defaults remain the fallback. Accepted keys:
        //   fill, stroke, lineWidth, lineStyle, borderRadius, opacity, glow, zIndex
        // `glow` becomes box-shadow. Numeric values get 'px' appended where
        // CSS demands a unit; pass strings to control units yourself.
        marqueeStyle(overrides) {
          if (arguments.length === 0) {
            return this._marqueeStyle ? Object.assign({}, this._marqueeStyle) : {};
          }
          if (overrides === null) {
            this._marqueeStyle = null;
            this._applyMarqueeStyle();
            return this;
          }
          if (typeof overrides !== 'object') return this;
          if (!this._marqueeStyle) this._marqueeStyle = {};
          for (const k of Object.keys(overrides)) {
            const v = overrides[k];
            if (v == null) delete this._marqueeStyle[k];
            else this._marqueeStyle[k] = v;
          }
          this._applyMarqueeStyle();
          return this;
        }

        // Update marquee_el's position/size from the active marquee rect
        // (in world coords, matching the world-transformed marquee_container).
        // Toggles display based on marqueeActive. Called from draw().
        _updateMarqueeDom() {
          const el = this.marquee_el;
          if (!el) return;
          if (!this.marqueeActive) {
            if (el.style.display !== 'none') el.style.display = 'none';
            return;
          }
          const x = Math.min(this.marqueeStartX, this.mouseX);
          const y = Math.min(this.marqueeStartY, this.mouseY);
          const w = Math.abs(this.mouseX - this.marqueeStartX);
          const h = Math.abs(this.mouseY - this.marqueeStartY);
          el.style.left   = x + 'px';
          el.style.top    = y + 'px';
          el.style.width  = w + 'px';
          el.style.height = h + 'px';
          el.style.display = 'block';
        }

        // Translate the _marqueeStyle override map onto inline CSS of
        // marquee_el. Each branch maps a key to its CSS property; '' clears.
        _applyMarqueeStyle() {
          const el = this.marquee_el;
          if (!el) return;
          const s = this._marqueeStyle || {};
          const px = (v) => (typeof v === 'number') ? (v + 'px') : v;
          el.style.background   = ('fill'         in s) ? s.fill                 : '';
          el.style.borderColor  = ('stroke'       in s) ? s.stroke               : '';
          el.style.borderWidth  = ('lineWidth'    in s) ? px(s.lineWidth)        : '';
          el.style.borderStyle  = ('lineStyle'    in s) ? s.lineStyle            : '';
          el.style.borderRadius = ('borderRadius' in s) ? px(s.borderRadius)     : '';
          el.style.opacity      = ('opacity'      in s) ? s.opacity              : '';
          el.style.boxShadow    = ('glow'         in s) ? s.glow                 : '';
          el.style.zIndex       = ('zIndex'       in s) ? s.zIndex               : '';
        }

        // Connect handle (the circle that appears on a node side when hovered
        // for a new edge). Stored centrally; applied via _applyConnectHandle
        // to every visible handle (the source handle and the four target
        // handles during an edge draft). Accepted keys:
        //   size, fill, stroke, lineWidth, borderRadius,
        //   activeFill, activeStroke, activeScale, glow, zIndex
        connectHandleStyle(overrides) {
          if (arguments.length === 0) {
            return this._connectHandleStyle ? Object.assign({}, this._connectHandleStyle) : {};
          }
          if (overrides === null) {
            this._connectHandleStyle = null;
            this._refreshAllConnectHandles();
            return this;
          }
          if (typeof overrides !== 'object') return this;
          if (!this._connectHandleStyle) this._connectHandleStyle = {};
          for (const k of Object.keys(overrides)) {
            const v = overrides[k];
            if (v == null) delete this._connectHandleStyle[k];
            else this._connectHandleStyle[k] = v;
          }
          this._refreshAllConnectHandles();
          return this;
        }

        // Reapply connect-handle styling to every currently-existing handle DOM.
        // Called when connectHandleStyle() changes, or by setTheme.
        _refreshAllConnectHandles() {
          // Source handles are always rendered in the inactive variant; the
          // active branch is only used for the hovered target side during a
          // draft. We don't track that hovered side here at refresh time -
          // showTargetHandles is the source of truth and will reapply on
          // the next pointer move - so all target handles also use the
          // inactive variant on refresh.
          for (const n of this._nodes) {
            if (n._connectHandle) {
              this._applyConnectHandle(n._connectHandle, { active: false });
            }
            if (n._targetHandles) {
              for (const s of ['left','right','top','bottom']) {
                const h = n._targetHandles[s];
                if (!h) continue;
                this._applyConnectHandle(h, { active: false });
              }
            }
          }
        }

        // Apply connect-handle styling to a single DOM element. Reads from
        // _connectHandleStyle with hardcoded fallbacks that preserve the
        // historical look (15px white circle, 2px accent border, scale 1.4
        // when active). `active` selects the activeFill/activeStroke/scale
        // branch (used for the hovered target side during an edge draft).
        _applyConnectHandle(el, opts) {
          const s = this._connectHandleStyle || {};
          const active = opts && opts.active;
          const px = (v) => (typeof v === 'number') ? (v + 'px') : v;
          const size = s.size != null ? px(s.size) : '15px';
          const radius = s.borderRadius != null ? px(s.borderRadius) : '50%';
          const lineWidth = s.lineWidth != null ? px(s.lineWidth) : '2px';
          const fill   = active && s.activeFill   != null ? s.activeFill
                       : s.fill   != null ? s.fill   : 'var(--tc-surface)';
          const stroke = active && s.activeStroke != null ? s.activeStroke
                       : s.stroke != null ? s.stroke : 'var(--tc-accent)';
          const activeScale = s.activeScale != null ? s.activeScale : 1.4;
          el.style.width = size;
          el.style.height = size;
          el.style.borderRadius = radius;
          el.style.borderWidth = lineWidth;
          el.style.borderStyle = 'solid';
          el.style.borderColor = stroke;
          el.style.backgroundColor = fill;
          el.style.boxShadow = s.glow != null ? s.glow : '';
          el.style.zIndex = s.zIndex != null ? s.zIndex : '200';
          // Preserve the translate-50% centering that _refreshHandlePosition
          // relies on (its left/top point at the side midpoint).
          el.style.transform = active
            ? `translate(-50%, -50%) scale(${activeScale})`
            : 'translate(-50%, -50%)';
        }

        // Phantom edge (edge draft preview) styling. Mirrors edge.style()
        // key shape so callers familiar with that API can reuse muscle
        // memory. Accepted keys:
        //   stroke, width, dash, dashOffset, glow, curvature, cap
        // `glow` is { color, blur } (matches edge.style({glow})).
        // Defaults: stroke -> theme.edgePreviewColor, width -> 2, dash -> [8,4].
        edgePreviewStyle(overrides) {
          if (arguments.length === 0) {
            return this._edgePreviewStyle ? Object.assign({}, this._edgePreviewStyle) : {};
          }
          if (overrides === null) {
            this._edgePreviewStyle = null;
            if (this.edgeDraft) this.requestDraw();
            return this;
          }
          if (typeof overrides !== 'object') return this;
          if (!this._edgePreviewStyle) this._edgePreviewStyle = {};
          for (const k of Object.keys(overrides)) {
            const v = overrides[k];
            if (v == null) delete this._edgePreviewStyle[k];
            else this._edgePreviewStyle[k] = v;
          }
          if (this.edgeDraft) this.requestDraw();
          return this;
        }

        // Selected-node styling. Renamed from selectedNodeStyle so the call
        // site advertises that this is NOT a standard *Style() contract
        // method - it accepts a function in addition to an object, and does
        // not support per-key clears. Accepts:
        //   object  - CSS properties (camelCase) applied to every selected node's _dom;
        //   (node) => object - per-node logic, evaluated each selection update;
        //   null    - restore the default outline (2px solid accent with 2px offset).
        // Get form: zero args returns the current value (function or object) as-is.
        // Previously-applied keys are tracked per node so the next call can
        // cleanly clear them, even if it swaps outline for boxShadow.
        selectedNodeDecorator(styleOrFn) {
          if (arguments.length === 0) {
            return this._selectedNodeDecorator;
          }
          if (styleOrFn === null) {
            this._selectedNodeDecorator = null;
          } else if (typeof styleOrFn === 'function' || typeof styleOrFn === 'object') {
            this._selectedNodeDecorator = styleOrFn;
          } else {
            return this;
          }
          this._updateSelectionStyles();
          return this;
        }

        // Parallel to selectedNodeDecorator, but for the hovered node. Same
        // argument shapes (object | (node)=>object | null) and same caveat
        // about NOT being a standard *Style() method. The default (when
        // null) is the historical inline ridge-border + drop-shadow
        // combination written from event_mousemove pre-refactor.
        hoveredNodeDecorator(styleOrFn) {
          if (arguments.length === 0) {
            return this._hoveredNodeDecorator;
          }
          if (styleOrFn === null) {
            this._hoveredNodeDecorator = null;
          } else if (typeof styleOrFn === 'function' || typeof styleOrFn === 'object') {
            this._hoveredNodeDecorator = styleOrFn;
          } else {
            return this;
          }
          this.requestDraw();
          return this;
        }

        // Single-image sugar over setBackground(). Keeps the original signature
        // for callers that only ever set an image.
        setBackgroundImage(pathOrUrl, opts) {
          this.backgroundImage = (typeof pathOrUrl === "string" && pathOrUrl) ? pathOrUrl : null;
          if (opts) {
            if (opts.size !== undefined) this.backgroundSize = opts.size || 'auto';
            if (Number.isFinite(opts.opacity)) {
              this.backgroundOpacity = Math.min(1, Math.max(0, opts.opacity));
            }
          }
          this._applyBackground();
          return this;
        }

        // Multi-layer background setter. Layers stack: color (bottom) ->
        // gradient -> image (top). Each key is independently set/cleared;
        // pass null as the whole arg to clear everything.
        //   canvas.setBackground({ color: '#0a0d1c' })            - color only
        //   canvas.setBackground({ gradient: 'linear-gradient(...)' })
        //   canvas.setBackground({ image: 'path.png', size: 'cover' })
        //   canvas.setBackground({ image: '...', gradient: '...', color: '...', opacity: 0.8 })
        //   canvas.setBackground({ image: null })                 - clear image only
        //   canvas.setBackground(null)                            - clear all
        // `size` applies only to the image layer; gradient and color always
        // fill the (world-transformed) background_layer.
        setBackground(opts) {
          if (opts === null) {
            this.backgroundImage    = null;
            this.backgroundGradient = null;
            this.backgroundColor    = null;
            this.backgroundOpacity  = 1;
            this._applyBackground();
            return this;
          }
          if (typeof opts !== 'object') return this;
          // 'in' so the caller can explicitly null-out one layer without
          // touching the others.
          if ('image'    in opts) this.backgroundImage    = (typeof opts.image    === 'string' && opts.image)    ? opts.image    : null;
          if ('gradient' in opts) this.backgroundGradient = (typeof opts.gradient === 'string' && opts.gradient) ? opts.gradient : null;
          if ('color'    in opts) this.backgroundColor    = (typeof opts.color    === 'string' && opts.color)    ? opts.color    : null;
          if ('size'     in opts) this.backgroundSize     = opts.size || 'auto';
          if ('opacity'  in opts && Number.isFinite(opts.opacity)) {
            this.backgroundOpacity = Math.min(1, Math.max(0, opts.opacity));
          }
          this._applyBackground();
          return this;
        }

        _applyBackground() {
          const worldLayer = this.background_layer;
          const fixedLayer = this.background_fixed_layer;
          if (!worldLayer || !fixedLayer) return;

          // -------- Viewport-fixed layer: color + gradient. --------
          // These act as canvas chrome and must fill the visible area
          // regardless of pan/zoom, so they live on the un-transformed
          // container-sized layer.
          fixedLayer.style.backgroundColor = this.backgroundColor || "";
          if (this.backgroundGradient) {
            fixedLayer.style.backgroundImage  = this.backgroundGradient;
            fixedLayer.style.backgroundSize   = "auto";
            fixedLayer.style.backgroundRepeat = "no-repeat";
            fixedLayer.style.backgroundPosition = "0 0";
          } else {
            fixedLayer.style.backgroundImage = "";
          }
          const fixedHasContent = !!this.backgroundColor || !!this.backgroundGradient;
          fixedLayer.style.opacity = fixedHasContent ? String(this.backgroundOpacity) : "1";

          // -------- World-transformed layer: image only. --------
          // Image has a natural position in world space; it pans/zooms with
          // content. background_layer is the 100000x100000 layer centered on
          // world origin via the container's translate trick.
          const E = this._bgLayerExtent + "px";
          if (this.backgroundImage) {
            const resolved = this.resolveFile ? this.resolveFile(this.backgroundImage) : this.backgroundImage;
            // CSS url() escaping - escape backslashes and double quotes.
            const safe = String(resolved).replace(/\\/g, "\\\\").replace(/\x22/g, '\\"');
            worldLayer.style.backgroundImage = `url("${safe}")`;
            const size = this.backgroundSize;
            if (size === "tile") {
              worldLayer.style.backgroundRepeat = "repeat";
              worldLayer.style.backgroundSize = "auto";
            } else {
              worldLayer.style.backgroundRepeat = "no-repeat";
              worldLayer.style.backgroundSize = (size === "auto" || !size) ? "auto" : size;
            }
            worldLayer.style.backgroundPosition = E + " " + E;
            worldLayer.style.opacity = String(this.backgroundOpacity);
          } else {
            worldLayer.style.backgroundImage = "";
            worldLayer.style.backgroundSize = "auto";
            worldLayer.style.backgroundRepeat = "no-repeat";
            worldLayer.style.backgroundPosition = E + " " + E;
            worldLayer.style.opacity = "1";
          }
        }

        // Host-registered viewport overlay. Returns a fresh <div> sized to
        // fill the canvas viewport, with `pointer-events: none` by default
        // so it doesn't swallow canvas interactions. Insert before the
        // toolbar so toolbar UI stays clickable; opt into the highest
        // stacking layer with `zIndex: 'top'` if you want scanlines / a
        // vignette / a watermark to paint over the toolbar too.
        //
        //   const layer = canvas.addOverlay({ pointerEvents: 'none' });
        //   layer.style.background =
        //     'repeating-linear-gradient(0deg, rgba(0,255,0,0.04) 0 1px, transparent 1px 3px)';
        //   // ... later
        //   canvas.removeOverlay(layer);
        addOverlay(opts = {}) {
          const el = document.createElement('div');
          el.style.position = 'absolute';
          el.style.top = '0';
          el.style.left = '0';
          el.style.width = '100%';
          el.style.height = '100%';
          el.style.pointerEvents = opts.pointerEvents != null ? opts.pointerEvents : 'none';
          if (opts.zIndex === 'top') el.style.zIndex = '2000';
          else if (opts.zIndex != null) el.style.zIndex = String(opts.zIndex);
          if (!this._overlays) this._overlays = [];
          this._overlays.push(el);
          // Insert before toolbar so toolbar stays interactive by default.
          if (this.toolbar_container && this.toolbar_container.parentNode === this.container) {
            this.container.insertBefore(el, this.toolbar_container);
          } else {
            this.container.appendChild(el);
          }
          return el;
        }

        // Remove a previously-added overlay. Silently no-ops if the element
        // wasn't created by addOverlay() on this canvas.
        removeOverlay(el) {
          if (!el || !this._overlays) return this;
          const i = this._overlays.indexOf(el);
          if (i === -1) return this;
          this._overlays.splice(i, 1);
          if (el.parentNode) el.parentNode.removeChild(el);
          return this;
        }

        // Host-registered world-transformed layer. Returns a fresh <div>
        // whose contents are positioned in world coordinates (children's
        // `style.left/top` should be in world units, no manual pan/zoom).
        // Pans and zooms with the canvas automatically. Inserted above
        // drawing_container so it paints on top of content nodes; use
        // `zIndex: 'top'` to raise above hitbox/toolbar as well.
        //
        //   const layer = canvas.addContentLayer();
        //   const tag = document.createElement('div');
        //   Object.assign(tag.style, { position: 'absolute', left: '300px', top: '200px',
        //                              background: '#ff0', padding: '4px' });
        //   tag.textContent = 'annotation';
        //   layer.appendChild(tag);
        addContentLayer(opts = {}) {
          const el = document.createElement('div');
          el.style.position = 'absolute';
          el.style.top = '0';
          el.style.left = '0';
          el.style.transformOrigin = '0 0';
          el.style.pointerEvents = opts.pointerEvents != null ? opts.pointerEvents : 'none';
          if (opts.zIndex === 'top') el.style.zIndex = '2000';
          else if (opts.zIndex != null) el.style.zIndex = String(opts.zIndex);
          if (!this._contentLayers) this._contentLayers = [];
          this._contentLayers.push(el);
          // Above drawing_container, below hitbox_container so canvas events
          // still flow through (the layer itself has pointer-events:none).
          if (this.hitbox_container && this.hitbox_container.parentNode === this.container) {
            this.container.insertBefore(el, this.hitbox_container);
          } else {
            this.container.appendChild(el);
          }
          // Apply the current world transform immediately so the layer is
          // positioned correctly before the next pan/zoom event.
          el.style.transform = `translate(${this.panX}px, ${this.panY}px) scale(${this.scale})`;
          return el;
        }

        removeContentLayer(el) {
          if (!el || !this._contentLayers) return this;
          const i = this._contentLayers.indexOf(el);
          if (i === -1) return this;
          this._contentLayers.splice(i, 1);
          if (el.parentNode) el.parentNode.removeChild(el);
          return this;
        }

        // Apply a declarative theme. Composed of three per-element function
        // hooks plus a handful of canvas-wide static settings. Applied to
        // every existing node/edge immediately, and to every new node/edge
        // as it's created (via nodeCreate / edgeCreate listeners).
        //
        //   canvas.setTheme({
        //     // Per-element styling functions. Return an object passed to
        //     // node.style() / edge.style(); return null/undefined to skip.
        //     node:  (node)  => ({ borderColor: '#0ff' }),
        //     group: (group) => ({ borderStyle: 'dashed', label: { fontFamily: 'Georgia' }}),
        //     edge:  (edge)  => ({ width: 2, glow: { color: '#0ff', blur: 12 }}),
        //
        //     // Canvas-wide settings (each optional).
        //     defaultNodeStyle: { borderRadius: '0', fontFamily: 'JetBrains Mono' },
        //     background:       { color: '#0a0d1c', gradient: 'radial-gradient(...)' },
        //     showGrid:         'dots',
        //     cssVars:          { '--tc-grid-dot': '#163e4a', '--tc-accent': '#00d9ff' },
        //   });
        //
        //   canvas.clearTheme();   // wipe overrides, restore snapshot
        //   canvas.theme;          // returns the current theme object (or null)
        //
        // Semantics
        // - `group` is called for group nodes; `node` is called for every
        //   non-group node. If `group` is absent, `node` is called for
        //   groups too.
        // - clearTheme is coarse: it calls style(null) on every node/edge,
        //   so any per-node fine-tuning you did via node.style() after
        //   setTheme is lost. Use addClass() if you need theme-independent
        //   per-node visual state.
        // - Canvas-wide settings (defaultNodeStyle, background, showGrid)
        //   are snapshotted before the theme writes to them and restored
        //   on clearTheme.
        setTheme(theme) {
          if (theme == null) return this.clearTheme();
          if (typeof theme !== 'object') return this;

          // Tear down any previous theme's per-element work and cssVars
          // first so themes can be swapped without leaving residue. We
          // keep the original snapshot from the first theme so clearTheme
          // always returns to the pre-theme state, no matter how many
          // setThemes have stacked.
          if (this._theme) this._teardownThemeStyles();

          // Take a one-time snapshot of pre-theme canvas-wide settings the
          // theme might overwrite. Once captured, it's preserved across
          // subsequent setTheme calls until clearTheme.
          if (!this._themeSnapshot) {
            const snap = {};
            if (theme.defaultNodeStyle != null) {
              snap.defaultNodeStyle = this._defaultNodeStyle ? Object.assign({}, this._defaultNodeStyle) : null;
            }
            if (theme.background != null) {
              snap.background = {
                image:    this.backgroundImage,
                gradient: this.backgroundGradient,
                color:    this.backgroundColor,
                size:     this.backgroundSize,
                opacity:  this.backgroundOpacity,
              };
            }
            if (typeof theme.showGrid === 'string') {
              snap.showGrid = this.showGrid;
            }
            if (theme.marquee != null) {
              snap.marquee = this._marqueeStyle ? Object.assign({}, this._marqueeStyle) : null;
            }
            if (theme.connectHandle != null) {
              snap.connectHandle = this._connectHandleStyle ? Object.assign({}, this._connectHandleStyle) : null;
            }
            if (theme.edgePreviewStyle != null) {
              snap.edgePreviewStyle = this._edgePreviewStyle ? Object.assign({}, this._edgePreviewStyle) : null;
            }
            if ('selectedNode' in theme) {
              // _selectedNodeDecorator can be an object or a function; capture
              // the reference as-is so the function form survives restore.
              snap.selectedNode = this._selectedNodeDecorator;
            }
            if ('hoveredNode' in theme) {
              snap.hoveredNode = this._hoveredNodeDecorator;
            }
            this._themeSnapshot = snap;
          }

          this._theme = theme;

          // CSS variables - applied to the root element so they cascade
          // through .tzara-canvas-root rules. Track keys for teardown.
          if (theme.cssVars && this.outer_container) {
            this._themeCssVarKeys = [];
            for (const k of Object.keys(theme.cssVars)) {
              this.outer_container.style.setProperty(k, theme.cssVars[k]);
              this._themeCssVarKeys.push(k);
            }
          }

          // Canvas-wide static settings, applied via the existing setters.
          if (theme.defaultNodeStyle != null) this.defaultNodeStyle(theme.defaultNodeStyle);
          if (theme.background != null)       this.setBackground(theme.background);
          if (typeof theme.showGrid === 'string') this.setGrid(theme.showGrid);
          // Chrome styling: marquee, connect handle, edge preview, selection.
          // Same setter convention as defaultNodeStyle - pass null on a
          // key to clear, or null on the whole arg to restore CSS-var defaults.
          if (theme.marquee != null)          this.marqueeStyle(theme.marquee);
          if (theme.connectHandle != null)    this.connectHandleStyle(theme.connectHandle);
          if (theme.edgePreviewStyle != null) this.edgePreviewStyle(theme.edgePreviewStyle);
          if ('selectedNode' in theme)        this.selectedNodeDecorator(theme.selectedNode);
          if ('hoveredNode' in theme)         this.hoveredNodeDecorator(theme.hoveredNode);

          // Re-read the theme cache (cssVars we just set might include
          // --tc-color-*) and re-derive every preset-colored node/edge so
          // theme.palette + the new cssVars take effect immediately. Also
          // triggers a redraw.
          this.refreshTheme();

          // Apply per-element functions to everything that already exists.
          // Runs AFTER refreshTheme so any non-color style overrides layer
          // on top of the freshly-painted preset.
          for (const n of this._nodes) this._applyThemeToNode(n);
          for (const e of this._edges) this._applyThemeToEdge(e);

          // Hook future creates. on() returns a disposer - collect them.
          this._themeUnhook = [
            this.on('nodeCreate', (node) => this._applyThemeToNode(node)),
            this.on('edgeCreate', (edge) => this._applyThemeToEdge(edge)),
          ];

          return this;
        }

        clearTheme() {
          if (!this._theme) return this;
          this._teardownThemeStyles();

          // Unhook nodeCreate / edgeCreate.
          if (this._themeUnhook) {
            for (const dispose of this._themeUnhook) dispose();
            this._themeUnhook = null;
          }

          // Restore canvas-wide settings from snapshot.
          if (this._themeSnapshot) {
            const s = this._themeSnapshot;
            if ('defaultNodeStyle' in s) {
              this.defaultNodeStyle(null);
              if (s.defaultNodeStyle) this.defaultNodeStyle(s.defaultNodeStyle);
            }
            if ('background' in s) this.setBackground(s.background);
            if ('showGrid' in s)   this.setGrid(s.showGrid);
            if ('marquee' in s) {
              this.marqueeStyle(null);
              if (s.marquee) this.marqueeStyle(s.marquee);
            }
            if ('connectHandle' in s) {
              this.connectHandleStyle(null);
              if (s.connectHandle) this.connectHandleStyle(s.connectHandle);
            }
            if ('edgePreviewStyle' in s) {
              this.edgePreviewStyle(null);
              if (s.edgePreviewStyle) this.edgePreviewStyle(s.edgePreviewStyle);
            }
            if ('selectedNode' in s) {
              this.selectedNodeDecorator(s.selectedNode);
            }
            if ('hoveredNode' in s) {
              this.hoveredNodeDecorator(s.hoveredNode);
            }
            this._themeSnapshot = null;
          }

          this._theme = null;

          // _teardownThemeStyles removed any --tc-color-* / --tc-*-default
          // cssVars, so the palette resolution chain now falls through to
          // canvasColor's hardcoded defaults. Refresh existing elements to
          // pick up that change.
          this.refreshTheme();
          return this;
        }

        // Returns the current theme object passed to setTheme(), or null.
        get theme() {
          return this._theme || null;
        }

        _applyThemeToNode(node) {
          const t = this._theme;
          if (!t || !node) return;
          // group falls through to node if not defined.
          const fn = (node.type === 'group' && typeof t.group === 'function') ? t.group : t.node;
          if (typeof fn !== 'function') return;
          let style;
          try { style = fn(node); }
          catch (err) { console.error("TzaraCanvas theme.node/group threw:", err); return; }
          if (style && typeof style === 'object') node.style(style);
        }

        _applyThemeToEdge(edge) {
          const t = this._theme;
          if (!t || !edge || typeof t.edge !== 'function') return;
          let style;
          try { style = t.edge(edge); }
          catch (err) { console.error("TzaraCanvas theme.edge threw:", err); return; }
          if (style && typeof style === 'object') edge.style(style);
        }

        _teardownThemeStyles() {
          // Coarse: wipe all style() overrides on every node and edge.
          // Per-node user tweaks made after setTheme are lost - see the
          // setTheme doc-comment for the rationale and the addClass()
          // workaround.
          for (const n of this._nodes) { if (n._dom) n.style(null); }
          for (const e of this._edges) e.style(null);

          // Remove cssVars we set.
          if (this._themeCssVarKeys && this.outer_container) {
            for (const k of this._themeCssVarKeys) this.outer_container.style.removeProperty(k);
          }
          this._themeCssVarKeys = null;
        }

        _drawGrid(viewX, viewY, viewW, viewH) {
          const step = this.gridSize;
          if (!step || step <= 0) return;
          // Skip when the on-screen step would be below ~3px; lines that
          // dense are visual noise and tank paint cost when zoomed far out.
          if (step * this.scale < 3) return;

          const ctx = this.ctx;
          const major = this.gridMajorEvery|0;
          const x0 = Math.floor(viewX / step) * step;
          const y0 = Math.floor(viewY / step) * step;
          const x1 = viewX + viewW;
          const y1 = viewY + viewH;

          if (this.showGrid === 'lines') {
            const lw = 1 / this.scale;
            const lwMajor = 1.5 / this.scale;

            // Minor pass - single batched path for everything that isn't a
            // major line. Drawing the major line on top with a different
            // color hides the minor line underneath, so we don't bother
            // skipping minor cells.
            ctx.save();
            ctx.lineWidth = lw;
            ctx.strokeStyle = this._palette.gridStroke;
            ctx.beginPath();
            for (let x = x0; x <= x1; x += step) {
              ctx.moveTo(x, viewY);
              ctx.lineTo(x, y1);
            }
            for (let y = y0; y <= y1; y += step) {
              ctx.moveTo(viewX, y);
              ctx.lineTo(x1, y);
            }
            ctx.stroke();

            if (major > 0) {
              ctx.lineWidth = lwMajor;
              ctx.strokeStyle = this._palette.gridStrokeMajor;
              ctx.beginPath();
              // Major lines are at world coords that are multiples of
              // (step * major). Find first such coord >= viewX/viewY.
              const majorStep = step * major;
              const mx0 = Math.floor(viewX / majorStep) * majorStep;
              const my0 = Math.floor(viewY / majorStep) * majorStep;
              for (let x = mx0; x <= x1; x += majorStep) {
                ctx.moveTo(x, viewY);
                ctx.lineTo(x, y1);
              }
              for (let y = my0; y <= y1; y += majorStep) {
                ctx.moveTo(viewX, y);
                ctx.lineTo(x1, y);
              }
              ctx.stroke();
            }
            ctx.restore();
            return;
          }

          if (this.showGrid === 'dots') {
            // Cached-pattern path. Tile is integer-sized (canvas dims must be
            // integers) but its repeat period in *world* coordinates is set
            // via pattern.setTransform to exactly `step` (per minor cell), so
            // dot positions are smooth functions of zoom - no integer
            // rounding of stepPx ever enters the on-screen position.
            const stepPx = step * this.scale;
            // Density fade: as stepPx shrinks toward the 3px cutoff, dots
            // would otherwise dither out the background. Fade alpha and
            // shrink dot size on a single `t` ramp so the grid stays a hint
            // rather than a wash. Above FADE_HI nothing changes.
            const FADE_LO = 4, FADE_HI = 20;
            const MIN_ALPHA = 0.25, MIN_DOT = 1.2;
            const t = Math.max(0, Math.min(1, (stepPx - FADE_LO) / (FADE_HI - FADE_LO)));
            const alpha = MIN_ALPHA + (1 - MIN_ALPHA) * t;
            const minorPx = MIN_DOT + (2 - MIN_DOT) * t;
            const majorPx = 2 * minorPx;
            const pattern = this._ensureDotPattern(stepPx, step, major, this.scale, minorPx, majorPx);
            ctx.save();
            ctx.globalAlpha = alpha;
            ctx.fillStyle = pattern;
            ctx.fillRect(viewX, viewY, viewW, viewH);
            ctx.restore();
            return;
          }
        }

        _ensureDotPattern(stepPx, step, major, scale, minorPx, majorPx) {
          const minorColor = this._palette.gridDot;
          const majorColor = this._palette.gridDotMajor;
          const hasMajor = major > 0;

          // Tile covers one major cycle (or 1 minor cell if no majors).
          // At extreme zoom the tile would get too big - fall back to a
          // minor-only tile and drop majors. (Major dots are visually
          // redundant at very high zoom anyway.)
          const TILE_CAP_PX = 512;
          let cycle = hasMajor ? major : 1;
          if (cycle > 1 && stepPx * cycle > TILE_CAP_PX) cycle = 1;

          // N = integer tile pixels per minor cell. Picked close to stepPx so
          // the dot has decent resolution. The tile's *world-space* period is
          // pinned by pattern.setTransform below, so changes to N as we zoom
          // affect only dot rendering crispness, not position.
          const N = Math.max(2, Math.round(stepPx));
          const tilePx = N * cycle;
          const wpt = step / N;                 // world units per tile pixel
          // Fractional dot sizes - fillRect antialiases, which keeps the
          // on-screen dot size ≈minorPx/majorPx regardless of how N rounds.
          // minorPx/majorPx are the target *screen-pixel* dot sizes, set by
          // the caller (driven by zoom-density fade).
          const dotMinor = (minorPx * N) / stepPx;
          const dotMajor = (majorPx * N) / stepPx;

          const sig = N + '|' + cycle + '|' + stepPx.toFixed(4) + '|' +
                      scale.toFixed(6) + '|' + minorPx.toFixed(2) + '|' +
                      majorPx.toFixed(2) + '|' + minorColor + '|' + majorColor;
          if (this._gridPattern && this._gridPatternSig === sig) {
            return this._gridPattern;
          }

          const off = document.createElement("canvas");
          off.width = tilePx;
          off.height = tilePx;
          const octx = off.getContext("2d");
          octx.fillStyle = minorColor;
          for (let j = 0; j < cycle; j++) {
            for (let i = 0; i < cycle; i++) {
              if (hasMajor && cycle === major && i === 0 && j === 0) continue;
              octx.fillRect(i * N, j * N, dotMinor, dotMinor);
            }
          }
          if (hasMajor && cycle === major) {
            octx.fillStyle = majorColor;
            octx.fillRect(0, 0, dotMajor, dotMajor);
          }

          const pattern = this.ctx.createPattern(off, 'repeat');
          // setTransform maps tile-pixel space into the canvas's current user
          // space (world coords). Scale wpt makes one minor cell = `step`
          // world units, so the pattern repeats exactly with the grid. The
          // -1/scale translate matches the original `x - 1/scale` centering
          // that the loop-based code used (1 screen px above-left of the
          // intersection), so a 2px dot visually centers on (0,0).
          pattern.setTransform(new DOMMatrix([wpt, 0, 0, wpt, -1 / scale, -1 / scale]));
          this._gridPattern = pattern;
          this._gridPatternSig = sig;
          return pattern;
        }

        event_wheel(e) {

            e.preventDefault();

            // If the wheel is over (or operating on) a node whose content
            // overflows, scroll that node instead of zooming the canvas.
            const rect = this.container.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;
            const wheelWorld = this.toWorld(mouseX, mouseY);
            const hovered = this.hitNode(wheelWorld.x, wheelWorld.y);
            let scrollTarget = null;
            if (hovered && hovered._scrollEl && this._scrollbarWidth(hovered) > 0) {
                scrollTarget = hovered;
            } else if (this.selectedNodes.length === 1) {
                const sel = this.selectedNodes[0];
                if (sel._scrollEl && this._scrollbarWidth(sel) > 0) scrollTarget = sel;
            }
            if (scrollTarget) {
                scrollTarget._scrollEl.scrollTop += e.deltaY;
                return;
            }

            // If we got here, we're not scrolling a node, just regular zoom
            if (!this._can('zoom')) {
              return;
            }

            // Delta-based zoom: step proportional to wheel delta magnitude.
            // Math.exp keeps it multiplicative so zoom in then out by the same delta returns
            // to the same scale. ~0.001 gives a 100px wheel notch ≈ 1.105x (close to old feel),
            // while trackpad pixel-deltas of 4-10px become gentle 1.004-1.011x steps.
            const ZOOM_SENSITIVITY = 0.001;
            const zoomFactor = Math.exp(-e.deltaY * ZOOM_SENSITIVITY);
            const newScale = Math.min(Math.max(this.scale * zoomFactor, 0.2), 5);

            this.panX = mouseX - (mouseX - this.panX) * (newScale / this.scale);
            this.panY = mouseY - (mouseY - this.panY) * (newScale / this.scale);
            this.scale = newScale; 

            // updates drawing container
            this.updateTransform();

            if (this.outer_container) {
              this._sizeCanvas();
              this.requestDraw();
            }
            this._emitViewportChange();
        }

        ////////////////////////
        ///// MOUSE MOVE //////
        //////////////////////
        event_mousemove(e) {
            e.preventDefault();

            const rect = this.container.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;

            const { x, y } = this.toWorld(mouseX, mouseY);
            
          
            this.mouseX = x;
            this.mouseY = y;

            // Hover tracking: emit nodeHover('enter'|'leave') on transitions.
            // The hover visual is applied draw-time via _updateHoverStyles,
            // so any transition needs to request a redraw.
            const hoverNode = this.hitNode(x, y) || null;
            const prevHover = this._hoveredNode || null;
            if (hoverNode !== prevHover) {
              if (prevHover) this._emit('nodeHover', prevHover, 'leave');
              this._hoveredNode = hoverNode;
              if (hoverNode) this._emit('nodeHover', hoverNode, 'enter');
              this.requestDraw();
            }

            this._updateLinkInteractivity();

            // Promote a pending click to a real drag once the cursor has moved
            // past the 4px hysteresis. This lets a release-without-movement be
            // treated as a link follow in mouseup.
            if (this._dragPending && this._dragPending.canDrag) {
              const p = this._dragPending;
              if (Math.hypot(x - p.downX, y - p.downY) > 4) {
                this._promoteToDrag();
              }
            }

            // const { x, y } = this.toWorld(this.mouseX, this.mouseY);
            if (this.scrollingNode) {
                const dom = this.scrollingNode._scrollEl || this.scrollingNode._dom;
                // Scale mouse delta by ratio of total content to visible area so
                // the thumb tracks the cursor (emulates native scrollbar drag).
                const ratio = dom.scrollHeight / Math.max(1, dom.clientHeight);
                dom.scrollTop = this.scrollStartTop + (y - this.scrollStartY) * ratio;
                return;
            }
            if (this.resizing) {
                const dx = x - this.resizeStartX, dy = y - this.resizeStartY;
                const start = this.resizeStartDims;
                const sides = this.resizeSides;
                const minW = 40, minH = 30;
                let nx = start.x, ny = start.y, nw = start.width, nh = start.height;
                if (sides.right)  nw = Math.max(minW, start.width  + dx);
                if (sides.bottom) nh = Math.max(minH, start.height + dy);
                if (sides.left) {
                  const newW = Math.max(minW, start.width - dx);
                  nx = start.x + (start.width - newW);
                  nw = newW;
                }
                if (sides.top) {
                  const newH = Math.max(minH, start.height - dy);
                  ny = start.y + (start.height - newH);
                  nh = newH;
                }
                // Aspect lock for image file nodes and group nodes with ratio backgrounds
                // (Shift overrides for free resize).
                const aspect = this._aspectFor(this.resizing, e);
                const horizSide = sides.left ? "left" : sides.right ? "right" : null;
                const vertSide  = sides.top  ? "top"  : sides.bottom ? "bottom" : null;
                let driverAxis = null;
                if (aspect) {
                  if (horizSide && vertSide) {
                    // Pick the axis whose candidate produces the larger aspect-correct box, so the
                    // resulting box encloses the cursor and the cursor sits on the dragged edge.
                    driverAxis = (nw >= nh * aspect) ? "w" : "h";
                  } else if (horizSide) {
                    driverAxis = "w";
                  } else if (vertSide) {
                    driverAxis = "h";
                  }
                }
                // Snap the moving edge so the opposite edge stays put. With aspect lock,
                // snap only the driver axis; the derived axis is recomputed below.
                if (this.snapToGrid && !e.altKey) {
                  if (sides.right && (driverAxis === null || driverAxis === "w")) {
                    const snapped = this._snapValue(nx + nw);
                    nw = Math.max(minW, snapped - nx);
                  }
                  if (sides.bottom && (driverAxis === null || driverAxis === "h")) {
                    const snapped = this._snapValue(ny + nh);
                    nh = Math.max(minH, snapped - ny);
                  }
                  if (sides.left && (driverAxis === null || driverAxis === "w")) {
                    const right = start.x + start.width;
                    const snappedX = this._snapValue(nx);
                    const candW = right - snappedX;
                    if (candW >= minW) { nx = snappedX; nw = candW; }
                  }
                  if (sides.top && (driverAxis === null || driverAxis === "h")) {
                    const bottom = start.y + start.height;
                    const snappedY = this._snapValue(ny);
                    const candH = bottom - snappedY;
                    if (candH >= minH) { ny = snappedY; nh = candH; }
                  }
                }
                // Derive the non-driver axis from the driver and re-anchor.
                // Edge drags center on the perpendicular axis; corner drags keep the opposite corner pinned.
                if (driverAxis === "w") {
                  nh = Math.max(minH, nw / aspect);
                  nw = nh * aspect;
                  if (vertSide) {
                    if (vertSide === "top") ny = (start.y + start.height) - nh;
                    else ny = start.y;
                  } else {
                    ny = start.y + (start.height - nh) / 2;
                  }
                  if (horizSide === "left") nx = (start.x + start.width) - nw;
                  else nx = start.x;
                } else if (driverAxis === "h") {
                  nw = Math.max(minW, nh * aspect);
                  nh = nw / aspect;
                  if (horizSide) {
                    if (horizSide === "left") nx = (start.x + start.width) - nw;
                    else nx = start.x;
                  } else {
                    nx = start.x + (start.width - nw) / 2;
                  }
                  if (vertSide === "top") ny = (start.y + start.height) - nh;
                  else ny = start.y;
                }
                const n = this.resizing;
                n.x = nx; n.y = ny; n.width = nw; n.height = nh;
                n._dom.style.left = nx + "px";
                n._dom.style.top = ny + "px";
                n._dom.style.width = nw + "px";
                n._dom.style.height = nh + "px";
                n._refreshAttached();
                this.requestDraw();
                return;
            } else if (this.dragging && this.selectedNodes.length) {
                // Compute primary node's proposed top-left from origin + mouse
                // delta, then snap (grid, and optionally node-to-node). Apply
                // the resulting delta to all dragged nodes so groups stay rigid.
                const primary = this._dragPrimary || this.selectedNodes[0];
                const mouseDX = x - this.dragOriginWorldX;
                const mouseDY = y - this.dragOriginWorldY;
                let sx = primary._dragOriginX + mouseDX;
                let sy = primary._dragOriginY + mouseDY;

                if (!e.altKey) {
                  // Axis precedence: node-to-node wins over grid per-axis.
                  let guides = [];
                  if (this.snapToNodes) {
                    const ignore = new Set();
                    for (const n of this.selectedNodes) ignore.add(n);
                    if (this._dragExtraNodes) {
                      for (const n of this._dragExtraNodes) ignore.add(n);
                    }
                    const r = this._snapToNodes(sx, sy, primary.width, primary.height, ignore);
                    const xHit = r.guides.some(g => g.axis === "v");
                    const yHit = r.guides.some(g => g.axis === "h");
                    if (xHit) sx = r.x;
                    if (yHit) sy = r.y;
                    guides = r.guides;
                    if (this.snapToGrid) {
                      if (!xHit) sx = this._snapValue(sx);
                      if (!yHit) sy = this._snapValue(sy);
                    }
                  } else if (this.snapToGrid) {
                    sx = this._snapValue(sx);
                    sy = this._snapValue(sy);
                  }
                  this._activeGuides = guides;
                } else {
                  this._activeGuides = [];
                }

                const dx = sx - primary.x, dy = sy - primary.y;
                if (dx !== 0 || dy !== 0) {
                  const moved = new Set();
                  for (const n of this.selectedNodes) {
                    if (moved.has(n)) continue;
                    n._moveBy(dx, dy);
                    moved.add(n);
                  }
                  if (this._dragExtraNodes) {
                    for (const n of this._dragExtraNodes) {
                      if (moved.has(n)) continue;
                      n._moveBy(dx, dy);
                      moved.add(n);
                    }
                  }
                }

                this.cursor = "grabbing";
                this.requestDraw();
                return;
            } else if (this.isPanning) {
                // Per-axis permission gates: a host can lock horizontal pan
                // while leaving vertical free, or vice versa. Independent
                // gating preserves the camera's other axis even when the
                // user drags diagonally against a locked axis.
                const dx = this._can('panX') ? e.movementX : 0;
                const dy = this._can('panY') ? e.movementY : 0;
                if (dx || dy) this._panDidMove = true;
                this.panX += dx;
                this.panY += dy;
                this.updateTransform();
                this.requestDraw();
                this._emitViewportChange();
                this.cursor = "grab";
                return;
            } else if (this.marqueeActive) {
              this.requestDraw();
            } else if (this._edgeReattachPending) {
              const p = this._edgeReattachPending;
              if (Math.hypot(x - p.downX, y - p.downY) > 4) {
                this._startEdgeReattach(p.edge, p.detachedEnd, x, y);
                this._edgeReattachPending = null;
              }
              return;
            } else if (this.edgeDraft) {
              this.edgeDraft.mouseX = x;
              this.edgeDraft.mouseY = y;
              const hit = this.hitNode(x, y);
              const prevTo = this.edgeDraft.toNode;
              // Self-loops (hit === anchor) are permitted, but disallow landing
              // on the same side as the anchor - that collapses the bezier.
              let snap = null;
              if (hit) {
                snap = this._targetSideSnap(hit, x, y);
                if (hit === this.edgeDraft.fromNode && snap === this.edgeDraft.fromSide) {
                  snap = null;
                }
              }
              if (hit && snap) {
                if (prevTo && prevTo !== hit) prevTo.hideTargetHandles();
                this.edgeDraft.toNode = hit;
                this.edgeDraft.toSide = snap;
                hit.showTargetHandles(snap);
              } else {
                if (prevTo) prevTo.hideTargetHandles();
                this.edgeDraft.toNode = null;
                this.edgeDraft.toSide = null;
              }
              this.requestDraw();
              return;
            };
        

         
          //var x = e.clientX, y = e.clientY;
          // var x = e.layerX, y=e.layerY;
          var n = this.hitNode(x,y);
          var edge = this.hitEdge(x,y);



          // e.preventDefault();
          if (n) {
            // Clear any leftover edge-hover state from a prior frame: a fast
            // mousemove can jump from "over edge" to "over node" in one event,
            // skipping the else-if branch that would otherwise reset it.
            let _edgeWasHovered = false;
            for (const e of this._edges) {
              if (e.hovered) { e.hovered = false; _edgeWasHovered = true; }
            }
            if (_edgeWasHovered) this.requestDraw();
            // Hover visuals are applied draw-time by _updateInteractionStyles
            // based on this._hoveredNode (set in the transition block
            // above). No inline writes from here - that previously wiped
            // selection box-shadow on every other node.

              // Floating label hover - sits outside the bbox, so resize edges
              // and connection handles don't apply. Show grab and bail out.
              if (this._hitNodeLabel(n, x, y)) {
                n.hideConnectHandle();
                this._hideAllSourceHandles(n);
                this.cursor = "grab";
                return;
              }

              // resize

              const ext = this.selectedNodes.includes(n) ? this.outlineExtent : 0;
              var sx = x - (n.x - ext);
              var sy = y - (n.y - ext);
              let cursor = "default";
              let showDot = false;
              var dotX, dotY;
              const edgeSize = this.resizeEdgeSize + ext;
              const totalW = (n._dom.offsetWidth  || n.width)  + 2 * ext;
              const totalH = (n._dom.offsetHeight || n.height) + 2 * ext;
              const onScrollbar = !!this._scrollbarHit(n, x, y);
              const onLeft = sx >= 0 && sx <=edgeSize;
              // Scrollbar track sits inside content, just inside the right border;
              // when the cursor is on it, defer to default so the scrollbar reads
              // naturally. The outer right border remains resize.
              const onRight = !onScrollbar && sx >= totalW - edgeSize && sx <= totalW;
              const onTop = sy >= 0 && sy <= edgeSize;
              const onBottom = sy >= totalH - edgeSize && sy <= totalH;


              if (onTop && onLeft)      cursor = 'nw-resize';
              else if (onTop && onRight) cursor = 'ne-resize';
              else if (onBottom && onLeft) cursor = 'sw-resize';
              else if (onBottom && onRight) cursor = 'se-resize';
              else if (onTop) {
                           cursor = 'n-resize'; 
                          showDot = true;
                          dotX = n.x + n.width / 2;
                          dotY = n.y;
                          }
              else if (onBottom) {
                        cursor = 's-resize';
                        showDot = true;
                        dotX = n.x + n.width / 2;
                        dotY = n.y + n.height; 
                      }
              else if (onLeft)  {
                         cursor = 'w-resize';
                         showDot = true; 
                         dotX = n.x;
                         dotY = n.y + n.height / 2;
                        }
              else if (onRight) {
                         cursor = 'e-resize';
                         showDot = true; 
                         dotX = n.x + n.width;
                         dotY = n.y + n.height / 2;
                        }

              // Connection handle appears on the side nearest the cursor
              // (outside corner zones). Handle itself shows a default cursor
              // so the user reads it as a click-target rather than a resize.
              // Suppress while a competing action is committed-but-not-yet-promoted
              // (mouse down on body) or while editing - otherwise the hover code
              // re-shows the handle that mousedown / _enterEdit just hid.
              const suppressHandle = this._dragPending || this.editing;
              const handleSide = (this._can('createEdge') && !suppressHandle) ? this._hoverSide(n, x, y) : null;
              if (handleSide) {
                n.showConnectHandle(handleSide);
                // When hovering the handle's midpoint, reclaim the cursor
                // from resize so it reads as draggable.
                const mid = n.sideMidpoint(handleSide);
                if (Math.hypot(x - mid.x, y - mid.y) <= 10) {
                  cursor = "crosshair";
                }
              } else {
                n.hideConnectHandle();
              }
              this._hideAllSourceHandles(n);

              // Body of file/text nodes is fully draggable; advertise that
              // with grab when the bbox interior isn't claimed by a resize
              // edge or connect handle.
              if (cursor === "default" && !handleSide && (n.type === "file" || n.type === "text")) {
                cursor = "grab";
              }

              this.cursor = cursor;

          } else if (edge) {
              this._hideAllSourceHandles(null);
              for (const e of this._edges) { e.hovered = (e === edge); }
              this.requestDraw();

          } else {
            // no node hovered - _updateInteractionStyles will clear any
            // prior hover styling on next draw based on this._hoveredNode
            // being null (set in the transition block above).
            for (const e of this._edges) {
              e.hovered = false;
            }

            this._hideAllSourceHandles(null);

            this.cursor = "default";
            this.requestDraw();
          }


        }

        event_mouseleave(e) {
          // Cursor left the canvas; mousemove won't fire to clear hover state.
          // Reset any edge glow so it doesn't get stuck on, then redraw.
          let edgeWasHovered = false;
          for (const ed of this._edges) {
            if (ed.hovered) { ed.hovered = false; edgeWasHovered = true; }
          }
          let nodeWasHovered = false;
          if (this._hoveredNode) {
            this._emit('nodeHover', this._hoveredNode, 'leave');
            this._hoveredNode = null;
            nodeWasHovered = true;
          }
          if (edgeWasHovered || nodeWasHovered) this.requestDraw();
        }

        //////////////////////////
        ////// MOSUE DOWN ///////
        ////////////////////////
        event_mousedown(e) {
          /*
          There are three things to click on:
          1. open space
            RIGHT DOWN + MOVE : pans everything
          2. node
            LEFT (mouse down, mouse move) Drag node around

          3. edge
          */

            e.preventDefault();

            // Capture the pointer so move/up keep flowing back here even when
            // the cursor leaves the hitbox. Auto-released on pointerup/cancel.
            if (e.pointerId !== undefined) {
              try { this.hitbox_container.setPointerCapture(e.pointerId); } catch (_) {}
            }

            const rect = this.container.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;
          
            const { x, y } = this.toWorld(mouseX, mouseY);

            const node = this.hitNode(x, y), edge = this.hitEdge(x, y);

            if (node) {
                // Check scrollbar first - it lives at the right edge, overlapping the resize edge.
                if (e.button === 0 && this._scrollbarHit(node, x, y)) {
                    this.scrollingNode = node;
                    this.scrollStartY = y;
                    this.scrollStartTop = (node._scrollEl || node._dom).scrollTop;
                    this.requestDraw();
                    return;
                }

                // Connect-handle drag takes priority over node drag / resize
                // (but only when a source handle is visible at the cursor).
                if (e.button === 0 && this._can('createEdge')) {
                    const side = this._connectHandleHit(node, x, y);
                    if (side) {
                        this._startEdgeDraft(node, side, x, y);
                        return;
                    }
                }
                const edgeHit = (e.button === 0) ? this._edgeHit(node, x, y) : null;
                const onAnyEdge = edgeHit && (edgeHit.left || edgeHit.right || edgeHit.top || edgeHit.bottom);

                // Selection mutation is gated by the 'select' permission. When
                // denied, the click does not change selection (host can still
                // set selection via canvas.selection.set()). Drag-to-move on a
                // pre-selected node continues to work; drag-to-move on an
                // unselected node is a no-op (the user can't select it).
                if (this._can('select')) {
                    if (e.shiftKey) {
                        if (this.selectedNodes.includes(node)) {
                           this.selectedNodes = this.selectedNodes.filter(n => n !== node);
                        }
                        else {this.selectedNodes.push(node);}
                    } else {
                        if (!this.selectedNodes.includes(node)) this.selectedNodes = [node];
                    }
                    this.selectedEdge = null;
                    this._clickedEdge = null;
                    for (const ed of this._edges) { ed.selected = false; }
                }
                // mouse-clicking a node moves keyboard focus to it
                // so subsequent arrow nav originates from there. Done
                // unconditionally - focus follows the click even when
                // 'select' permission is denied. The browser tracks
                // input modality, so :focus-visible (the keyboard
                // focus ring) won't show on this mouse-driven focus.
                this.setFocusedNode(node);

                // Raise a clicked node above its peers so dragging doesn't
                // leave it tucked behind siblings. For groups, this only
                // reorders within the group_container layer - content nodes
                // and edges stay on top of all groups regardless.
                this._bringNodeToFront(node);

                if (this._can('resizeNode') && onAnyEdge && !(node._lock && node._lock.resize)) {
                    this.resizing = node;
                    this.resizeSides = edgeHit;
                    this.resizeStartX = x;
                    this.resizeStartY = y;
                    this.resizeStartDims = { x: node.x, y: node.y, width: node.width, height: node.height };
                    node.hideConnectHandle();
                } else if (e.button === 0) {
                    // Defer drag promotion until the cursor moves past a 4px
                    // threshold. Plain mousedown stays a "pending" click so a
                    // release without movement can be treated as a link follow
                    // (anchor lookup happens in mouseup). Modifier-click bypasses
                    // the threshold check at mouseup.
                    this._dragPending = {
                      node,
                      downX: x,
                      downY: y,
                      ctrlMeta: !!(e.ctrlKey || e.metaKey),
                      canDrag: this._can('dragNode') && !(node._lock && node._lock.move),
                    };
                    node.hideConnectHandle();
                }
            } else if (edge) {
              if (this._can('select')) {
                this.selectedNodes = [];
                this.selectedEdge = edge;
                this._clickedEdge = edge;

                for (const e of this._edges) { e.selected = false; }

                edge.selected = true;
              }

              // Arm a potential endpoint-reattach: if the user drags from here,
              // the endpoint closer to the click point will detach and follow
              // the cursor. A plain click (no drag) just selects the edge.
              if (e.button === 0 && this._can('createEdge')) {
                const last = edge.pathX.length - 1;
                if (last >= 0) {
                  const dFrom = Math.hypot(x - edge.pathX[0],    y - edge.pathY[0]);
                  const dTo   = Math.hypot(x - edge.pathX[last], y - edge.pathY[last]);
                  this._edgeReattachPending = {
                    edge,
                    detachedEnd: dFrom <= dTo ? "from" : "to",
                    downX: x,
                    downY: y,
                  };
                }
              }
            } else {
                // clicked in open space.
                if (this._can('select')) {
                  if (e.shiftKey) {
                      // Start marquee add/remove mode
                  } else {
                      // Start marquee replace mode
                      this.selectedNodes = [];
                  }

                  this.selectedEdge = null;
                  this._clickedEdge = null;
                  for (const ed of this._edges) { ed.selected = false; }
                  this.marqueeActive = true;
                  this.marqueeStartX = x;
                  this.marqueeStartY = y;
                }

              if (e.button == 2) {
                this.marqueeActive = false;
                // Only enter pan mode if at least one axis is allowed -
                // otherwise the cursor would flash "grab" with no effect.
                if (this._can('panX') || this._can('panY')) {
                  this.isPanning = true;
                  this.cursor = "grab";
                }
              }
            }
            this.requestDraw();
            this._maybeEmitSelectionChange();

          //   this.requestDraw();

        }
        ///////////////////////////
        /////// MOUSE UP /////////
        /////////////////////////
        event_mouseup(e) {

          e.preventDefault();
              // Prevent context menu from appearing on right-click

          if (this.edgeDraft) {
            const d = this.edgeDraft;
            let edgeDraftCommitted = false;
            let committedEdge = null;
            if (d.reattaching) {
              // Anchor held in d.fromNode. Self-loops are allowed (mousemove
              // already blocks same-side landings to avoid degenerate curves).
              if (d.toNode && d.toSide) {
                // capture old endpoints so we can refresh summaries
                // on both the detached and re-attached nodes.
                const prevFrom = d.edge.fromNode, prevTo = d.edge.toNode;
                if (d.detachedEnd === "from") {
                  d.edge.fromNode = d.toNode.id;
                  d.edge.fromSide = d.toSide;
                } else {
                  d.edge.toNode = d.toNode.id;
                  d.edge.toSide = d.toSide;
                }
                if (typeof d.edge._applyA11y === "function") d.edge._applyA11y();
                if (typeof this._refreshA11yNodeSummary === "function") {
                  const ids = new Set([prevFrom, prevTo, d.edge.fromNode, d.edge.toNode]);
                  for (const id of ids) this._refreshA11yNodeSummary(this.getNode(id));
                }
                edgeDraftCommitted = true;
                committedEdge = d.edge;
              }
              d.edge._hiddenForReattach = false;
            } else if (d.toNode && d.toSide) {
              const newEdge = new CanvasEdge(this, {
                id: this._newId(),
                fromNode: d.fromNode.id,
                toNode: d.toNode.id,
                fromSide: d.fromSide,
                toSide: d.toSide,
              });
              this._edges.push(newEdge);
              this._emit('edgeCreate', newEdge);
              edgeDraftCommitted = true;
              committedEdge = newEdge;
            } else if (this.createNodeOnDrop && this._can('createNode')) {
              const created = this._createNodeFromEdgeDrop(d);
              if (created) {
                this._emit('nodeCreate', created.node);
                const newEdge = new CanvasEdge(this, {
                  id: this._newId(),
                  fromNode: d.fromNode.id,
                  toNode: created.node.id,
                  fromSide: d.fromSide,
                  toSide: created.side,
                });
                this._edges.push(newEdge);
                this._emit('edgeCreate', newEdge);
                this.selectedNodes = [created.node];
                edgeDraftCommitted = true;
                committedEdge = newEdge;
              }
            }
            this._cancelEdgeDraft({ committed: edgeDraftCommitted, edge: committedEdge });
            if (edgeDraftCommitted) this._markDirty();
            return;
          }

          if (this.marqueeActive) {
            const p1 = { x: Math.min(this.marqueeStartX, this.mouseX), y: Math.min(this.marqueeStartY, this.mouseY) };
            const p2 = { x: Math.max(this.marqueeStartX, this.mouseX), y: Math.max(this.marqueeStartY, this.mouseY) };

            var nodesInRect = [];
            for (const n of this._nodes) {
              if ( n.x >= p1.x && n.y >= p1.y && (n.x + n.width) <= p2.x && (n.y + n.height) <= p2.y){
                nodesInRect.push(n)
              }
            }

            
                if (e.shiftKey) {
                    // toggle nodes
                    nodesInRect.forEach(n => {
                        if (this.selectedNodes.includes(n)) 
                             this.selectedNodes = this.selectedNodes.filter(s => s !== n);
                        else {
                          this.selectedNodes.push(n);
                        }
                    });
                } else {
                    this.selectedNodes = nodesInRect;
                }

          }
          // Link follow: a release without drag promotion is a click; a release
          // while ctrl/cmd was held bypasses the threshold check entirely. In
          // both cases, look for an <a href> under the cursor inside the node
          // and open it in a new tab.
          const pending = this._dragPending;
          const ctrlMeta = pending ? pending.ctrlMeta : false;
          const wasClick = !!pending && !this.dragging;
          if ((wasClick || ctrlMeta) && pending && pending.node && pending.node._dom) {
            const a = this._findAnchorAtClient(e.clientX, e.clientY, pending.node._dom);
            if (a && a.href) {
              const ev = {
                href: a.href,
                node: pending.node,
                originalEvent: e,
                defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
              };
              this._emit('linkClick', ev);
              if (!ev.defaultPrevented) window.open(a.href, "_blank", "noopener");
            }
          }

          const wasNodeDrag = this.dragging;
          const wasResize   = !!this.resizing;
          // Snapshot move/resize state before it's reset so we can emit
          // typed events after the cleanup runs.
          const movedDragNodes = wasNodeDrag ? this._collectMovedDragNodes() : null;
          const resizeTarget   = wasResize  ? this.resizing : null;
          const resizeOrigDims = wasResize  ? this.resizeStartDims : null;
          this._dragPending = null;
          this.dragging = false;
          this._dragExtraNodes = null;
          this._dragPrimary = null;
          this._activeGuides = [];
          this.isPanning = false;
          this.marqueeActive = false;
          this.resizing = null;
          this.resizeSides = null;
          this.scrollingNode = null;
          this._edgeReattachPending = null;
          this.cursor = "default";
          this._updateLinkInteractivity();
          this.requestDraw();
          if (wasNodeDrag || wasResize) this._markDirty();
          if (wasResize && resizeTarget && resizeOrigDims
              && (resizeOrigDims.width  !== resizeTarget.width
              ||  resizeOrigDims.height !== resizeTarget.height)) {
            this._emit('nodeResize', {
              node: resizeTarget,
              width: resizeTarget.width, height: resizeTarget.height,
              prevWidth: resizeOrigDims.width, prevHeight: resizeOrigDims.height,
            });
            this._emit('nodeUpdate', {
              node: resizeTarget, kind: 'size',
              width: resizeTarget.width, height: resizeTarget.height,
              prevWidth: resizeOrigDims.width, prevHeight: resizeOrigDims.height,
            });
          }
          if (wasNodeDrag && movedDragNodes) {
            for (const m of movedDragNodes) {
              this._emit('nodeMove', { node: m.node, x: m.node.x, y: m.node.y, prevX: m.prevX, prevY: m.prevY });
              this._emit('nodeUpdate', { node: m.node, kind: 'position', x: m.node.x, y: m.node.y, prevX: m.prevX, prevY: m.prevY });
            }
            this._emit('dragEnd', { nodes: movedDragNodes.map(m => m.node), moved: movedDragNodes.length > 0 });
          } else if (wasNodeDrag) {
            this._emit('dragEnd', { nodes: [], moved: false });
          }
          this._maybeEmitSelectionChange();
        }
        
        ///////////////////////////////////////////
        // Build the file picker panel DOM (the "add from file" dropdown).
        _buildFilePicker() {
          const picker = document.createElement("div");
          picker.className = "tc-file-picker";

          const input = document.createElement("input");
          input.type = "text";
          input.className = "tc-file-picker-input";
          input.placeholder = "Type a file path…";
          input.setAttribute("autocomplete", "off");

          const list = document.createElement("div");
          list.className = "tc-file-picker-list";
          if (!this.listFiles) list.style.display = "none";

          const hint = document.createElement("div");
          hint.className = "tc-file-picker-hint";
          hint.textContent = this.listFiles
            ? "↑↓ navigate, ↵ open, esc dismiss"
            : "↵ open, esc dismiss";

          picker.appendChild(input);
          picker.appendChild(list);
          picker.appendChild(hint);
          this.container.appendChild(picker);

          // Don't let the canvas steal clicks/keys while the picker is open.
          const stopProp = e => e.stopPropagation();
          picker.addEventListener("mousedown", stopProp);
          picker.addEventListener("mouseup", stopProp);
          picker.addEventListener("click", stopProp);
          picker.addEventListener("dblclick", stopProp);
          picker.addEventListener("wheel", stopProp);
          input.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Escape") {
              this._closeFilePicker();
            } else if (e.key === "Enter") {
              this._commitFilePicker();
            } else if (e.key === "ArrowDown") {
              e.preventDefault();
              this._moveFilePickerHighlight(1);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              this._moveFilePickerHighlight(-1);
            }
          });

          let debounceTimer = null;
          input.addEventListener("input", () => {
            if (!this.listFiles) return;
            clearTimeout(debounceTimer);
            const query = input.value;
            debounceTimer = setTimeout(() => this._runFilePickerQuery(query), 150);
          });

          // Clicking outside closes the picker.
          this._filePickerOutsideHandler = (e) => {
            if (!this._filePicker.open) return;
            if (picker.contains(e.target)) return;
            if (this.toolbarNewFileButton.contains(e.target)) return;
            this._closeFilePicker();
          };
          document.addEventListener("mousedown", this._filePickerOutsideHandler, true);

          this._filePicker = {
            root: picker,
            input: input,
            list: list,
            results: [],
            highlightIndex: -1,
            requestId: 0,
            open: false,
          };
        }

        _buildSettingsPanel() {
          const panel = document.createElement("div");
          panel.className = "tc-settings-panel";

          // Inputs keyed by setting name so _syncSettingsPanel can refresh
          // them from current canvas state (e.g. after setGrid() is called
          // programmatically by a theme).
          const inputs = {};

          const makeToggleRow = (labelText, key, getValue, onChange) => {
            const row = document.createElement("label");
            row.className = "tc-settings-row";
            const span = document.createElement("span");
            span.textContent = labelText;
            const sw = document.createElement("span");
            sw.className = "tc-switch";
            const input = document.createElement("input");
            input.type = "checkbox";
            input.checked = !!getValue();
            const slider = document.createElement("span");
            slider.className = "tc-slider";
            sw.appendChild(input);
            sw.appendChild(slider);
            row.appendChild(span);
            row.appendChild(sw);
            input.addEventListener("change", () => onChange(input.checked));
            inputs[key] = { el: input, kind: "checkbox", getValue };
            return row;
          };

          const makeSelectRow = (labelText, key, options, getValue, onChange) => {
            const row = document.createElement("label");
            row.className = "tc-settings-row";
            const span = document.createElement("span");
            span.textContent = labelText;
            const select = document.createElement("select");
            select.className = "tc-select";
            const current = getValue();
            for (const opt of options) {
              const o = document.createElement("option");
              o.value = opt.value;
              o.textContent = opt.label;
              if (opt.value === current) o.selected = true;
              select.appendChild(o);
            }
            row.appendChild(span);
            row.appendChild(select);
            select.addEventListener("change", () => onChange(select.value));
            inputs[key] = { el: select, kind: "select", getValue };
            return row;
          };

          panel.appendChild(makeToggleRow("Create node on drop", "createNodeOnDrop", () => this.createNodeOnDrop, (v) => {
            this.createNodeOnDrop = v;
          }));
          panel.appendChild(makeToggleRow("Node alignment", "snapToNodes", () => this.snapToNodes, (v) => {
            this.snapToNodes = v;
            if (!v) { this._activeGuides = []; this.requestDraw(); }
          }));
          panel.appendChild(makeToggleRow("Snap to grid", "snapToGrid", () => this.snapToGrid, (v) => {
            this.snapToGrid = v;
          }));
          panel.appendChild(makeSelectRow("Show grid", "showGrid", [
            { value: "off",   label: "Off" },
            { value: "lines", label: "Lines" },
            { value: "dots",  label: "Dots" },
          ], () => this.showGrid, (v) => {
            this.setGrid(v);
          }));

          this.container.appendChild(panel);

          const stopProp = e => e.stopPropagation();
          panel.addEventListener("mousedown", stopProp);
          panel.addEventListener("mouseup", stopProp);
          panel.addEventListener("click", stopProp);
          panel.addEventListener("wheel", stopProp);

          this._settingsOutsideHandler = (e) => {
            if (!this._settingsPanel.open) return;
            if (panel.contains(e.target)) return;
            if (this.toolbarSettingsButton.contains(e.target)) return;
            this._closeSettingsPanel();
          };
          document.addEventListener("mousedown", this._settingsOutsideHandler, true);

          this._settingsPanel = { root: panel, open: false, inputs };
        }

        _syncSettingsPanel() {
          const sp = this._settingsPanel;
          if (!sp || !sp.inputs) return;
          for (const key in sp.inputs) {
            const entry = sp.inputs[key];
            const current = entry.getValue();
            if (entry.kind === "checkbox") {
              entry.el.checked = !!current;
            } else if (entry.kind === "select") {
              entry.el.value = current;
            }
          }
        }

        _toggleSettingsPanel(e) {
          if (e && e.stopPropagation) e.stopPropagation();
          if (!this._can('editNodeStyle')) return;
          if (!this._settingsPanel) return;
          if (this._settingsPanel.open) this._closeSettingsPanel();
          else this._openSettingsPanel();
        }

        _openSettingsPanel() {
          const panel = this._settingsPanel.root;
          // Refresh control values from current canvas state so settings
          // changed programmatically (e.g. by applyTheme -> setGrid) are
          // reflected in the panel.
          this._syncSettingsPanel();
          panel.classList.add("open");
          this._settingsPanel.open = true;
          this.toolbarSettingsButton.classList.add("active");
          // Align under the gear button; fall back to default if measurements
          // are unavailable.
          const containerRect = this.container.getBoundingClientRect();
          const btnRect = this.toolbarSettingsButton.getBoundingClientRect();
          const top = btnRect.bottom - containerRect.top + 6;
          let left = btnRect.right - containerRect.left - panel.offsetWidth;
          if (left < 8) left = 8;
          panel.style.top = top + "px";
          panel.style.left = left + "px";
        }

        _closeSettingsPanel() {
          this._settingsPanel.root.classList.remove("open");
          this._settingsPanel.open = false;
          this.toolbarSettingsButton.classList.remove("active");
        }

        _openFilePicker(anchorEl = null) {
          const fp = this._filePicker;
          fp.input.value = "";
          fp.results = [];
          fp.highlightIndex = -1;
          fp.list.innerHTML = "";
          this._filePickerAnchor = anchorEl || null;
          if (anchorEl) {
            const r = anchorEl.getBoundingClientRect();
            const containerRect = this.container.getBoundingClientRect();
            const pickerH = 280;
            const pickerW = 300;
            // Position relative to the canvas container (picker is appended there, position:absolute).
            let top = (r.bottom - containerRect.top) + 6;
            let left = (r.left - containerRect.left);
            const maxTop = this.container.clientHeight - pickerH - 8;
            const maxLeft = this.container.clientWidth - pickerW - 8;
            if (maxTop > 0 && top > maxTop) top = maxTop;
            if (left < 8) left = 8;
            if (maxLeft > 8 && left > maxLeft) left = maxLeft;
            fp.root.style.top = top + "px";
            fp.root.style.left = left + "px";
          } else {
            fp.root.style.top = "";
            fp.root.style.left = "";
          }
          fp.root.classList.add("open");
          fp.open = true;
          fp.input.focus();
          // Pull initial results based on mode.
          if (this._filePickerImageOnly) {
            if (this.listImageFiles) this._runFilePickerQuery("");
          } else {
            if (this.listFiles) this._runFilePickerQuery("");
          }
        }

        _openImageFilePicker(onPick, anchorEl = null) {
          this._filePickerImageOnly = true;
          this._filePickerOnPick = onPick || null;
          this._openFilePicker(anchorEl);
        }

        _closeFilePicker() {
          const fp = this._filePicker;
          fp.root.classList.remove("open");
          fp.root.style.top = "";
          fp.root.style.left = "";
          fp.open = false;
          this._filePickerImageOnly = false;
          this._filePickerOnPick = null;
          this._filePickerAnchor = null;
        }

        async _runFilePickerQuery(query) {
          const fp = this._filePicker;
          const reqId = ++fp.requestId;
          const imageOnly = !!this._filePickerImageOnly;
          const cb = imageOnly ? this.listImageFiles : this.listFiles;
          if (!cb) {
            fp.results = [];
            fp.highlightIndex = -1;
            this._renderFilePickerList();
            return;
          }
          let results;
          try {
            results = await Promise.resolve(cb(query));
          } catch (err) {
            console.error((imageOnly ? "listImageFiles" : "listFiles") + " callback threw:", err);
            results = [];
          }
          // Latest-wins: discard stale responses.
          if (reqId !== fp.requestId) return;
          if (!Array.isArray(results)) results = [];
          // Normalize to {path, display} objects.
          let normalized = results.slice(0, 200).map(r =>
            typeof r === "string" ? { path: r, display: r } : { path: r.path, display: r.display || r.path }
          );
          // Defensive: in image-only mode, drop anything that doesn't look like an image file.
          if (imageOnly) {
            normalized = normalized.filter(r => fileKind(r.path) === "image");
          }
          fp.results = normalized;
          fp.highlightIndex = fp.results.length > 0 ? 0 : -1;
          this._renderFilePickerList();
        }

        _renderFilePickerList() {
          const fp = this._filePicker;
          fp.list.innerHTML = "";
          if (fp.results.length === 0) {
            const empty = document.createElement("div");
            empty.className = "tc-file-picker-empty";
            empty.textContent = fp.input.value ? "No matches" : "No files";
            fp.list.appendChild(empty);
            return;
          }
          fp.results.forEach((r, i) => {
            const row = document.createElement("div");
            row.className = "tc-file-picker-row" + (i === fp.highlightIndex ? " active" : "");
            row.textContent = r.display;
            row.addEventListener("mousedown", (e) => {
              e.preventDefault();
              e.stopPropagation();
              fp.highlightIndex = i;
              this._commitFilePicker();
            });
            row.addEventListener("mouseenter", () => {
              if (fp.highlightIndex === i) return;
              fp.highlightIndex = i;
              this._renderFilePickerList();
            });
            fp.list.appendChild(row);
          });
        }

        _moveFilePickerHighlight(delta) {
          const fp = this._filePicker;
          if (fp.results.length === 0) return;
          fp.highlightIndex = (fp.highlightIndex + delta + fp.results.length) % fp.results.length;
          this._renderFilePickerList();
          // Keep highlight in view.
          const active = fp.list.querySelector(".tc-file-picker-row.active");
          if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
        }

        _commitFilePicker() {
          const fp = this._filePicker;
          let path = "";
          if (fp.highlightIndex >= 0 && fp.results[fp.highlightIndex]) {
            path = fp.results[fp.highlightIndex].path;
          } else {
            path = fp.input.value.trim();
          }
          const onPick = this._filePickerOnPick;
          this._closeFilePicker();
          if (!path) return;
          if (onPick) {
            onPick(path);
          } else {
            this._createFileNode(path);
          }
        }

        _createFileNode(path) {
          const new_id = this._newId();

          // Center of the current viewport in canvas coordinates.
          const rect = this.container.getBoundingClientRect();
          const viewCenterX = (rect.width / 2 - this.panX) / this.scale;
          const viewCenterY = (rect.height / 2 - this.panY) / this.scale;

          const newNodeWidth = 250;
          const newNodeHeight = 180;

          const nodeData = {
            "id": new_id,
            "x": viewCenterX - newNodeWidth / 2,
            "y": viewCenterY - newNodeHeight / 2,
            "width": newNodeWidth,
            "height": newNodeHeight,
            "type": "file",
            "file": path,
          };

          const newNode = new CanvasNode(this, nodeData);
          this._nodes.push(newNode);
          this.selectedNodes = [newNode];
          this._emit('nodeCreate', newNode);
          this.requestDraw();
          this._markDirty();
        }

        _buildLinkPicker() {
          const picker = document.createElement("div");
          picker.className = "tc-file-picker tc-link-picker";

          const input = document.createElement("input");
          input.type = "url";
          input.className = "tc-file-picker-input";
          input.placeholder = "https://…";
          input.setAttribute("autocomplete", "off");

          const hint = document.createElement("div");
          hint.className = "tc-file-picker-hint";
          hint.textContent = "↵ create, esc dismiss";

          picker.appendChild(input);
          picker.appendChild(hint);
          this.container.appendChild(picker);

          const stopProp = e => e.stopPropagation();
          picker.addEventListener("mousedown", stopProp);
          picker.addEventListener("mouseup", stopProp);
          picker.addEventListener("click", stopProp);
          picker.addEventListener("dblclick", stopProp);
          picker.addEventListener("wheel", stopProp);
          input.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Escape") {
              this._closeLinkPicker();
            } else if (e.key === "Enter") {
              this._commitLinkPicker();
            }
          });

          this._linkPickerOutsideHandler = (e) => {
            if (!this._linkPicker.open) return;
            if (picker.contains(e.target)) return;
            if (this.toolbarNewLinkButton.contains(e.target)) return;
            this._closeLinkPicker();
          };
          document.addEventListener("mousedown", this._linkPickerOutsideHandler, true);

          this._linkPicker = { root: picker, input: input, open: false };
        }

        _openLinkPicker() {
          const lp = this._linkPicker;
          lp.input.value = "";
          lp.root.classList.add("open");
          lp.open = true;
          lp.input.focus();
        }

        _closeLinkPicker() {
          const lp = this._linkPicker;
          lp.root.classList.remove("open");
          lp.open = false;
        }

        _commitLinkPicker() {
          const lp = this._linkPicker;
          const url = lp.input.value.trim();
          this._closeLinkPicker();
          if (!url) return;
          this._createLinkNode(url);
        }

        _createLinkNode(url) {
          const new_id = this._newId();
          const rect = this.container.getBoundingClientRect();
          const viewCenterX = (rect.width / 2 - this.panX) / this.scale;
          const viewCenterY = (rect.height / 2 - this.panY) / this.scale;
          const newNodeWidth = 400;
          const newNodeHeight = 300;

          const nodeData = {
            "id": new_id,
            "x": viewCenterX - newNodeWidth / 2,
            "y": viewCenterY - newNodeHeight / 2,
            "width": newNodeWidth,
            "height": newNodeHeight,
            "type": "link",
            "url": url,
          };

          const newNode = new CanvasNode(this, nodeData);
          this._nodes.push(newNode);
          this.selectedNodes = [newNode];
          this._emit('nodeCreate', newNode);
          this.requestDraw();
          this._markDirty();
        }

        event_newLinkNode(e) {
          if (!this._can('createNode')) return;
          if (e && e.stopPropagation) e.stopPropagation();
          if (this._linkPicker && this._linkPicker.open) {
            this._closeLinkPicker();
          } else {
            this._openLinkPicker();
          }
        }

        // Per-key permission check. Prefer over _canEdit() for new code so
        // hosts can grant a single capability without disabling read-only.
        _can(key) { return this.permissions.get(key); }

        // Build the canvas-level toolbar plus the file / link / settings
        // panels. Each section/panel is gated by the specific permission it
        // exposes so a runtime permission change can rebuild only the
        // currently-allowed surface.
        _buildCanvasUI() {
          this.toolbar = document.createElement("div");
          this.toolbar.className = "tc-toolbar-canvas tc-toolbar-pos-tl tc-toolbar-anchor-bottom";

          const sections = [];
          if (this._can('createNode')) {
            sections.push({ key: "newNode",     emoji: "🔲", title: "New Node",      action: () => this._handlers.newNode() });
            sections.push({ key: "newFileNode", emoji: "📄", title: "New File Node", action: () => this._handlers.newFileNode() });
            sections.push({ key: "newLinkNode", emoji: "🔗", title: "New Link Node", action: () => this._handlers.newLinkNode() });
          }
          if (this._can('zoom')) {
            sections.push({ key: "zoomIn",  emoji: "➕", title: "Zoom In",             action: () => this._handlers.zoomIn() });
            sections.push({ key: "zoomOut", emoji: "➖", title: "Zoom Out",            action: () => this._handlers.zoomOut() });
            sections.push({ key: "reset",   emoji: "🎯", title: "Reset Zoom & Center", action: () => this._handlers.resetZoom() });
          }
          if (this._can('editNodeStyle')) {
            sections.push({ key: "settings", emoji: "⚙️", title: "Settings", action: () => this._toggleSettingsPanel() });
          }
          if (this.onSaveRequest && !this.permissions.isReadOnly()) {
            sections.push({ key: "save", emoji: "💾", title: "Save", action: () => this._handlers.saveCanvas() });
          }
          if (!this.permissions.isReadOnly()) {
            sections.push({ key: "resetCanvas", emoji: "↩️", title: "Reset to initial state", action: () => this._handlers.resetCanvas() });
          }

          this._wireToolbar(this.toolbar, sections);

          // Named-property aliases for back-compat with code that reaches in
          // to toggle visibility or read button rects. Each is only assigned
          // when its section was built - readers must null-check.
          this.toolbarZoomInButton       = this.toolbar._triggers.zoomIn       || null;
          this.toolbarZoomOutButton      = this.toolbar._triggers.zoomOut      || null;
          this.toolbarResetButton        = this.toolbar._triggers.reset        || null;
          this.toolbarNewButton          = this.toolbar._triggers.newNode      || null;
          this.toolbarNewFileButton      = this.toolbar._triggers.newFileNode  || null;
          this.toolbarNewLinkButton      = this.toolbar._triggers.newLinkNode  || null;
          this.toolbarSettingsButton     = this.toolbar._triggers.settings     || null;
          this.toolbarSaveButton         = this.toolbar._triggers.save         || null;
          this.toolbarResetCanvasButton  = this.toolbar._triggers.resetCanvas  || null;
          if (this.toolbarSaveButton)        this.toolbarSaveButton.style.display        = "none";
          if (this.toolbarResetCanvasButton) this.toolbarResetCanvasButton.style.display = "none";

          this.container.appendChild(this.toolbar);

          // File picker panel - only meaningful when createNode is allowed.
          if (this._can('createNode')) this._buildFilePicker();
          if (this._can('createNode')) this._buildLinkPicker();
          if (this._can('editNodeStyle')) this._buildSettingsPanel();
        }

        // Remove the canvas-level toolbar and any panels that _buildCanvasUI
        // installed. Mirrors the document-level outside-click handlers added
        // by the panel builders so a subsequent rebuild starts from a clean
        // state.
        _destroyCanvasUI() {
          if (this.toolbar && this.toolbar.parentNode) {
            this.toolbar.parentNode.removeChild(this.toolbar);
          }
          this.toolbar = null;
          this.toolbarZoomInButton = null;
          this.toolbarZoomOutButton = null;
          this.toolbarResetButton = null;
          this.toolbarNewButton = null;
          this.toolbarNewFileButton = null;
          this.toolbarNewLinkButton = null;
          this.toolbarSettingsButton = null;
          this.toolbarSaveButton = null;
          this.toolbarResetCanvasButton = null;

          if (this._filePicker && this._filePicker.root && this._filePicker.root.parentNode) {
            this._filePicker.root.parentNode.removeChild(this._filePicker.root);
          }
          if (this._filePickerOutsideHandler) {
            document.removeEventListener("mousedown", this._filePickerOutsideHandler, true);
            this._filePickerOutsideHandler = null;
          }
          this._filePicker = null;

          if (this._linkPicker && this._linkPicker.root && this._linkPicker.root.parentNode) {
            this._linkPicker.root.parentNode.removeChild(this._linkPicker.root);
          }
          if (this._linkPickerOutsideHandler) {
            document.removeEventListener("mousedown", this._linkPickerOutsideHandler, true);
            this._linkPickerOutsideHandler = null;
          }
          this._linkPicker = null;

          if (this._settingsPanel && this._settingsPanel.root && this._settingsPanel.root.parentNode) {
            this._settingsPanel.root.parentNode.removeChild(this._settingsPanel.root);
          }
          if (this._settingsOutsideHandler) {
            document.removeEventListener("mousedown", this._settingsOutsideHandler, true);
            this._settingsOutsideHandler = null;
          }
          this._settingsPanel = null;
        }

        // Tear down and rebuild the canvas-level UI. Used by PermissionsAPI
        // when permissions change at runtime so the toolbar and panels match
        // the currently-allowed operations.
        _rebuildCanvasUI() {
          // Preserve the visibility the host applied (via the toolbars.canvas.hidden
          // option or ui.toolbar.canvas.hide()) so a permission flip doesn't
          // spring a previously-hidden toolbar back into view.
          const wasHidden = !!(this.toolbar && this.toolbar.classList.contains("tc-toolbar-hidden"));
          this._destroyCanvasUI();
          this._buildCanvasUI();
          // The controller cached the prior toolbar DOM node - re-point it at
          // the new element so subsequent show()/hide() calls land on what's
          // actually mounted, then restore the saved visibility.
          if (this.ui && this.ui.toolbar && this.ui.toolbar.canvas) {
            this.ui.toolbar.canvas._tb = this.toolbar;
          }
          if (wasHidden && this.toolbar) this.toolbar.classList.add("tc-toolbar-hidden");
        }

        // Back-compat: returns true iff the canvas is not fully read-only.
        // Existing call sites have been migrated to _can('<key>') where the
        // gated operation is specific; this helper remains for any caller
        // that genuinely means "any edit is allowed."
        _canEdit() { return !this.permissions.isReadOnly(); }

        _createGroupFromSelection() {
          if (!this._can('group')) return;
          if (this.selectedNodes.length < 2) return;

          const bbox = getBoundingBox(this.selectedNodes);
          const pad = this.gridSize;

          const new_id = this._newId();

          const nodeData = {
            "id": new_id,
            "type": "group",
            "x": bbox.left - 2 * pad,
            "y": bbox.top - 2 * pad,
            "width": bbox.width + 4 * pad,
            "height": bbox.height + 4 * pad,
            "label": "Untitled group",
          };

          const newNode = new CanvasNode(this, nodeData);
          // Default placement is at the top of the group stack (CanvasNode's
          // constructor appends newNode._dom to group_container as its last
          // child). When the new group encloses other groups, that default
          // would hide their labels/bodies, so slot the new group BEHIND
          // every enclosed group instead - both in the DOM and in _nodes.
          const enclosed = [];
          for (const n of this._nodes) {
            if (n === newNode || n.type !== "group") continue;
            if (n.x >= newNode.x && n.y >= newNode.y
                && n.x + n.width  <= newNode.x + newNode.width
                && n.y + n.height <= newNode.y + newNode.height) {
              enclosed.push(n);
            }
          }
          if (enclosed.length) {
            // Pick the earliest enclosed group in DOM order; insert the new
            // group's _dom right before it so the others paint on top.
            let anchorDom = null;
            for (let child = this.group_container.firstChild; child; child = child.nextSibling) {
              if (enclosed.some(g => g._dom === child)) { anchorDom = child; break; }
            }
            if (anchorDom && newNode._dom && newNode._dom.parentNode === this.group_container) {
              this.group_container.insertBefore(newNode._dom, anchorDom);
            }
            // Mirror that placement in _nodes: position the new group just
            // before the earliest-indexed enclosed group.
            let anchorIdx = this._nodes.length;
            for (const g of enclosed) {
              const idx = this._nodes.indexOf(g);
              if (idx !== -1 && idx < anchorIdx) anchorIdx = idx;
            }
            this._nodes.splice(anchorIdx, 0, newNode);
          } else {
            // No enclosed groups - keep the default "on top" placement.
            // Sit just after the last existing group in _nodes so hit-test
            // order matches the DOM order set up by the constructor.
            let insertAt = 0;
            for (let k = 0; k < this._nodes.length; k++) {
              if (this._nodes[k].type === "group") insertAt = k + 1;
            }
            this._nodes.splice(insertAt, 0, newNode);
          }
          this.selectedNodes = [newNode];
          this._emit('nodeCreate', newNode);
          this.requestDraw();
          this._markDirty();

          // rAF defers until after _updateToolbars collapses the drawer on
          // selection change, so re-opening the label panel sticks.
          requestAnimationFrame(() => {
            if (this.nodeToolbar._setActive) this.nodeToolbar._setActive("label");
            if (this.nodeToolbarLabel) {
              this.nodeToolbarLabel.value = newNode.label;
              this.nodeToolbarLabel.focus();
              this.nodeToolbarLabel.select();
            }
          });
        }

        event_newFileNode(e) {
          if (!this._can('createNode')) return;
          if (e && e.stopPropagation) e.stopPropagation();
          if (this._filePicker && this._filePicker.open) {
            this._closeFilePicker();
          } else {
            this._openFilePicker();
          }
        }

        ///////////////////////////////////////////
        event_newNode(e) {
          if (!this._can('createNode')) return;

          var new_id = this._newId();

          const rect = this.container.getBoundingClientRect();
          const centerX = (rect.width / 2 - this.panX) / this.scale;
          const centerY = (rect.height / 2 - this.panY) / this.scale;
          const newNodeWidth = 200;
          const newNodeHeight = 150;

          var nodeData = {
            "id":new_id,
            "x": centerX - newNodeWidth / 2,
            "y": centerY - newNodeHeight / 2,
            "width":newNodeWidth,
            "height":newNodeHeight,
            "type":"text",
            "text":""
          }

          var newNode = new CanvasNode(this, nodeData);
          this._nodes.push(newNode);

          this.selectedNodes = [newNode];
          this._emit('nodeCreate', newNode);
          this.requestDraw();
          this._markDirty();
        }

        _createNodeFromEdgeDrop(draft) {
          if (!this._can('createNode')) return null;
          const A = draft.fromNode.sideMidpoint(draft.fromSide);
          if (!A) return null;
          const mx = draft.mouseX;
          const my = draft.mouseY;
          const dx = mx - A.x;
          const dy = my - A.y;
          const w = 200;  // new node width and height. 
          const h = 150;
          let toSide, x, y;
          if (Math.abs(dx) >= Math.abs(dy)) {
            if (dx >= 0) { toSide = "left";  x = mx;         y = my - h / 2; }
            else         { toSide = "right"; x = mx - w;     y = my - h / 2; }
          } else {
            if (dy >= 0) { toSide = "top";    x = mx - w / 2; y = my;     }
            else         { toSide = "bottom"; x = mx - w / 2; y = my - h; }
          }
          const new_id = this._newId();
          const nodeData = {
            id: new_id,
            x: x,
            y: y,
            width: w,
            height: h,
            type: "text",
            text: "",
          };
          const fromColor = draft.fromNode.color;
          if (fromColor && fromColor !== "default") nodeData.color = fromColor;
          const newNode = new CanvasNode(this, nodeData);
          this._nodes.push(newNode);
          return { node: newNode, side: toSide };
        }

        // Zoom around a screen-space anchor point, clamped to the same
        // limits used by the mouse wheel handler.
        _zoomAround(zoomFactor, anchorX, anchorY) {
          const newScale = Math.min(Math.max(this.scale * zoomFactor, 0.2), 5);
          this.panX = anchorX - (anchorX - this.panX) * (newScale / this.scale);
          this.panY = anchorY - (anchorY - this.panY) * (newScale / this.scale);
          this.scale = newScale;
          this.updateTransform();

          if (this.outer_container) {
            this._sizeCanvas();
          }
          this.requestDraw();
          this._emitViewportChange();
        }

        event_zoomIn(e) {
          if (!this._can('zoom')) return;
          const rect = this.container.getBoundingClientRect();
          this._zoomAround(1.1, rect.width / 2, rect.height / 2);
        }

        event_zoomOut(e) {
          if (!this._can('zoom')) return;
          const rect = this.container.getBoundingClientRect();
          this._zoomAround(0.9, rect.width / 2, rect.height / 2);
        }

        event_resetZoom(e) {
          if (!this._can('zoom')) return;
          this.scale = 1.0;
          const rect = this.container.getBoundingClientRect();

          if (this._nodes.length === 0) {
            this.panX = 0;
            this.panY = 0;
          } else {
            let sumX = 0, sumY = 0;
            for (const n of this._nodes) {
              sumX += n.x + n.width / 2;
              sumY += n.y + n.height / 2;
            }
            const avgX = sumX / this._nodes.length;
            const avgY = sumY / this._nodes.length;
            // Place the average center at the viewport center in screen space:
            // screen = world * scale + pan → pan = screenCenter - world * scale
            this.panX = rect.width / 2 - avgX * this.scale;
            this.panY = rect.height / 2 - avgY * this.scale;
          }

          this.updateTransform();
          this._emitViewportChange();

          if (this.outer_container) {
            this._sizeCanvas();
          }
          this.requestDraw();
        }

        // True if `id` is already taken by a node or an edge. Both share the
        // same lookup / DOM-id namespace, so an id must be unique across both.
        // Guarded against the early constructor call (_instanceId) that runs
        // before _nodes/_edges are initialized.
        _idInUse(id) {
          return (this._nodes && this._nodes.some(n => n.id === id))
              || (this._edges && this._edges.some(e => e.id === id))
              || false;
        }
        // Raw 16-char hex id (Obsidian .canvas convention). Not collision-checked
        // on its own - go through _newId() for that.
        _rawId() {
          return Math.floor(Math.random() * 0xffffffff).toString(16).padEnd(6, "0")
               + Math.floor(Math.random() * 0xffffffff).toString(16).padEnd(6, "0");
        }
        // The single id generator: loops until the id is free across nodes+edges.
        // `reserved` (optional Set) covers in-batch generation where freshly made
        // ids aren't in _nodes/_edges yet (see _normalizeData).
        _newId(reserved) {
          let id;
          do { id = this._rawId(); } while (this._idInUse(id) || (reserved && reserved.has(id)));
          return id;
        }

        _snapValue(v) {
          return this.snapToGrid ? Math.round(v / this.gridSize) * this.gridSize : v;
        }

        _snapToNodes(nx, ny, nw, nh, ignore) {
          // For the moving node's three X-axis targets (left/center/right) and
          // three Y-axis targets (top/center/bottom), find the nearest matching
          // edge/center on any non-ignored node within snapThreshold (in screen
          // pixels, so divide by scale). Adjust nx/ny and emit guide segments.
          const tol = this.snapThreshold / this.scale;
          const mxs = [
            { k: "l", v: nx },
            { k: "c", v: nx + nw / 2 },
            { k: "r", v: nx + nw },
          ];
          const mys = [
            { k: "t", v: ny },
            { k: "m", v: ny + nh / 2 },
            { k: "b", v: ny + nh },
          ];
          let bestX = null, bestY = null;
          const guideCandidatesX = [];
          const guideCandidatesY = [];
          for (const other of this._nodes) {
            if (ignore && ignore.has(other)) continue;
            const ox = other.x, oy = other.y, ow = other.width, oh = other.height;
            const oxs = [ox, ox + ow / 2, ox + ow];
            const oys = [oy, oy + oh / 2, oy + oh];
            for (const m of mxs) {
              for (const t of oxs) {
                const d = Math.abs(m.v - t);
                if (d <= tol && (!bestX || d < bestX.d)) {
                  bestX = { d, shift: t - m.v, x: t, other };
                }
              }
            }
            for (const m of mys) {
              for (const t of oys) {
                const d = Math.abs(m.v - t);
                if (d <= tol && (!bestY || d < bestY.d)) {
                  bestY = { d, shift: t - m.v, y: t, other };
                }
              }
            }
          }
          const guides = [];
          if (bestX) {
            nx += bestX.shift;
            const o = bestX.other;
            const y1 = Math.min(ny, o.y) - 20;
            const y2 = Math.max(ny + nh, o.y + o.height) + 20;
            guides.push({ axis: "v", x: bestX.x, y1, y2 });
          }
          if (bestY) {
            ny += bestY.shift;
            const o = bestY.other;
            const x1 = Math.min(nx, o.x) - 20;
            const x2 = Math.max(nx + nw, o.x + o.width) + 20;
            guides.push({ axis: "h", y: bestY.y, x1, x2 });
          }
          return { x: nx, y: ny, guides };
        }

        _aspectFor(node, e) {
          if (e && e.shiftKey) return null;
          if (node.type === "file" && node._imageAspect) return node._imageAspect;
          if (node.type === "group" && node.backgroundStyle === "ratio" && node._backgroundImageAspect)
            return node._backgroundImageAspect;
          return null;
        }

        _edgeHit(node, x, y) {
          // Selected nodes draw an outline outside the border; treat that ring
          // as part of the resize strip so cursor over the visible outline
          // still triggers resize.
          const ext = this.selectedNodes.includes(node) ? this.outlineExtent : 0;
          const edgeSize = this.resizeEdgeSize + ext;
          const totalW = (node._dom.offsetWidth  || node.width)  + 2 * ext;
          const totalH = (node._dom.offsetHeight || node.height) + 2 * ext;
          const sx = x - (node.x - ext), sy = y - (node.y - ext);
          return {
            left:   sx >= 0 && sx <= edgeSize,
            right:  sx >= totalW - edgeSize && sx <= totalW,
            top:    sy >= 0 && sy <= edgeSize,
            bottom: sy >= totalH - edgeSize && sy <= totalH,
          };
        }

        _hoverSide(node, x, y) {
          // Returns which side's connection handle should be shown, or null.
          // Cursor must be within a tolerance of one side and in that side's
          // middle region (not in the corner zone, which is resize territory).
          const w = node._dom.offsetWidth  || node.width;
          const h = node._dom.offsetHeight || node.height;
          const sx = x - node.x, sy = y - node.y;
          const pad = 10;
          if (sx < -pad || sx > w + pad || sy < -pad || sy > h + pad) return null;
          const dL = sx, dR = w - sx, dT = sy, dB = h - sy;
          let side = "left", dist = dL;
          if (dR < dist) { side = "right";  dist = dR; }
          if (dT < dist) { side = "top";    dist = dT; }
          if (dB < dist) { side = "bottom"; dist = dB; }
          // how close to a side the cursor must be
          const tol = 20; 
          if (dist > tol) return null;
          const marginFrac = 0.2;
          if (side === "left" || side === "right") {
            if (sy < h * marginFrac || sy > h * (1 - marginFrac)) return null;
          } else {
            if (sx < w * marginFrac || sx > w * (1 - marginFrac)) return null;
          }
          return side;
        }

        _connectHandleHit(node, x, y) {
          // True only if a source handle is currently shown on this node AND
          // the cursor is within the handle's clickable radius.
          if (!node._connectHandle) return null;
          if (node._connectHandle.style.display === "none") return null;
          const side = node._connectHandleSide;
          if (!side) return null;
          const mid = node.sideMidpoint(side);
          if (Math.hypot(x - mid.x, y - mid.y) > 14) return null;
          return side;
        }

        _targetSideSnap(node, x, y) {
          // During a drag, pick which target side to snap to on the hovered
          // node (the one whose midpoint is closest to the cursor, within a
          // snap radius). Returns null for "free cursor" (no snap).
          const sides = ["left","right","top","bottom"];
          let best = null, bestD = Infinity;
          for (const s of sides) {
            const m = node.sideMidpoint(s);
            const d = Math.hypot(x - m.x, y - m.y);
            if (d < bestD) { bestD = d; best = s; }
          }
          const snapR = Math.max(node.width, node.height) / 2; 
          return bestD <= snapR ? best : null;
        }

        _hideAllSourceHandles(except) {
          for (const n of this._nodes) {
            if (n === except) continue;
            n.hideConnectHandle && n.hideConnectHandle();
          }
        }

        _hideAllTargetHandles() {
          for (const n of this._nodes) {
            n.hideTargetHandles && n.hideTargetHandles();
          }
        }

        _startEdgeDraft(fromNode, fromSide, x, y) {
          if (!this._can('createEdge')) return;
          this.edgeDraft = {
            fromNode,
            fromSide,
            toNode: null,
            toSide: null,
            mouseX: x,
            mouseY: y,
          };
          // Keep the source handle visible as the origin indicator.
          fromNode.showConnectHandle(fromSide);
          this._hideAllSourceHandles(fromNode);
          this.cursor = "crosshair";
          this.requestDraw();
          this._emit('connectStart', { fromNode, fromSide });
        }

        _startEdgeReattach(edge, detachedEnd, x, y) {
          if (!this._can('createEdge')) return;
          // The endpoint named by detachedEnd ("from" or "to") follows the
          // cursor; the opposite end stays anchored. We reuse the same
          // edgeDraft fields (fromNode/fromSide as "anchor") so the existing
          // preview and target-snap logic work unchanged.
          const anchorIsFrom = detachedEnd === "to";
          const anchorNodeId = anchorIsFrom ? edge.fromNode : edge.toNode;
          const anchorSide   = anchorIsFrom ? edge.fromSide : edge.toSide;
          const anchorNode   = this.getNode(anchorNodeId);
          if (!anchorNode) return;

          this.edgeDraft = {
            reattaching: true,
            edge,
            detachedEnd,
            fromNode: anchorNode,
            fromSide: anchorSide,
            toNode: null,
            toSide: null,
            mouseX: x,
            mouseY: y,
          };
          edge._hiddenForReattach = true;
          anchorNode.showConnectHandle(anchorSide);
          this._hideAllSourceHandles(anchorNode);
          this.cursor = "crosshair";
          this.requestDraw();
        }

        // Optional result describes a successful commit so connectEnd can
        // tell consumers whether the draft produced an edge.
        _cancelEdgeDraft(result) {
          if (!this.edgeDraft) return;
          const committed = !!(result && result.committed);
          const edge     = result ? result.edge || null : null;
          const fromNode = this.edgeDraft.fromNode;
          const fromSide = this.edgeDraft.fromSide;
          if (this.edgeDraft.edge) this.edgeDraft.edge._hiddenForReattach = false;
          this.edgeDraft = null;
          this._hideAllTargetHandles();
          this._hideAllSourceHandles(null);
          this.cursor = "default";
          this.requestDraw();
          this._emit('connectEnd', { committed, edge, fromNode, fromSide });
        }

        _drawEdgePreview() {
          const d = this.edgeDraft;
          const start = d.fromNode.sideMidpoint(d.fromSide);
          let end;
          if (d.toNode && d.toSide) {
            end = d.toNode.sideMidpoint(d.toSide);
          } else {
            end = { x: d.mouseX, y: d.mouseY, dx: 0, dy: 0 };
          }
          const s = this._edgePreviewStyle || {};
          const curvature = s.curvature != null ? s.curvature : 1;
          const { ctrl1, ctrl2 } = CanvasEdge._bezierControls(start, end, curvature);
          const ctx = this.ctx;
          ctx.save();
          ctx.strokeStyle = s.stroke != null ? s.stroke : this._palette.edgePreviewColor;
          ctx.lineWidth = (s.width != null ? s.width : 2) / this.scale;
          // Allow dash=null (or []) for a solid preview; otherwise default
          // to the historical [8,4] pattern, scaled by zoom.
          const dash = s.dash != null ? s.dash : [8, 4];
          if (Array.isArray(dash) && dash.length) {
            ctx.setLineDash(dash.map(d => d / this.scale));
          } else {
            ctx.setLineDash([]);
          }
          if (s.dashOffset != null) ctx.lineDashOffset = s.dashOffset / this.scale;
          if (s.cap) ctx.lineCap = s.cap;
          if (s.glow && s.glow.color) {
            ctx.shadowColor = s.glow.color;
            ctx.shadowBlur  = (s.glow.blur != null ? s.glow.blur : 6) / this.scale;
          }
          ctx.beginPath();
          ctx.moveTo(start.x, start.y);
          ctx.bezierCurveTo(ctrl1.x, ctrl1.y, ctrl2.x, ctrl2.y, end.x, end.y);
          ctx.stroke();
          ctx.restore();
        }

        _borders(node) {
          const cs = getComputedStyle(node._dom);
          return {
            l: parseFloat(cs.borderLeftWidth)   || 0,
            r: parseFloat(cs.borderRightWidth)  || 0,
            t: parseFloat(cs.borderTopWidth)    || 0,
            b: parseFloat(cs.borderBottomWidth) || 0,
          };
        }

        _scrollbarWidth(node) {
          // Visible vertical scrollbar width (in world/layout coords), else 0.
          const dom = node._scrollEl;
          if (!dom) return 0;
          if (dom.scrollHeight <= dom.clientHeight) return 0;
          const cs = getComputedStyle(dom);
          const bl = parseFloat(cs.borderLeftWidth)  || 0;
          const br = parseFloat(cs.borderRightWidth) || 0;
          const sbw = dom.offsetWidth - dom.clientWidth - bl - br;
          return sbw > 0 ? sbw : 0;
        }

        _scrollbarHit(node, x, y) {
          // Scrollbar sits inside the content box (offset by the left border)
          // at the right edge of content, full content height.
          const sbw = this._scrollbarWidth(node);
          if (!sbw) return 0;
          const b = this._borders(node);
          const sx = x - node.x, sy = y - node.y;
          const trackLeft  = b.l + node.width - sbw;
          const trackRight = b.l + node.width;
          const onTrack = sx >= trackLeft && sx <= trackRight
                       && sy >= b.t && sy <= b.t + node.height;
          return onTrack ? sbw : 0;
        }

        // _pickSide(fromNode, toNode) {
        //   const fcx = fromNode.x + fromNode.width / 2,  fcy = fromNode.y + fromNode.height / 2;
        //   const tcx = toNode.x   + toNode.width   / 2,  tcy = toNode.y   + toNode.height   / 2;
        //   const dx = tcx - fcx, dy = tcy - fcy;
        //   let fromSide, toSide;
        //   if (Math.abs(dx) > Math.abs(dy)) {
        //     fromSide = dx > 0 ? "right" : "left";
        //     toSide   = dx > 0 ? "left"  : "right";
        //   } else {
        //     fromSide = dy > 0 ? "bottom" : "top";
        //     toSide   = dy > 0 ? "top"    : "bottom";
        //   }
        //   return { fromSide, toSide };
        // }

        // Right-click handler. Suppresses the native browser menu (preserved
        // back-compat), cancels any in-flight edge draft, then emits
        // 'contextMenu' so hosts can show their own menus. Payload identifies
        // what was clicked (node / edge / canvas) and the sub-zone within it.
        event_contextmenu(e) {
          e.preventDefault();
          // Host-level menu is gated by the 'contextMenu' permission. The
          // event still preventDefaults the native browser menu so a locked
          // canvas doesn't flash the OS menu either.
          if (!this._can('contextMenu')) return;
          // Suppress when this right-click was actually a pan-drag.
          if (this._panDidMove) {
            this._panDidMove = false;
            return;
          }
          this._cancelEdgeDraft();

          const rect = this.container.getBoundingClientRect();
          const mouseX = e.clientX - rect.left;
          const mouseY = e.clientY - rect.top;
          const { x, y } = this.toWorld(mouseX, mouseY);

          const node = this.hitNode(x, y);
          const edge = node ? null : this.hitEdge(x, y);

          let ctx;
          if (node) {
            let zone = 'body';
            let sides = null;
            let side = null;

            // Precedence mirrors event_mousedown routing.
            if (this._scrollbarHit(node, x, y)) {
              zone = 'scrollbar';
            } else {
              const handleSide = this._connectHandleHit(node, x, y);
              if (handleSide) {
                zone = 'connect-handle';
                side = handleSide;
              } else {
                const hit = this._edgeHit(node, x, y);
                if (hit && (hit.left || hit.right || hit.top || hit.bottom)) {
                  zone = 'resize-edge';
                  sides = { left: !!hit.left, right: !!hit.right, top: !!hit.top, bottom: !!hit.bottom };
                } else if (this._hitNodeLabel(node, x, y)) {
                  zone = 'label';
                } else if (node.type === 'group') {
                  // hitNode only returned a group when on label or perimeter;
                  // label was tested above, so this must be perimeter.
                  zone = 'perimeter';
                }
              }
            }

            ctx = { target: 'node', node, edge: null, zone, sides, side };
          } else if (edge) {
            // Classify which third of the edge was hit so hosts can offer
            // endpoint-specific menu items (e.g. reattach this end).
            let zone = 'mid';
            let side = null;
            const px = edge.pathX, py = edge.pathY;
            if (px && py && px.length >= 2) {
              const R = edge.hitRadius;
              const C = { x, y };
              let hitIdx = -1;
              for (let i = 0; i < px.length - 1; i++) {
                const P = { x: px[i],   y: py[i]   };
                const Q = { x: px[i+1], y: py[i+1] };
                if (edge.__lineCircleCollision(R, C, P, Q)) { hitIdx = i; break; }
              }
              if (hitIdx >= 0) {
                const frac = hitIdx / Math.max(1, px.length - 1);
                if (frac < 1/3) { zone = 'near-from'; side = edge.fromSide; }
                else if (frac >= 2/3) { zone = 'near-to'; side = edge.toSide; }
              }
            }
            ctx = { target: 'edge', node: null, edge, zone, sides: null, side };
          } else {
            ctx = { target: 'canvas', node: null, edge: null, zone: null, sides: null, side: null };
          }

          ctx.world = { x, y };
          ctx.screen = { x: e.clientX, y: e.clientY };
          ctx.originalEvent = e;

          this._emit('contextMenu', ctx);
        }

        // True only when the drag originated from outside the browser (OS file
        // drag). Internal drags never put 'Files' on dataTransfer.types.
        _isExternalFileDrag(e) {
          const types = e.dataTransfer && e.dataTransfer.types;
          if (!types) return false;
          for (let i = 0; i < types.length; i++) {
            if (types[i] === 'Files') return true;
          }
          return false;
        }

        event_dragenter(e) {
          if (!this._isExternalFileDrag(e)) return;
          e.preventDefault();
        }

        event_dragover(e) {
          if (!this._isExternalFileDrag(e)) return;
          // preventDefault is required to receive the subsequent 'drop'.
          e.preventDefault();
          if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
        }

        event_dragleave(e) {
          // No-op. Kept symmetric for a possible future hover indicator.
        }

        event_drop(e) {
          if (!this._isExternalFileDrag(e)) return;
          e.preventDefault();

          const files = e.dataTransfer ? e.dataTransfer.files : null;
          if (!files || files.length === 0) return;

          const rect = this.container.getBoundingClientRect();
          const mouseX = e.clientX - rect.left;
          const mouseY = e.clientY - rect.top;
          const { x, y } = this.toWorld(mouseX, mouseY);
          const node = this.hitNode(x, y);

          const position = {
            world: { x, y },
            screen: { x: e.clientX, y: e.clientY },
            node,
            originalEvent: e,
          };

          // Snapshot - FileList is live and listeners may take time.
          const list = Array.from(files);
          for (const file of list) {
            this._emit('fileDrop', file, position);
          }
        }

        event_dblclick(e) {
          if (!this._can('editText')) return;
          if (this.edgeDraft) return;
          const rect = this.container.getBoundingClientRect();
          const { x, y } = this.toWorld(e.clientX - rect.left, e.clientY - rect.top);
          const node = this.hitNode(x, y);
          if (node && node.type === "text") {
            e.preventDefault();
            this._enterEdit(node);
          }
        }

        _enterEdit(node) {
          if (!this._can('editText')) return;
          if (node && node._lock && node._lock.edit) return;
          if (this.editing) return;
          if (!this._emitCancellable('beforeNodeEditStart', node)) return;
          this.editing = node;
          this._editOriginalText = node.text || "";
          node.hideConnectHandle();

          this.hitbox_container.style.pointerEvents = "none";

          let inner = node._dom.querySelector(":scope > div");
          if (!inner) {
            inner = document.createElement("div");
            inner.style.padding = "10px";
            node._dom.appendChild(inner);
          }
          // Show raw markdown source for editing, not the rendered HTML.
          inner.textContent = node.text || "";
          inner.contentEditable = "true";
          inner.style.outline = "none";
          inner.style.cursor = "text";
          inner.style.whiteSpace = "pre-wrap";
          inner.focus();

          const range = document.createRange();
          range.selectNodeContents(inner);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);

          this._editInner = inner;
          this._editBlur = () => this._exitEdit(true);
          this._editKeydown = (ke) => {
            if (ke.key === "Escape") {
              ke.preventDefault();
              ke.stopPropagation();
              this._exitEdit(false);
            } else if (ke.key === "Enter" && !ke.shiftKey) {
              ke.preventDefault();
              this._exitEdit(true);
            }
          };
          inner.addEventListener("blur", this._editBlur);
          inner.addEventListener("keydown", this._editKeydown);
          this._emit('nodeEditStart', node);
        }

        // Flush all transient interaction state before a model-replacing
        // operation (clearCanvas / history apply / destroy) tears node DOM out
        // from under it. commit=false discards the in-progress edit buffer
        // (reverts to the text captured at _enterEdit). Idempotent: each guarded
        // call is a no-op when its state is inactive. NOT used by io.reconcile,
        // which must preserve the user's place - see the reconcile edit-contract.
        _abortInteraction({ commit = false } = {}) {
          if (this.editing)   this._exitEdit(commit);
          if (this.edgeDraft) this._cancelEdgeDraft();
          this._dragPending = null;
        }

        _exitEdit(commit) {
          if (!this.editing) return;
          const node = this.editing;
          const inner = this._editInner;
          inner.removeEventListener("blur", this._editBlur);
          inner.removeEventListener("keydown", this._editKeydown);

          const textChanged = commit && inner.innerText !== this._editOriginalText;
          const oldText = this._editOriginalText;
          if (commit) {
            node.text = inner.innerText;
          } else {
            node.text = this._editOriginalText;
          }

          inner.contentEditable = "false";
          inner.style.cursor = "";
          inner.style.whiteSpace = "";
          inner.blur();

          this.editing = null;

          if (textChanged || node._html == null) {
            node._renderMarkdownContent();
          } else {
            // _html is the sanitized cache (sanitized once at cache time in
            // _renderMarkdownContent), so this raw restore is safe - do NOT
            // re-wrap it in renderUntrustedHTML.
            inner.innerHTML = node._html;
          }
          if (textChanged) node._applyA11y();

          this.hitbox_container.style.pointerEvents = "";
          this._editInner = null;
          this._editBlur = null;
          this._editKeydown = null;
          this._editOriginalText = null;
          // return keyboard focus to the wrapping node so the
          // user doesn't fall back to <body> after Enter/Escape. Only
          // when this node was (or becomes) the focused one - host
          // code may have already shifted focus elsewhere.
          if (this.focusedNode === node || this.focusedNode == null) {
            this.setFocusedNode(node);
          }
          this.requestDraw();
          if (textChanged) {
            this._emit('nodeEdit', node, oldText, node.text);
            this._markDirty();
          }
          this._emit('nodeEditEnd', {
            node,
            committed: !!commit,
            changed: textChanged,
            oldText,
            newText: node.text,
          });
        }

        event_keydown(e) {
          const target = e.target;
          if (target && (target.isContentEditable ||
              target.tagName === "INPUT" || target.tagName === "TEXTAREA")) {
            return;
          }

          // Help-dialog modal: Escape always closes it regardless of how
          // `cancel` is rebound. Other keys are swallowed so global
          // handlers don't act on dialog keystrokes.
          if (this._helpDialogEl) {
            if (e.key === "Escape") {
              this._hideHelpDialog();
              e.preventDefault();
            }
            return;
          }

          const canonical = eventToCanonical(e);
          const actionName = this._shortcutIndex.get(canonical);
          if (!actionName) return;
          const entry = this._shortcuts.get(actionName);
          if (!entry || entry.current == null) return;
          const handled = entry.handler(e, this);
          if (handled !== false) e.preventDefault();
        }

        // ---- built-in action handlers ----
        // Each returns truthy when it actually did something (so the
        // dispatcher should preventDefault) and false to opt out. Most
        // built-ins return true unconditionally; a few gate on state
        // (focus, selection, permissions) and return false to leave the
        // event alone for the host page.

        _doHelp() { this._showHelpDialog(); return true; }

        _doFocusFirst() {
          if (!this._nodes.length || !this._canvasHasKeyboardFocus()) return false;
          return !!this._focusFirstNode();
        }

        _doFocusLast() {
          if (!this._nodes.length || !this._canvasHasKeyboardFocus()) return false;
          return !!this._focusLastNode();
        }

        _doSelectFocused() {
          // Bridge focus -> selection for keyboard users. Move mode and the
          // arrow-move keys act on selectedNodes, not the focused node, so a
          // keyboard-only user needs a way to promote the focused node into
          // the selection. Space is the ARIA-conventional toggle. (Space
          // inside a text edit still types a space: event_keydown bails out
          // early for INPUT/TEXTAREA/contentEditable before reaching here.)
          const node = this.focusedNode;
          if (!node) return false; // no focus target -> leave Space to the host page
          const label = (typeof node._effectiveA11y === "function" && node._effectiveA11y().label) || "Node";
          if (node.isSelected()) {
            node.deselect();
            this._announce(label + " deselected.");
          } else {
            node.select({ additive: true });
            this._announce(label + " selected.");
          }
          return true;
        }

        _doToggleMoveMode() {
          // M toggles an explicit nav-mode override so a user with a
          // selection can navigate focus without losing it. Only meaningful
          // when a selection is present; with no selection we're already
          // in navigate mode. Announce the effective mode if it
          // actually flipped (silent no-op otherwise).
          const prevMode = this._arrowMode();
          this._navigationModeOverride = this._navigationModeOverride === "navigate" ? null : "navigate";
          const nextMode = this._arrowMode();
          if (prevMode !== nextMode) {
            this._announce(nextMode === "navigate" ? "Navigate mode." : "Move mode.");
          }
          return true;
        }

        _doMoveLeft()  { return this._dispatchArrow("left",  this.gridSize); }
        _doMoveRight() { return this._dispatchArrow("right", this.gridSize); }
        _doMoveUp()    { return this._dispatchArrow("up",    this.gridSize); }
        _doMoveDown()  { return this._dispatchArrow("down",  this.gridSize); }

        _doNudgeLeft()  { return this._dispatchArrow("left",  1, /*forceMove*/ true); }
        _doNudgeRight() { return this._dispatchArrow("right", 1, /*forceMove*/ true); }
        _doNudgeUp()    { return this._dispatchArrow("up",    1, /*forceMove*/ true); }
        _doNudgeDown()  { return this._dispatchArrow("down",  1, /*forceMove*/ true); }

        _doCancel() {
          if (this.resizing) {
            const n = this.resizing, s = this.resizeStartDims;
            n.x = s.x; n.y = s.y; n.width = s.width; n.height = s.height;
            n._dom.style.left = s.x + "px";
            n._dom.style.top = s.y + "px";
            n._dom.style.width = s.width + "px";
            n._dom.style.height = s.height + "px";
            n._refreshAttached();
            this.resizing = null;
            this.resizeSides = null;
            this.requestDraw();
            return true;
          }
          if (this.edgeDraft) {
            this._cancelEdgeDraft();
            return true;
          }
          if (this.selectedNodes.length || this.selectedEdge) {
            this.selectedNodes = [];
            this.selectedEdge = null;
            this._clickedEdge = null;
            for (const ed of this._edges) { ed.selected = false; ed.hovered = false; }
            // Selection + hover both clean themselves up on the next draw
            // via _updateInteractionStyles.
            this._maybeEmitSelectionChange();
            this.requestDraw();
            return true;
          }
          return false;
        }

        _doDelete() {
          return this._deleteSelection();
        }

        // Internal arrow dispatch shared by move / nudge actions. For
        // move actions (forceMove=false) the same key drives either focus
        // navigation or selection movement depending on _arrowMode(); for
        // nudge actions (forceMove=true) we always move selected nodes by
        // 1px and never shift focus.
        _dispatchArrow(direction, step, forceMove) {
          if (forceMove) {
            // Nudge only matters with a selection + dragNode permission.
            if (!this.selectedNodes.length || !this._can('dragNode')) return false;
            this._applyArrowMove(direction, step);
            return true;
          }
          const mode = this._arrowMode();
          if (mode === "move" && this._can('dragNode')) {
            this._applyArrowMove(direction, step);
            return true;
          }
          if (mode === "navigate" && this._canvasHasKeyboardFocus()) {
            // Always preventDefault in nav mode (the dispatcher does this
            // by virtue of our truthy return) - otherwise pressing an
            // arrow at the edge of the graph scrolls the host page, which
            // feels broken even when the move is a no-op. Focus gate
            // keeps host pages that bind their own arrow handlers from
            // being hijacked when focus is outside the canvas.
            this._focusNeighbor(direction);
            return true;
          }
          return false;
        }

        _applyArrowMove(direction, step) {
          let dx = 0, dy = 0;
          if (direction === "left")  dx = -step;
          if (direction === "right") dx =  step;
          if (direction === "up")    dy = -step;
          if (direction === "down")  dy =  step;
          const moved = new Set();
          for (const n of this.selectedNodes) {
            if (moved.has(n)) continue;
            n._moveBy(dx, dy);
            moved.add(n);
          }
          this.requestDraw();
        }

        _canvasHasKeyboardFocus() {
          // Test outer_container (the keydown-listener scope and the
          // tabindex=0 tab stop), so this gate agrees with where events are
          // actually delivered. contains() covers focus on any node or the
          // help dialog, which live inside outer_container.
          return !!(this.outer_container && (
            this.outer_container === document.activeElement ||
            this.outer_container.contains(document.activeElement)
          ));
        }

        _deleteSelection() {
          if (!this._can('deleteNode')) return false;
          let changed = false;
          if (this.selectedNodes.length) {
            // beforeNodeDelete is per-node cancellable; nodes whose listener
            // returned false stay put. Incident edges of confirmed-deletes go
            // with them (no separate veto - they're a side-effect).
            const doomedNodes = this.selectedNodes.slice().filter(n => this._emitCancellable('beforeNodeDelete', n));
            if (doomedNodes.length) {
              const doomedIds = new Set(doomedNodes.map(n => n.id));
              const removedEdges = this._edges.filter(ed => doomedIds.has(ed.fromNode) || doomedIds.has(ed.toNode));
              this._edges = this._edges.filter(ed => !removedEdges.includes(ed));
              for (const n of doomedNodes) this._removeNodeDom(n);
              this._nodes = this._nodes.filter(n => !doomedIds.has(n.id));
              this.selectedNodes = this.selectedNodes.filter(n => !doomedIds.has(n.id));
              for (const ed of removedEdges) this._emit('edgeDelete', ed);
              for (const n of doomedNodes) this._emit('nodeDelete', n);
              changed = true;
            }
          }
          const edgeTarget = this._clickedEdge;
          if (edgeTarget && this._emitCancellable('beforeEdgeDelete', edgeTarget)) {
            this._edges = this._edges.filter(ed => ed !== edgeTarget);
            this.selectedEdge = null;
            this._clickedEdge = null;
            this._emit('edgeDelete', edgeTarget);
            changed = true;
          }
          if (changed) {
            this._maybeEmitSelectionChange();
            this.requestDraw();
            this._markDirty();
          }
          return changed;
        }


        ///////////////////////////////////////////////////////////////////////////////
        /////////////// DRAW /////////////////////////////////////////////////////////
        /////////////////////////////////////////////////////////////////////////////
        requestDraw() {
          if (this._rafId) return;
          this._rafId = requestAnimationFrame(() => {
            this._rafId = 0;
            this.draw();
          });
        }

        // Size the <canvas> backing store in *device* pixels (CSS px * dpr) while
        // keeping its CSS box in CSS pixels, so canvas-drawn content (edges, grid,
        // guides) is crisp on HiDPI/Retina displays. The DPR factor lives entirely
        // between the CSS box and the backing store; draw() prepends it to the
        // transform. Hit-testing is world-space off CSS-pixel pointer coords and is
        // unaffected. Returns true if the backing store actually changed (so the
        // resize/DPR paths know whether a redraw is warranted), false otherwise.
        _sizeCanvas() {
          if (!this.outer_container || !this.canvas) return false;
          const vp = this.outer_container.getBoundingClientRect();
          if (vp.width === 0 || vp.height === 0) return false;
          const dpr = window.devicePixelRatio || 1;
          const bw = Math.round(vp.width * dpr);
          const bh = Math.round(vp.height * dpr);
          if (this.canvas.width === bw && this.canvas.height === bh && this._dpr === dpr) {
            return false;
          }
          this.canvas.width = bw;
          this.canvas.height = bh;
          this.canvas.style.width = vp.width + "px";
          this.canvas.style.height = vp.height + "px";
          this.canvas.style.left = "0px";
          this.canvas.style.top = "0px";
          this._dpr = dpr;
          return true;
        }

        // Re-size + redraw when window.devicePixelRatio changes (e.g. dragging the
        // window to a monitor with a different scale factor). A resolution media
        // query fires once when crossing the queried ratio, so we re-arm at the new
        // ratio each time. Refs are stored for destroy() teardown.
        _watchDpr() {
          if (typeof window.matchMedia !== "function") return;
          this._teardownDprWatch();
          const dpr = window.devicePixelRatio || 1;
          const mql = window.matchMedia(`(resolution: ${dpr}dppx)`);
          const handler = () => {
            if (this._sizeCanvas()) this.draw();
            this._watchDpr();
          };
          if (mql.addEventListener) mql.addEventListener("change", handler, { once: true });
          else if (mql.addListener) mql.addListener(handler);
          this._dprMql = mql;
          this._dprHandler = handler;
        }

        _teardownDprWatch() {
          if (!this._dprMql || !this._dprHandler) return;
          if (this._dprMql.removeEventListener) this._dprMql.removeEventListener("change", this._dprHandler);
          else if (this._dprMql.removeListener) this._dprMql.removeListener(this._dprHandler);
          this._dprMql = null;
          this._dprHandler = null;
        }

        _beginFastDraw() { this._fastDrawCount++; }
        _endFastDraw()   { if (this._fastDrawCount > 0) this._fastDrawCount--; }

        // Re-read theme variables from the root element and request a redraw.
        // Call this from an embedding host (e.g. tzara) after mutating
        // --tc-* custom properties programmatically, since those mutations
        // don't fire the prefers-color-scheme matchMedia listener.
        //
        // Re-derives ALL preset-colored nodes/edges (keys "1".."6" and
        // "default") so palette mutations propagate without per-element
        // setColor calls. Explicit hex-colored elements aren't touched -
        // their colors come from a deterministic HSV transform in
        // canvasColor() that themes don't get to override.
        refreshTheme() {
          this._palette = this._readTheme();
          for (const n of this._nodes) if (n) this._refreshNodeColor(n);
          for (const e of this._edges) if (e) this._refreshEdgeColor(e);
          this.requestDraw();
        }

        // Resolve a color key into {bgcolor, borderColor}, layering palette
        // sources in precedence order:
        //   1. theme.palette[key]     (set via canvas.setTheme({palette}))
        //   2. CSS variables          (--tc-color-{1..6}-{bg,border},
        //                              --tc-node-bg-default, --tc-node-border-default,
        //                              --tc-edge-stroke-default)
        //   3. canvasColor() hardcoded preset (fallback)
        // `kind` distinguishes node vs. edge so edges only need the stroke
        // half of the palette (uses borderColor for stroke).
        _resolveColor(colorKey, kind) {
          const colors = canvasColor(colorKey);
          // 1) Active theme palette wins.
          const palette = this._theme && this._theme.palette;
          if (palette && palette[colorKey]) {
            const p = palette[colorKey];
            if (p.bg)     colors.bgcolor     = p.bg;
            if (p.border) colors.borderColor = p.border;
            return colors;
          }
          // 2) CSS-var fallback (only for the 1..6 + default preset keys).
          if (!this._palette) return colors;
          if (colorKey === "default") {
            if (kind === "edge") {
              if (this._palette.edgeStrokeDefault) colors.borderColor = this._palette.edgeStrokeDefault;
            } else {
              if (this._palette.nodeBgDefault)     colors.bgcolor     = this._palette.nodeBgDefault;
              if (this._palette.nodeBorderDefault) colors.borderColor = this._palette.nodeBorderDefault;
            }
          } else if (colorKey === "1" || colorKey === "2" || colorKey === "3" ||
                     colorKey === "4" || colorKey === "5" || colorKey === "6") {
            const bgKey     = "color" + colorKey + "Bg";
            const borderKey = "color" + colorKey + "Border";
            if (this._palette[bgKey])     colors.bgcolor     = this._palette[bgKey];
            if (this._palette[borderKey]) colors.borderColor = this._palette[borderKey];
          }
          // 3) Hex-string colors fall through unchanged.
          return colors;
        }

        // Re-derive and re-paint a node from its current color key - used by
        // refreshTheme() so palette mutations propagate without going through
        // setColor (which early-outs on idempotent values).
        _refreshNodeColor(n) {
          const c = this._resolveColor(n.color, "node");
          n.backgroundColor = c.bgcolor;
          n.borderColor     = c.borderColor;
          if (!n._dom) return;
          n._dom.style.backgroundColor = (n.type === "group")
            ? n.backgroundColor + "66"
            : n.backgroundColor;
          n._dom.style.borderColor = n.borderColor;
          if (n.group_label) {
            n.group_label.style.backgroundColor = n.backgroundColor + "66";
            n.group_label.style.borderColor     = n.borderColor;
          }
          if (n.file_label) {
            n.file_label.style.backgroundColor = n.backgroundColor + "66";
            n.file_label.style.borderColor     = n.borderColor;
          }
          if (n.link_label) {
            n.link_label.style.borderColor = n.borderColor;
          }
          // Re-layer canvas defaults + per-node style() overrides on top so
          // any non-color overrides survive the preset re-paint.
          if (typeof n._applyCanvasDefaultStyle  === "function") n._applyCanvasDefaultStyle();
          if (typeof n._applyStyleOverrides      === "function") n._applyStyleOverrides();
          if (typeof n._applyLabelStyleOverrides === "function") n._applyLabelStyleOverrides();
        }

        _refreshEdgeColor(e) {
          const c = this._resolveColor(e.color, "edge");
          e.backgroundColor = c.bgcolor;
          e.borderColor     = c.borderColor;
        }

        _promoteToDrag() {
          const p = this._dragPending;
          if (!p) return;
          this._dragPending = null;
          const node = p.node;
          this.dragging = true;
          this.dragStartX = p.downX;
          this.dragStartY = p.downY;
          this.dragOriginWorldX = p.downX;
          this.dragOriginWorldY = p.downY;
          this._dragPrimary = node;
          this._dragExtraNodes = this._computeGroupDragExtras();
          for (const n of this.selectedNodes) {
            n._dragOriginX = n.x;
            n._dragOriginY = n.y;
          }
          if (this._dragExtraNodes) {
            for (const n of this._dragExtraNodes) {
              n._dragOriginX = n.x;
              n._dragOriginY = n.y;
            }
          }
          const all = [...this.selectedNodes];
          if (this._dragExtraNodes) for (const n of this._dragExtraNodes) if (!all.includes(n)) all.push(n);
          this._emit('dragStart', { primary: node, nodes: all });
        }

        // Snapshot which dragged nodes actually moved during the gesture.
        // Called from mouseup before state is reset so nodeMove/dragEnd payloads
        // can include the original positions.
        _collectMovedDragNodes() {
          const list = [];
          const seen = new Set();
          const all = [...(this.selectedNodes || []), ...(this._dragExtraNodes || [])];
          for (const n of all) {
            if (!n || seen.has(n)) continue;
            seen.add(n);
            if (n._dragOriginX != null && (n._dragOriginX !== n.x || n._dragOriginY !== n.y)) {
              list.push({ node: n, prevX: n._dragOriginX, prevY: n._dragOriginY });
            }
          }
          return list;
        }

        // Find an <a href> anchor under (clientX, clientY) inside nodeDom.
        // The hitbox overlay normally captures all events, so we briefly disable
        // its pointer-events to let elementFromPoint see the underlying DOM.
        _findAnchorAtClient(clientX, clientY, nodeDom) {
          const prev = this.hitbox_container.style.pointerEvents;
          this.hitbox_container.style.pointerEvents = "none";
          let el;
          try {
            el = document.elementFromPoint(clientX, clientY);
          } finally {
            this.hitbox_container.style.pointerEvents = prev;
          }
          if (!el || !el.closest) return null;
          const a = el.closest("a[href]");
          if (!a) return null;
          if (nodeDom && !nodeDom.contains(a)) return null;
          return a;
        }

        _bringNodeToFront(node, { force = false } = {}) {
          if (!node) return;
          // Re-appending a link node's DOM detaches its iframe, which forces
          // the embedded page to reload and the user loses their state. Only
          // perform the reorder when explicitly requested by the user.
          if (node.type === "link" && !force) return;
          const i = this._nodes.indexOf(node);
          if (i === -1) return;

          if (node.type === "group") {
            // Groups live in group_container, behind the edge canvas and
            // content nodes. Reorder within that layer only - moving to the
            // last child paints this group on top of its peers without ever
            // covering edges or content nodes.
            if (node._dom && node._dom.parentNode === this.group_container
                && node._dom !== this.group_container.lastChild) {
              this.group_container.appendChild(node._dom);
            }
            // Mirror DOM order in _nodes so hit-testing (iterates end→start)
            // prefers the visually-topmost group when perimeters overlap.
            // _nodes keeps groups before non-groups so non-group hits still
            // win at shared pixels.
            this._nodes.splice(i, 1);
            let insertAt = 0;
            for (let k = 0; k < this._nodes.length; k++) {
              if (this._nodes[k].type === "group") insertAt = k + 1;
            }
            this._nodes.splice(insertAt, 0, node);
            return;
          }

          if (i !== this._nodes.length - 1) {
            this._nodes.splice(i, 1);
            this._nodes.push(node);
          }
          // Re-appending node._dom drops scrollTop on its scroll descendants in
          // most browsers. Skip when the DOM is already last (no z-order change),
          // and snapshot/restore scrollTop when we do have to move it.
          if (node._dom && node._dom.parentNode === this.drawing_container
              && node._dom !== this.drawing_container.lastChild) {
            const scrollEl = node._scrollEl || null;
            const savedTop = scrollEl ? scrollEl.scrollTop : 0;
            this.drawing_container.appendChild(node._dom);
            if (scrollEl) scrollEl.scrollTop = savedTop;
          }
        }

        _sendNodeToBack(node, { force = false } = {}) {
          if (!node) return;
          // Same link-iframe rationale as _bringNodeToFront.
          if (node.type === "link" && !force) return;
          const i = this._nodes.indexOf(node);
          if (i === -1) return;

          if (node.type === "group") {
            if (node._dom && node._dom.parentNode === this.group_container
                && node._dom !== this.group_container.firstChild) {
              this.group_container.insertBefore(node._dom, this.group_container.firstChild);
            }
            this._nodes.splice(i, 1);
            this._nodes.unshift(node);
            return;
          }

          if (i !== 0) {
            this._nodes.splice(i, 1);
            this._nodes.unshift(node);
          }
          if (node._dom && node._dom.parentNode === this.drawing_container
              && node._dom !== this.drawing_container.firstChild) {
            const scrollEl = node._scrollEl || null;
            const savedTop = scrollEl ? scrollEl.scrollTop : 0;
            this.drawing_container.insertBefore(node._dom, this.drawing_container.firstChild);
            if (scrollEl) scrollEl.scrollTop = savedTop;
          }
        }

        // Combined hover + selection updater. Runs from draw(). Done in
        // one pass so the two layers compose cleanly:
        //   1. Clear every node's prev hover and prev selection keys,
        //      then call _applyStyleOverrides() to put the theme's
        //      node-level CSS back on those keys. Without this, clearing
        //      an inline boxShadow we wrote would leave the node stripped
        //      of the theme's own boxShadow until the next theme apply.
        //   2. Apply hover style on _hoveredNode (skipped if that node is
        //      also selected - selection wins the visual). Mid-drag is
        //      also a no-op for hover (matches the pre-refactor branch
        //      that bailed when n.isDown was true).
        //   3. Apply selection style on every selected node. Selection
        //      goes last so when both layers target the same CSS key on
        //      the same node, selection wins.
        // selectedNodeDecorator() / hoveredNodeDecorator() route changes through
        // requestDraw() so this is the single point of truth for which
        // keys end up inline on a node.
        _updateInteractionStyles() {
          const selSet = new Set(this.selectedNodes);
          const hoverTarget = this._hoveredNode;

          const DEFAULT_SEL_STYLE = { outline: '2px solid var(--tc-accent)', outlineOffset: '2px' };
          const DEFAULT_HOVER_STYLE = {
            borderStyle: 'ridge',
            boxShadow: '0px 10px 10px -4px var(--tc-selection-shadow)',
          };
          const selOverride = this._selectedNodeDecorator;
          const hoverOverride = this._hoveredNodeDecorator;

          const resolve = (override, dflt, n) => {
            if (override == null) return dflt;
            if (typeof override === 'function') {
              let r;
              try { r = override(n); } catch (e) { r = null; }
              return (r && typeof r === 'object') ? r : dflt;
            }
            return override;
          };

          for (const n of this._nodes) {
            if (!n._dom) continue;
            // Phase 1: clear prev applied keys (hover + selection) and
            // restore the theme/per-node style overrides on those keys.
            const hk = n._dom._tzAppliedHoverStyleKeys;
            const sk = n._dom._tzAppliedSelectionStyleKeys;
            let cleared = false;
            if (hk && hk.size) {
              for (const k of hk) n._dom.style[k] = '';
              hk.clear();
              cleared = true;
            }
            if (sk && sk.size) {
              for (const k of sk) n._dom.style[k] = '';
              sk.clear();
              cleared = true;
            }
            if (cleared) n._applyStyleOverrides();

            // Phase 2: apply hover layer.
            const isSelected = selSet.has(n);
            if (n === hoverTarget && !isSelected && !n.isDown) {
              const resolved = resolve(hoverOverride, DEFAULT_HOVER_STYLE, n);
              const keys = n._dom._tzAppliedHoverStyleKeys
                || (n._dom._tzAppliedHoverStyleKeys = new Set());
              for (const k of Object.keys(resolved)) {
                const v = resolved[k];
                if (v == null || v === '') continue;
                n._dom.style[k] = v;
                keys.add(k);
              }
            }

            // Phase 3: apply selection layer (wins over hover on shared keys).
            if (isSelected) {
              const resolved = resolve(selOverride, DEFAULT_SEL_STYLE, n);
              const keys = n._dom._tzAppliedSelectionStyleKeys
                || (n._dom._tzAppliedSelectionStyleKeys = new Set());
              for (const k of Object.keys(resolved)) {
                const v = resolved[k];
                if (v == null || v === '') continue;
                n._dom.style[k] = v;
                keys.add(k);
              }
            }
          }
        }

        // Back-compat shims: external callers (e.g., selectedNodeDecorator())
        // and tests may reference these by name. They both route through
        // the combined updater so layering stays consistent.
        _updateSelectionStyles() { this._updateInteractionStyles(); }
        _updateHoverStyles()     { this._updateInteractionStyles(); }

        ///////////////////////////////////////////////////////////////////////
        // FLOATING TOOLBARS
        ///////////////////////////////////////////////////////////////////////

        _presetColorKeys() { return ["1","2","3","4","5","6","default"]; }

        _makeSwatch(colorKey, onPick) {
          // Swatches preview the actual rendered color; the 'default' swatch
          // therefore reflects any --tc-node-bg-default override in effect.
          const colors = this._resolveColor(colorKey, "node");
          const s = document.createElement("div");
          s.className = "tc-swatch";
          s.dataset.color = colorKey;
          s.style.background = colors.bgcolor;
          s.style.borderColor = colors.borderColor;
          s.title = colorKey === "default" ? "default" : "color " + colorKey;
          s.addEventListener("mousedown", e => e.stopPropagation());
          s.addEventListener("click", e => { e.stopPropagation(); onPick(colorKey); });
          return s;
        }

        _swallowEvents(el) {
          // Keep clicks/double-clicks on toolbar from bubbling to hitbox_container.
          for (const t of ["mousedown","mouseup","click","dblclick","wheel","contextmenu"]) {
            el.addEventListener(t, e => e.stopPropagation());
          }
        }

        _makeColorPanel(applyFn) {
          // Returns { panel, swatches, hex, syncFromColor(color) }
          const panel = document.createElement("div");
          panel.className = "tc-panel";

          const swatches = {};
          for (const k of this._presetColorKeys()) {
            const s = this._makeSwatch(k, c => applyFn(c));
            panel.appendChild(s);
            swatches[k] = s;
          }

          const hex = document.createElement("input");
          hex.type = "text";
          hex.className = "tc-hex";
          hex.placeholder = "#rrggbb";
          hex.spellcheck = false;
          hex.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Enter") hex.blur();
          });
          hex.addEventListener("change", () => {
            const v = hex.value.trim();
            if (/^#?[0-9a-fA-F]{6}$/.test(v)) {
              const normalized = v.startsWith("#") ? v : "#" + v;
              applyFn(normalized);
            }
          });
          panel.appendChild(hex);

          return { panel, swatches, hex };
        }

        _wireToolbar(tb, sections) {
          // sections: [{ key, emoji|icon|iconUrl, title, panel?, action?, className?, enabled? }]
          //   - key:       required, unique within this toolbar
          //   - emoji:     plain text (default rendering)
          //   - icon:      raw HTML/SVG string (set as innerHTML); takes precedence over emoji
          //   - iconUrl:   image URL; takes precedence over icon and emoji
          //   - panel:     optional drawer content; if present, the button toggles the drawer
          //   - action:    invoked on click when no panel; receives the click event
          //   - className: extra class on the trigger button
          //   - enabled:   false → start disabled. (Function predicates land in step 6.)
          // Drawer direction is driven by classes on the *outer* toolbar
          // (.tc-toolbar-vertical, .tc-toolbar-anchor-*), so the drawer itself
          // needs no per-orientation marker class.
          const triggerRow = document.createElement("div");
          triggerRow.className = "tc-trigger-row";

          const drawer = document.createElement("div");
          drawer.className = "tc-drawer";

          const liveSections = [];
          const triggers = {};

          const setActive = key => {
            tb._activeSection = key;
            for (const s of liveSections) {
              const btn = triggers[s.key];
              if (btn) btn.classList.toggle("active", s.key === key);
              if (s.panel) s.panel.classList.toggle("active", s.key === key);
            }
            drawer.classList.toggle("open", key != null);
          };

          // Render the button face from whichever icon-ish field the spec provides.
          // Re-used by future setIcon() on ToolbarButton, hence factored out.
          const applyIcon = (btn, spec) => {
            if (spec.iconUrl) {
              btn.textContent = "";
              const img = document.createElement("img");
              img.src = spec.iconUrl;
              img.alt = spec.title || spec.key || "";
              img.draggable = false;
              img.style.width = "18px";
              img.style.height = "18px";
              img.style.pointerEvents = "none";
              btn.appendChild(img);
            } else if (spec.icon) {
              btn.innerHTML = spec.icon;
            } else {
              btn.textContent = spec.emoji != null ? spec.emoji : "";
            }
          };

          const buildButton = s => {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "tc-trigger";
            if (s.className) b.classList.add(s.className);
            applyIcon(b, s);
            if (s.title) b.title = s.title;
            if (s.enabled === false) b.disabled = true;
            b.addEventListener("mousedown", e => e.stopPropagation());
            if (s.action) {
              b.addEventListener("click", e => {
                e.stopPropagation();
                s.action(e);
              });
            } else {
              b.addEventListener("click", e => {
                e.stopPropagation();
                setActive(tb._activeSection === s.key ? null : s.key);
              });
            }
            return b;
          };

          // Insert a section. opts: { index? | before? | after? } - index wins,
          // then before, then after; default is append. Returns the trigger
          // button so callers can hang state off it (label updates, etc.).
          const addSection = (s, opts = {}) => {
            if (!s || !s.key) throw new Error("section requires a { key }");
            if (triggers[s.key]) throw new Error(`section "${s.key}" already exists`);

            let idx = liveSections.length;
            if (typeof opts.index === "number") {
              idx = Math.max(0, Math.min(opts.index, liveSections.length));
            } else if (opts.before) {
              const i = liveSections.findIndex(x => x.key === opts.before);
              if (i >= 0) idx = i;
            } else if (opts.after) {
              const i = liveSections.findIndex(x => x.key === opts.after);
              if (i >= 0) idx = i + 1;
            }

            const btn = buildButton(s);
            const refBtn = triggerRow.children[idx] || null;
            triggerRow.insertBefore(btn, refBtn);
            if (s.panel) drawer.appendChild(s.panel);

            liveSections.splice(idx, 0, s);
            triggers[s.key] = btn;
            return btn;
          };

          const removeSection = key => {
            const i = liveSections.findIndex(s => s.key === key);
            if (i < 0) return false;
            const s = liveSections[i];
            const btn = triggers[key];
            if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
            if (s.panel && s.panel.parentNode === drawer) drawer.removeChild(s.panel);
            if (tb._activeSection === key) setActive(null);
            liveSections.splice(i, 1);
            delete triggers[key];
            return true;
          };

          for (const s of sections) addSection(s);

          tb.appendChild(triggerRow);
          tb.appendChild(drawer);
          tb._setActive = setActive;
          tb._activeSection = null;
          tb._triggers = triggers;
          tb._sections = liveSections;
          tb._triggerRow = triggerRow;
          tb._drawer = drawer;
          tb._addSection = addSection;
          tb._removeSection = removeSection;
          tb._applyIcon = applyIcon;
        }

        // Apply the constructor's `toolbars` option to the three controllers.
        // Shape: { canvas?: cfg, node?: cfg, edge?: cfg }, where cfg is
        // { hidden?, position?, orientation?, buttons?: [spec, …] }.
        // position/orientation are honored only on the canvas (global) toolbar.
        _applyToolbarOptions(opts) {
          if (!opts || typeof opts !== "object") return;
          for (const kind of ["canvas", "node", "edge"]) {
            const cfg = opts[kind];
            if (!cfg) continue;
            const ctrl = this.ui.toolbar[kind];
            if (kind === "canvas") {
              if (cfg.position)    ctrl.setPosition(cfg.position);
              if (cfg.orientation) ctrl.setOrientation(cfg.orientation);
            }
            if (Array.isArray(cfg.buttons)) {
              for (const spec of cfg.buttons) ctrl.addButton(spec);
            }
            if (cfg.hidden) ctrl.hide();
          }
        }

        _buildNodeToolbar() {
          const tb = document.createElement("div");
          tb.className = "tc-toolbar";
          this._swallowEvents(tb);

          const color = this._makeColorPanel(c => this._applyNodeColor(c));
          this._nodeSwatches = color.swatches;
          this.nodeToolbarHex = color.hex;

          // Label panel - only surfaced when a single group node is selected.
          const labelPanel = document.createElement("div");
          labelPanel.className = "tc-panel";
          const label = document.createElement("input");
          label.type = "text";
          label.className = "tc-label-input";
          label.placeholder = "label";
          label.spellcheck = false;
          label.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Enter") label.blur();
          });
          let labelOrigValue = "";
          label.addEventListener("focus", () => { labelOrigValue = label.value; });
          label.addEventListener("input", () => {
            if (this.selectedNodes.length !== 1) return;
            const n = this.selectedNodes[0];
            if (n.type !== "group") return;
            n.label = label.value;
            if (n.group_label) n.group_label.textContent = label.value;
            n._applyA11y();
          });
          label.addEventListener("blur", () => {
            if (label.value !== labelOrigValue) this._markDirty();
          });
          labelPanel.appendChild(label);
          this.nodeToolbarLabel = label;

          // URL panel - only surfaced when a single link node is selected.
          const urlPanel = document.createElement("div");
          urlPanel.className = "tc-panel";
          const urlInput = document.createElement("input");
          urlInput.type = "url";
          urlInput.className = "tc-label-input";
          urlInput.placeholder = "https://…";
          urlInput.spellcheck = false;
          const commitUrl = () => {
            if (this.selectedNodes.length !== 1) return;
            const n = this.selectedNodes[0];
            if (n.type !== "link") return;
            const next = urlInput.value.trim();
            if (next === n.url) return;
            n.setUrl(next);
            this._markDirty();
          };
          urlInput.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Enter") { commitUrl(); urlInput.blur(); }
          });
          urlInput.addEventListener("blur", commitUrl);
          urlPanel.appendChild(urlInput);
          this.nodeToolbarUrl = urlInput;

          // Background panel - only surfaced when a single group node is selected.
          const bgPanel = document.createElement("div");
          bgPanel.className = "tc-panel";

          const bgNoRow = document.createElement("div");
          bgNoRow.className = "tc-bg-row";
          const bgAddBtn = document.createElement("button");
          bgAddBtn.type = "button";
          bgAddBtn.className = "tc-btn";
          bgAddBtn.textContent = "Add background image";
          bgAddBtn.addEventListener("mousedown", e => e.stopPropagation());
          bgAddBtn.addEventListener("click", e => {
            e.stopPropagation();
            if (!this.listImageFiles) return;
            this._openImageFilePicker(
              (path) => this._setGroupBackground(path),
              this.nodeToolbar
            );
          });
          bgNoRow.appendChild(bgAddBtn);

          const bgHasRow = document.createElement("div");
          bgHasRow.className = "tc-bg-row";
          const bgReplaceBtn = document.createElement("button");
          bgReplaceBtn.type = "button";
          bgReplaceBtn.className = "tc-btn";
          bgReplaceBtn.textContent = "Replace";
          bgReplaceBtn.addEventListener("mousedown", e => e.stopPropagation());
          bgReplaceBtn.addEventListener("click", e => {
            e.stopPropagation();
            if (!this.listImageFiles) return;
            this._openImageFilePicker(
              (path) => this._setGroupBackground(path),
              this.nodeToolbar
            );
          });
          const bgRemoveBtn = document.createElement("button");
          bgRemoveBtn.type = "button";
          bgRemoveBtn.className = "tc-btn";
          bgRemoveBtn.title = "Remove background";
          bgRemoveBtn.textContent = "✖";
          bgRemoveBtn.addEventListener("mousedown", e => e.stopPropagation());
          bgRemoveBtn.addEventListener("click", e => {
            e.stopPropagation();
            this._clearGroupBackground();
          });
          const bgSep = document.createElement("div");
          bgSep.className = "tc-sep";
          const bgStyleBtns = {};
          for (const [styleKey, label] of [["cover", "Cover"], ["ratio", "Ratio"], ["repeat", "Repeat"]]) {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "tc-btn tc-bg-style-btn";
            b.dataset.style = styleKey;
            b.textContent = label;
            b.addEventListener("mousedown", e => e.stopPropagation());
            b.addEventListener("click", e => {
              e.stopPropagation();
              this._setGroupBackgroundStyle(styleKey);
            });
            bgHasRow.appendChild(b);
            bgStyleBtns[styleKey] = b;
          }
          bgHasRow.insertBefore(bgSep, bgStyleBtns.cover);
          bgHasRow.insertBefore(bgRemoveBtn, bgSep);
          bgHasRow.insertBefore(bgReplaceBtn, bgRemoveBtn);

          bgPanel.appendChild(bgNoRow);
          bgPanel.appendChild(bgHasRow);
          this._bgToolbar = {
            panel: bgPanel,
            noRow: bgNoRow,
            hasRow: bgHasRow,
            addBtn: bgAddBtn,
            replaceBtn: bgReplaceBtn,
            removeBtn: bgRemoveBtn,
            styleBtns: bgStyleBtns,
          };

          this._wireToolbar(tb, [
            { key: "edit", emoji: "✏️", title: "Edit", action: () => {
                if (this.selectedNodes.length === 1 && this.selectedNodes[0].type === "text") {
                  this._enterEdit(this.selectedNodes[0]);
                }
            }},
            // 👩‍👧‍👦🎶📁
            { key: "color", emoji: "🎨", title: "Color", panel: color.panel },
            { key: "label", emoji: "📝", title: "Label", panel: labelPanel  },
            { key: "url",   emoji: "🔗", title: "Edit URL", panel: urlPanel },
            { key: "openLink", emoji: "🌐", title: "Open link in new window", action: () => {
                if (this.selectedNodes.length === 1
                    && this.selectedNodes[0].type === "link"
                    && this.selectedNodes[0].url) {
                  window.open(this.selectedNodes[0].url, "_blank", "noopener");
                }
            }},
            { key: "bringToFront", emoji: "🔝", title: "Bring to front (reloads iframe)", action: () => {
                if (this.selectedNodes.length === 1
                    && this.selectedNodes[0].type === "link") {
                  this._bringNodeToFront(this.selectedNodes[0], { force: true });
                  this.requestDraw();
                }
            }},
            { key: "group", emoji: "👩‍👧‍👦", title: "Create Group", action: () => this._createGroupFromSelection() },
            { key: "background", emoji: "🖼️", title: "Background", panel: bgPanel },
            { key: "delete", emoji: "🗑️", title: "Delete", className: "tc-trigger-delete", action: () => this._deleteSelection() },
          ]);

          this.toolbar_container.appendChild(tb);
          this.nodeToolbar = tb;
        }

        _buildEdgeToolbar() {
          const tb = document.createElement("div");
          tb.className = "tc-toolbar";
          this._swallowEvents(tb);

          // Color panel
          const color = this._makeColorPanel(c => this._applyEdgeColor(c));
          this._edgeSwatches = color.swatches;
          this.edgeToolbarHex = color.hex;

          // Label panel
          const labelPanel = document.createElement("div");
          labelPanel.className = "tc-panel";
          const label = document.createElement("input");
          label.type = "text";
          label.className = "tc-label-input";
          label.placeholder = "label";
          label.spellcheck = false;
          let edgeLabelOrigValue = "";
          label.addEventListener("focus", () => { edgeLabelOrigValue = label.value; });
          label.addEventListener("keydown", e => {
            e.stopPropagation();
            if (e.key === "Enter") label.blur();
          });
          label.addEventListener("input", () => {
            if (this._clickedEdge) {
              this._clickedEdge.edge_label = label.value;
              this.requestDraw();
            }
          });
          label.addEventListener("blur", () => {
            if (label.value !== edgeLabelOrigValue) this._markDirty();
          });
          labelPanel.appendChild(label);
          this.edgeToolbarLabel = label;

          // Arrow panel
          const arrowPanel = document.createElement("div");
          arrowPanel.className = "tc-panel";
          const arrowDefs = [
            { key: "none", text: "-",  title: "no arrow",       fromEnd: "none",  toEnd: "none"  },
            { key: "uni",  text: "→", title: "unidirectional", fromEnd: "none",  toEnd: "arrow" },
            { key: "bi",   text: "↔", title: "bidirectional",  fromEnd: "arrow", toEnd: "arrow" },
          ];
          this._arrowButtons = {};
          for (const def of arrowDefs) {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "tc-btn";
            b.textContent = def.text;
            b.title = def.title;
            b.addEventListener("mousedown", e => e.stopPropagation());
            b.addEventListener("click", e => {
              e.stopPropagation();
              const ed = this._clickedEdge;
              if (!ed) return;
              const changed = ed.fromEnd !== def.fromEnd || ed.toEnd !== def.toEnd;
              ed.fromEnd = def.fromEnd;
              ed.toEnd   = def.toEnd;
              this._syncEdgeToolbarState();
              this.requestDraw();
              if (changed) this._markDirty();
            });
            arrowPanel.appendChild(b);
            this._arrowButtons[def.key] = b;
          }
          const flip = document.createElement("button");
          flip.type = "button";
          flip.className = "tc-btn";
          flip.textContent = "⇄";
          flip.title = "flip direction";
          flip.addEventListener("mousedown", e => e.stopPropagation());
          flip.addEventListener("click", e => {
            e.stopPropagation();
            const ed = this._clickedEdge;
            if (!ed) return;
            // Only swap the arrow ends. Swapping fromNode/toNode as well
            // would also reverse the bezier's traversal direction, putting
            // the arrow back in the same physical location - a visual no-op.
            const changed = ed.fromEnd !== ed.toEnd;
            [ed.fromEnd, ed.toEnd] = [ed.toEnd, ed.fromEnd];
            this.requestDraw();
            if (changed) this._markDirty();
          });
          arrowPanel.appendChild(flip);
          this._arrowFlipBtn = flip;

          this._wireToolbar(tb, [
            { key: "color",  emoji: "🎨", title: "color",          panel: color.panel },
            { key: "label",  emoji: "📝", title: "label",          panel: labelPanel  },
            { key: "arrow",  emoji: "➡️", title: "direction",      panel: arrowPanel  },
            { key: "delete", emoji: "🗑️", title: "delete selection", className: "tc-trigger-delete", action: () => this._deleteSelection() },
          ]);

          this.toolbar_container.appendChild(tb);
          this.edgeToolbar = tb;
        }

        _applyNodeColor(color) {
          if (!this.selectedNodes.length) return;
          let anyChanged = false;
          for (const n of this.selectedNodes) {
            const colors = this._resolveColor(color, "node");
            if (n.color !== color) anyChanged = true;
            n.color = color;
            n.backgroundColor = colors.bgcolor;
            n.borderColor = colors.borderColor;
            n._dom.style.borderColor = colors.borderColor;
            if (n.type === "group") {
              n._dom.style.backgroundColor = colors.bgcolor + "66";
              if (n.group_label) {
                n.group_label.style.borderColor = colors.borderColor;
                n.group_label.style.backgroundColor = colors.bgcolor + "66";
              }
            } else {
              n._dom.style.backgroundColor = colors.bgcolor;
            }
            if (n.file_label) {
              n.file_label.style.borderColor = colors.borderColor;
              n.file_label.style.backgroundColor = colors.bgcolor + "66";
            }
            if (n.link_label) {
              n.link_label.style.borderColor = colors.borderColor;
              n.link_label.style.backgroundColor = colors.bgcolor + "66";
            }
            // Re-layer: canvas defaults -> per-node style() -> per-node label
            // style(). Without this, picking a preset while a theme override
            // exists would *briefly* show the preset, then revert on the next
            // hover repaint (which re-applies _styleOverrides). The override
            // is supposed to win - that's the documented layering - so the
            // correct behavior is "no visible change, but n.color does
            // change underneath."
            if (typeof n._applyCanvasDefaultStyle  === "function") n._applyCanvasDefaultStyle();
            if (typeof n._applyStyleOverrides      === "function") n._applyStyleOverrides();
            if (typeof n._applyLabelStyleOverrides === "function") n._applyLabelStyleOverrides();
          }
          this._syncNodeToolbarState();
          this.requestDraw();
          if (anyChanged) this._markDirty();
        }

        _applyEdgeColor(color) {
          const e = this._clickedEdge;
          if (!e) return;
          const colors = this._resolveColor(color, "edge");
          const changed = e.color !== color;
          e.color = color;
          e.backgroundColor = colors.bgcolor;
          e.borderColor = colors.borderColor;
          this._syncEdgeToolbarState();
          this.requestDraw();
          if (changed) this._markDirty();
        }

        _syncNodeToolbarState() {
          const first = this.selectedNodes[0];
          if (!first) return;
          const color = first.color ?? "default";
          const allSame = this.selectedNodes.every(n => (n.color ?? "default") === color);
          for (const k of this._presetColorKeys()) {
            this._nodeSwatches[k].classList.toggle("selected", allSame && k === color);
          }
          if (document.activeElement !== this.nodeToolbarHex) {
            this.nodeToolbarHex.value = allSame && color && color.startsWith && color.startsWith("#") ? color : "";
          }

          const onlyGroup = this.selectedNodes.length === 1 && first.type === "group";
          const labelTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.label;
          if (labelTrigger) {
            labelTrigger.style.display = onlyGroup ? "" : "none";
          }
          const bgTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.background;
          if (bgTrigger) {
            bgTrigger.style.display = onlyGroup ? "" : "none";
          }
          if (onlyGroup) {
            this._syncBgToolbarState();
          } else if (this.nodeToolbar._activeSection === "background") {
            this.nodeToolbar._setActive(null);
          }
          const groupTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.group;
          if (groupTrigger) {
            groupTrigger.style.display = this.selectedNodes.length >= 2 ? "" : "none";
          }
          const onlyText = this.selectedNodes.length === 1 && first.type === "text";
          const editTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.edit;
          if (editTrigger) {
            editTrigger.style.display = (onlyText && this._can('editText')) ? "" : "none";
          }
          const deleteTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.delete;
          if (deleteTrigger) {
            deleteTrigger.style.display = this._can('deleteNode') ? "" : "none";
          }
          const onlyLinkWithUrl = this.selectedNodes.length === 1
            && first.type === "link"
            && !!first.url;
          const openLinkTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.openLink;
          if (openLinkTrigger) {
            openLinkTrigger.style.display = onlyLinkWithUrl ? "" : "none";
          }
          const bringToFrontTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.bringToFront;
          if (bringToFrontTrigger) {
            const onlyLinkSel = this.selectedNodes.length === 1 && first.type === "link";
            bringToFrontTrigger.style.display = onlyLinkSel ? "" : "none";
          }
          if (onlyGroup) {
            if (document.activeElement !== this.nodeToolbarLabel) {
              this.nodeToolbarLabel.value = first.label ?? "";
            }
          } else if (this.nodeToolbar._activeSection === "label") {
            this.nodeToolbar._setActive(null);
          }

          const onlyLink = this.selectedNodes.length === 1 && first.type === "link";
          const urlTrigger = this.nodeToolbar._triggers && this.nodeToolbar._triggers.url;
          if (urlTrigger) {
            urlTrigger.style.display = onlyLink ? "" : "none";
          }
          if (onlyLink) {
            if (document.activeElement !== this.nodeToolbarUrl) {
              this.nodeToolbarUrl.value = first.url ?? "";
            }
          } else if (this.nodeToolbar._activeSection === "url") {
            this.nodeToolbar._setActive(null);
          }

          // Custom buttons with a function `enabled` predicate ride the same
          // sync cycle as the built-ins.
          if (this.ui) this.ui.toolbar.node.refresh();
        }

        _syncBgToolbarState() {
          const bg = this._bgToolbar;
          if (!bg) return;
          const n = this.selectedNodes[0];
          if (!n || n.type !== "group") return;
          const hasBg = !!n.background;
          bg.noRow.style.display = hasBg ? "none" : "flex";
          bg.hasRow.style.display = hasBg ? "flex" : "none";
          const canPick = !!this.listImageFiles;
          const tip = canPick ? "" : "listImageFiles callback not provided";
          bg.addBtn.disabled = !canPick;
          bg.addBtn.title = tip;
          bg.replaceBtn.disabled = !canPick;
          bg.replaceBtn.title = tip;
          const style = n.backgroundStyle || "cover";
          for (const k of Object.keys(bg.styleBtns)) {
            bg.styleBtns[k].classList.toggle("active", k === style);
          }
        }

        _setGroupBackground(path) {
          const n = this.selectedNodes[0];
          if (!n || n.type !== "group") return;
          const changed = n.background !== path;
          n.background = path;
          if (!n.backgroundStyle) n.backgroundStyle = "cover";
          n._applyGroupBackground();
          this._syncNodeToolbarState();
          this.requestDraw();
          if (changed) this._markDirty();
        }

        _clearGroupBackground() {
          const n = this.selectedNodes[0];
          if (!n || n.type !== "group") return;
          const changed = n.background != null;
          n.background = null;
          n._backgroundImageAspect = null;
          n._applyGroupBackground();
          this._syncNodeToolbarState();
          this.requestDraw();
          if (changed) this._markDirty();
        }

        _setGroupBackgroundStyle(style) {
          if (!["cover", "ratio", "repeat"].includes(style)) return;
          const n = this.selectedNodes[0];
          if (!n || n.type !== "group") return;
          const changed = n.backgroundStyle !== style;
          n.backgroundStyle = style;
          n._applyGroupBackground();
          this._syncNodeToolbarState();
          this.requestDraw();
          if (changed) this._markDirty();
        }

        _syncEdgeToolbarState() {
          const e = this._clickedEdge;
          if (!e) return;
          const color = e.color ?? "default";
          for (const k of this._presetColorKeys()) {
            this._edgeSwatches[k].classList.toggle("selected", k === color);
          }
          if (document.activeElement !== this.edgeToolbarHex) {
            this.edgeToolbarHex.value = color && color.startsWith && color.startsWith("#") ? color : "";
          }
          if (document.activeElement !== this.edgeToolbarLabel) {
            this.edgeToolbarLabel.value = e.edge_label ?? "";
          }
          const isBi   = e.fromEnd === "arrow" && e.toEnd === "arrow";
          const isUni  = !isBi && (e.fromEnd === "arrow" || e.toEnd === "arrow");
          const isNone = !isBi && !isUni;
          this._arrowButtons.none.classList.toggle("active", isNone);
          this._arrowButtons.uni.classList.toggle("active",  isUni);
          this._arrowButtons.bi.classList.toggle("active",   isBi);
          this._arrowFlipBtn.style.display = isUni ? "" : "none";

          const edgeDeleteTrigger = this.edgeToolbar._triggers && this.edgeToolbar._triggers.delete;
          if (edgeDeleteTrigger) {
            edgeDeleteTrigger.style.display = this._can('deleteNode') ? "" : "none";
          }

          if (this.ui) this.ui.toolbar.edge.refresh();
        }

        _updateToolbars() {
          if (this.permissions.isReadOnly()) {
            this.nodeToolbar.style.display = "none";
            this.edgeToolbar.style.display = "none";
            return;
          }
          // During node drag/resize, hide both toolbars - they're overlay DOM
          // in a high-z container and intercept hit tests when the cursor
          // overshoots the moving node at high speed.
          if (this.dragging || this.resizing) {
            this.nodeToolbar.style.display = "none";
            this.edgeToolbar.style.display = "none";
            return;
          }

          // Collapse drawers when the selection target changes, so a new
          // selection starts with just the emoji triggers visible.
          const nodeKey = this.selectedNodes.map(n => n.id).join(",");
          if (this._lastNodeToolbarKey !== nodeKey) {
            this._lastNodeToolbarKey = nodeKey;
            if (this.nodeToolbar._setActive) this.nodeToolbar._setActive(null);
          }
          if (this._lastEdgeToolbarTarget !== this._clickedEdge) {
            this._lastEdgeToolbarTarget = this._clickedEdge;
            if (this.edgeToolbar._setActive) this.edgeToolbar._setActive(null);
          }

          // Node toolbar
          if (this.selectedNodes.length === 0) {
            this.nodeToolbar.style.display = "none";
          } else {
            const bbox = getBoundingBox(this.selectedNodes);
            const screenLeft = bbox.left * this.scale + this.panX;
            const screenTop  = bbox.top  * this.scale + this.panY;
            const screenW    = bbox.width * this.scale;
            this.nodeToolbar.style.display = "flex";
            const tbW = this.nodeToolbar.offsetWidth;
            const tbH = this.nodeToolbar.offsetHeight;
            let x = screenLeft + screenW / 2 - tbW / 2;
            let y = screenTop - tbH - 8;
            const containerW = this.container.clientWidth;
            if (x < 4) x = 4;
            if (x + tbW > containerW - 4) x = containerW - tbW - 4;
            // fall below if no room above, but what about sides?
            if (y < 4) y = screenTop + bbox.height * this.scale + 8; 
            this.nodeToolbar.style.left = x + "px";
            this.nodeToolbar.style.top  = y + "px";
            this._syncNodeToolbarState();
          }

          // Edge toolbar - only show on actual click selection, not hover.
          // (event_mousemove also writes to selectedEdge for hover highlight,
          // so we gate on a separate _clickedEdge set only by mousedown.)
          const e = this._clickedEdge;
          if (!e || e._labelX == null) {
            this.edgeToolbar.style.display = "none";
          } else {
            const sx = e._labelX * this.scale + this.panX;
            const sy = e._labelY * this.scale + this.panY;
            this.edgeToolbar.style.display = "flex";
            const tbW = this.edgeToolbar.offsetWidth;
            const tbH = this.edgeToolbar.offsetHeight;
            let x = sx - tbW / 2;
            let y = sy - tbH - 8;
            const containerW = this.container.clientWidth;
            if (x < 4) x = 4;
            if (x + tbW > containerW - 4) x = containerW - tbW - 4;
            if (y < 4) y = sy + 12;
            this.edgeToolbar.style.left = x + "px";
            this.edgeToolbar.style.top  = y + "px";
            this._syncEdgeToolbarState();
          }
        }

        draw() {
          this._updateInteractionStyles();
          // World -> device pixels: (world * scale + pan) * dpr. The backing store
          // is sized in device px by _sizeCanvas(), so the DPR factor is folded into
          // the transform here. Authored stroke widths are <n>/scale (world units),
          // which the scale*dpr factor renders at the intended CSS-pixel size.
          const dpr = this._dpr || window.devicePixelRatio || 1;
          this.ctx.setTransform(this.scale * dpr, 0, 0, this.scale * dpr, this.panX * dpr, this.panY * dpr);

          // Clear in world coords; backing store is device px, so divide by scale*dpr.
          var clearx = -Math.floor(this.panX / this.scale);
          var cleary = -Math.floor(this.panY / this.scale);
          var clearw = this.canvas.width / (this.scale * dpr);
          var clearh = this.canvas.height / (this.scale * dpr);
          this.ctx.clearRect(clearx, cleary, clearw, clearh);

          if (this.showGrid && this.showGrid !== 'off') {
            this._drawGrid(clearx, cleary, clearw, clearh);
          }

          // Marquee is now a DOM div in marquee_container (a layer between
          // background_container and group_container), so it visually sits
          // *below* group bodies. _updateMarqueeDom positions/styles it
          // (or hides it when marqueeActive is false).
          this._updateMarqueeDom();

          this._edges.forEach(e => {
            if (e._hiddenForReattach) return;
            e.drawSmartBezier();
          });

          if (this.edgeDraft) {
            this._drawEdgePreview();
          }

          if (this._activeGuides && this._activeGuides.length) {
            const ctx = this.ctx;
            ctx.save();
            ctx.strokeStyle = this._palette.guideStroke;
            ctx.lineWidth = 2 / this.scale;
            const dash = 4 / this.scale;
            ctx.setLineDash([dash, dash]);
            for (const g of this._activeGuides) {
              ctx.beginPath();
              if (g.axis === "v") {
                ctx.moveTo(g.x, g.y1);
                ctx.lineTo(g.x, g.y2);
              } else {
                ctx.moveTo(g.x1, g.y);
                ctx.lineTo(g.x2, g.y);
              }
              ctx.stroke();
            }
            ctx.restore();
          }

          this._updateToolbars();





        }

        /////////////////////////////////////////////////////////
        get cursor() {
          return this._cursor;
        }
        set cursor(cursor) {
          this._cursor = cursor;
          this.outer_container.style.cursor = cursor;
        }

        /////////////////////////////////////////////////////////
        // Returns a fresh serialized {nodes, edges} snapshot of current
        // state (same as io.toData()) - NOT a live mutable object. Mutating
        // the result has no effect on the canvas; assign back through the
        // setter (a hard reset) to apply changes.
        get data() {
          return this.io.toData();
        }
        // The report from the most recent load (constructor or `data =`): what
        // the normalization gate changed - { droppedEdges, duplicateNodeIds,
        // duplicateEdgeIds, generatedNodeIds, generatedEdgeIds, repairedGeometry }.
        // The 'load' event carries the same object for later reloads; this getter
        // is how a host reads the initial constructor load (no listener yet then).
        get lastLoadReport() {
          return this._lastLoadReport;
        }
        set data(canvas_data) {
          this.clearCanvas();
          this.createNodesAndEdges(canvas_data);
          this._updateWrapperAriaLabel();
          // clearCanvas removed every node DOM, dropping focusedNode
          // along with them. Restore an initial Tab target on the new
          // first node so keyboard nav still works after a data reset.
          if (this._nodes.length > 0 && !this.focusedNode) {
            this.setFocusedNode(this._nodes[0], { focus: false });
          }

          if (this._nodes.length === 0) return;

          var node_bounding_box = getBoundingBox(this._nodes);

          var centerX = (node_bounding_box.right + node_bounding_box.left) / 2;
          var centerY = (node_bounding_box.top + node_bounding_box.bottom) / 2;

          this._nodes.forEach(n => {
            n._moveBy(-centerX + this.canvas.width/2, -centerY + this.canvas.height/2);
          });
        }

        resetCanvas() {
            this.data = this.raw_data;
        }

        // Walk the node/edge graph and surface integrity problems. Returns an
        // array of { kind, message, ...refs } objects. Always read-only.
        validate() {
          const issues = [];
          const nodeIds = new Set();
          for (const n of this._nodes) {
            if (!n.id) issues.push({ kind: 'nodeMissingId' });
            else if (nodeIds.has(n.id)) issues.push({ kind: 'duplicateNodeId', id: n.id });
            else nodeIds.add(n.id);
            // Same finiteness rule the load gate repairs with (finiteOr).
            if (finiteOr(n.x, null) === null || finiteOr(n.y, null) === null
                || finiteOr(n.width, null) === null || finiteOr(n.height, null) === null) {
              issues.push({ kind: 'nodeBadGeometry', id: n.id });
            }
          }
          const edgeIds = new Set();
          const edgeKeys = new Set();
          for (const e of this._edges) {
            if (!e.id) issues.push({ kind: 'edgeMissingId' });
            else if (edgeIds.has(e.id)) issues.push({ kind: 'duplicateEdgeId', id: e.id });
            else edgeIds.add(e.id);
            if (!nodeIds.has(e.fromNode)) issues.push({ kind: 'orphanEdge', id: e.id, missing: e.fromNode });
            if (!nodeIds.has(e.toNode))   issues.push({ kind: 'orphanEdge', id: e.id, missing: e.toNode });
            const key = edgeKey(e);
            if (edgeKeys.has(key)) issues.push({ kind: 'duplicateEdge', id: e.id });
            else edgeKeys.add(key);
          }
          return issues;
        }

        // Best-effort repair of live state - the user-invoked counterpart to the
        // load-time gate, applying the same shared rules (edgeKey, finiteOr).
        // opts: removeOrphanEdges (default true), dedupeEdges (default false -
        // opt-in since which duplicate to keep is opinionated), repairGeometry
        // (default false - coerce non-finite node geometry to defaults like the
        // gate). Returns the count of changes per category.
        cleanup(opts = {}) {
          const removeOrphan = opts.removeOrphanEdges !== false;
          const dedupe       = opts.dedupeEdges === true;
          const repairGeom   = opts.repairGeometry === true;
          const result = { orphansRemoved: 0, duplicatesRemoved: 0, geometryRepaired: 0 };
          this.batch(() => {
            if (repairGeom) {
              for (const n of this._nodes) {
                const x = finiteOr(n.x, DEFAULT_NODE_GEOMETRY.x);
                const y = finiteOr(n.y, DEFAULT_NODE_GEOMETRY.y);
                let w = finiteOr(n.width,  DEFAULT_NODE_GEOMETRY.width);
                let h = finiteOr(n.height, DEFAULT_NODE_GEOMETRY.height);
                if (w <= 0) w = DEFAULT_NODE_GEOMETRY.width;
                if (h <= 0) h = DEFAULT_NODE_GEOMETRY.height;
                w = Math.min(w, MAX_NODE_EXTENT);
                h = Math.min(h, MAX_NODE_EXTENT);
                if (x !== n.x || y !== n.y || w !== n.width || h !== n.height) {
                  n._positionAt(x, y);
                  n._sizeAt(w, h);
                  result.geometryRepaired++;
                }
              }
              if (result.geometryRepaired) this._markDirty();
            }
            if (removeOrphan) {
              const orphans = this._edges.filter(e => !this.getNode(e.fromNode) || !this.getNode(e.toNode));
              for (const e of orphans) {
                if (this.deleteEdge(e)) result.orphansRemoved++;
              }
            }
            if (dedupe) {
              const seen = new Set();
              const dupes = [];
              for (const e of this._edges) {
                const key = edgeKey(e);
                if (seen.has(key)) dupes.push(e);
                else seen.add(key);
              }
              for (const e of dupes) {
                if (this.deleteEdge(e)) result.duplicatesRemoved++;
              }
            }
          });
          return result;
        }

        clearCanvas() {
          // Flush any open edit / edge-draft / drag before tearing node DOM out
          // from under it (would orphan this.editing on a removed node).
          this._abortInteraction();
          this._nodes.forEach(node => this._removeNodeDom(node));
          // edge <li>s aren't owned by node DOM, so wipe them
          // explicitly. _removeNodeDom already disposed each node's
          // summary span, but a belt-and-suspenders reset is cheap.
          this._resetA11yMirror();
          this._nodes = [];
          this._edges = [];
          this.selectedNodes = [];
          this.selectedEdge = null;
          this._clickedEdge = null;
          this.requestDraw();
        }

        _removeNodeDom(node) {
          if (node._connectHandle) node._connectHandle.remove();
          if (node._targetHandles) {
            for (const s of ["left","right","top","bottom"]) {
              node._targetHandles[s].remove();
            }
          }
          if (node._badge && node._badge.parentNode) node._badge.remove();
          if (node._decorations && node._decorations.size > 0) {
            for (const d of node._decorations.values()) {
              if (d.el && d.el.parentNode) d.el.remove();
            }
            node._decorations.clear();
          }
          if (node._dom && node._dom.parentNode) node._dom.remove();
          // detach the per-node connections-summary span.
          if (typeof node._destroyA11ySummary === "function") {
            node._destroyA11ySummary();
          }
          this._clearFocusIfRemoved(node);
        }

        // Register a callback to run during destroy(). Returns a disposer
        // that unregisters the callback if you want to cancel before destroy.
        // Composes with on(): pass an on() disposer straight in for one-shot
        // setup-and-cleanup:
        //   canvas.onDestroy(canvas.on('selectionChange', handler));
        //   const tickId = setInterval(tick, 1000);
        //   canvas.onDestroy(() => clearInterval(tickId));
        // Callbacks run in LIFO order; one throwing does not block the rest
        // or the rest of destroy().
        onDestroy(fn) {
          if (typeof fn !== "function") return () => {};
          if (!this._destroyCallbacks) this._destroyCallbacks = [];
          this._destroyCallbacks.push(fn);
          return () => {
            if (!this._destroyCallbacks) return;
            const i = this._destroyCallbacks.indexOf(fn);
            if (i !== -1) this._destroyCallbacks.splice(i, 1);
          };
        }

        destroy() {
          // Idempotent: a second destroy() must not double-decrement the shared
          // style refcount (which would reclaim styles other live instances need).
          if (this._destroyed) return;
          this._destroyed = true;
          // Release the shared <head> styles when the last live instance goes.
          if (--_tzaraLiveInstances <= 0) {
            _tzaraLiveInstances = 0;
            _releaseSharedStyles();
          }
          // Run consumer-registered cleanup first (LIFO), so their handlers
          // can still see canvas state before we tear DOM/listeners down.
          if (this._destroyCallbacks) {
            const cbs = this._destroyCallbacks;
            this._destroyCallbacks = null;
            for (let i = cbs.length - 1; i >= 0; i--) {
              try { cbs[i](); } catch (e) { console.error("onDestroy callback threw:", e); }
            }
          }
          this._abortInteraction();
          this.hitbox_container.removeEventListener("pointermove",   this._handlers.pointermove, false);
          this.hitbox_container.removeEventListener("pointerleave",  this._handlers.pointerleave, false);
          this.hitbox_container.removeEventListener("pointerdown",   this._handlers.pointerdown, false);
          this.hitbox_container.removeEventListener("pointerup",     this._handlers.pointerup, false);
          this.hitbox_container.removeEventListener("pointercancel", this._handlers.pointercancel, false);
          this.hitbox_container.removeEventListener("dblclick",      this._handlers.dblclick, false);
          this.hitbox_container.removeEventListener("wheel",         this._handlers.wheel, false);
          this.hitbox_container.removeEventListener("contextmenu",   this._handlers.contextmenu);
          // Toolbar button listeners live inside the per-button closures
          // created by _wireToolbar; they are released when the toolbar DOM
          // is removed with the canvas, so no explicit removal here.
          this.outer_container.removeEventListener("keydown",      this._handlers.keydown, false);
          window.removeEventListener("blur",                       this._handlers.blur, false);

          // Tear down toolbar + panels (also removes their document-level
          // outside-click handlers so they don't outlive the canvas).
          this._destroyCanvasUI();

          if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
          }

          this._teardownDprWatch();

          if (this._themeMedia && this._onThemeChange) {
            this._themeMedia.removeEventListener("change", this._onThemeChange);
          }

          if (this._rafId) {
            cancelAnimationFrame(this._rafId);
            this._rafId = 0;
          }

          this.clearCanvas();
          if (this.container && this.container.parentNode) {
            this.container.parentNode.removeChild(this.container);
          }
          // close the help dialog if it was open at destroy time
          // so its backdrop and any captured focus don't outlive us.
          if (this._helpDialogEl) this._hideHelpDialog();
          // also strip the sr-only landmarks we appended to
          // the user-provided outer_container so leftover IDs / list
          // items / live regions don't outlive the canvas. Pending
          // announce timers get cleared too - they hold references to
          // the about-to-be-detached region.
          if (this._pendingDeleteTimer) {
            clearTimeout(this._pendingDeleteTimer);
            this._pendingDeleteTimer = null;
          }
          if (this._selectionAnnounceTimer) {
            clearTimeout(this._selectionAnnounceTimer);
            this._selectionAnnounceTimer = null;
          }
          if (this._a11yHeading && this._a11yHeading.parentNode) {
            this._a11yHeading.parentNode.removeChild(this._a11yHeading);
          }
          if (this._a11yEdgesList && this._a11yEdgesList.parentNode) {
            this._a11yEdgesList.parentNode.removeChild(this._a11yEdgesList);
          }
          if (this._a11yDescriptions && this._a11yDescriptions.parentNode) {
            this._a11yDescriptions.parentNode.removeChild(this._a11yDescriptions);
          }
          if (this._a11yLiveRegion && this._a11yLiveRegion.parentNode) {
            this._a11yLiveRegion.parentNode.removeChild(this._a11yLiveRegion);
          }
          this._handlers = null;
        }

        getNode(id)  {
          return this._nodes.find(n => n.id === id);

        }

        getEdge(id) {
          return this._edges.find(e => e.id === id);
        }

        // ----------------------------------------------------------------
        // Top-level Canvas API: selection, create/delete, batch
        // ----------------------------------------------------------------
        getSelection() {
          return {
            nodes: this.selectedNodes.slice(),
            edge:  this.selectedEdge || null,
          };
        }

        // Replace the current selection. `nodes` is an array of ids/instances
        // (or omitted to leave nodes untouched); `edge` is a single id/instance
        // or null (or omitted). Edge selection is singular by design today -
        // the UI exposes no multi-edge gesture, and bulk programmatic edge ops
        // are better served by canvas.graph.findEdges(...).forEach(...).
        setSelection({ nodes = null, edge = undefined } = {}) {
          const nextNodes = nodes != null ? this._resolveNodes(nodes) : this.selectedNodes.slice();
          let nextEdge;
          if (edge === undefined) {
            nextEdge = this.selectedEdge;
          } else {
            nextEdge = this._resolveEdge(edge);
          }
          this._setSelection(nextNodes, nextEdge);
          return this;
        }

        clearSelection() {
          this._setSelection([], null);
          return this;
        }

        _resolveNodes(list) {
          if (!Array.isArray(list)) list = [list];
          const out = [];
          for (const item of list) {
            if (!item) continue;
            if (typeof item === "object" && this._nodes.includes(item)) { out.push(item); continue; }
            const id = (typeof item === "object" && item.id) ? item.id : item;
            const n = this.getNode(id);
            if (n) out.push(n);
          }
          return out;
        }

        _resolveEdge(item) {
          if (item == null) return null;
          if (typeof item === "object" && this._edges.includes(item)) return item;
          const id = (typeof item === "object" && item.id) ? item.id : item;
          return this._edges.find(ed => ed.id === id) || null;
        }

        _setSelection(nextNodes, nextEdge) {
          const sameNodes = nextNodes.length === this.selectedNodes.length
                          && nextNodes.every((n, i) => n === this.selectedNodes[i]);
          const sameEdge = nextEdge === this.selectedEdge;
          if (sameNodes && sameEdge) return;
          // Keep edge.selected (read by drawSmartBezier) in lockstep with
          // selectedEdge. Mouse handlers set this flag directly; programmatic
          // selection via setSelection/clearSelection used to miss it.
          if (this.selectedEdge && this.selectedEdge !== nextEdge) {
            this.selectedEdge.selected = false;
          }
          if (nextEdge) nextEdge.selected = true;
          this.selectedNodes = nextNodes;
          this.selectedEdge = nextEdge;
          this._lastEmittedSelection = this._snapshotSelection();
          this._emit('selectionChange', this.getSelection());
          this.requestDraw();
        }

        _snapshotSelection() {
          return {
            nodeIds: this.selectedNodes.map(n => n.id).join('|'),
            edgeId: this.selectedEdge ? this.selectedEdge.id : null,
          };
        }

        // Called from existing UI handlers that mutate selection directly,
        // so emission still happens without rewriting each call site.
        _maybeEmitSelectionChange() {
          const snap = this._snapshotSelection();
          const prev = this._lastEmittedSelection;
          if (prev && prev.nodeIds === snap.nodeIds && prev.edgeId === snap.edgeId) return;
          this._lastEmittedSelection = snap;
          this._emit('selectionChange', this.getSelection());
        }

        _emitViewportChange() {
          this._emit('viewportChange', { pan: { x: this.panX, y: this.panY }, zoom: this.scale });
        }

        createNode(data) {
          const nodeData = { ...(data || {}) };
          if (!nodeData.id) nodeData.id = this._newId();
          if (nodeData.type == null) nodeData.type = "text";
          if (nodeData.x == null) nodeData.x = 0;
          if (nodeData.y == null) nodeData.y = 0;
          if (nodeData.width == null) nodeData.width = 250;
          if (nodeData.height == null) nodeData.height = 60;
          const node = new CanvasNode(this, nodeData);
          this._nodes.push(node);
          this._emit('nodeCreate', node);
          this._markDirty();
          this.requestDraw();
          return node;
        }

        createEdge(data) {
          const edgeData = { ...(data || {}) };
          if (!edgeData.id) edgeData.id = this._newId();
          if (!edgeData.fromSide) edgeData.fromSide = "right";
          if (!edgeData.toSide) edgeData.toSide = "left";
          const edge = new CanvasEdge(this, edgeData);
          this._edges.push(edge);
          this._emit('edgeCreate', edge);
          this._markDirty();
          this.requestDraw();
          return edge;
        }

        deleteNode(idOrInstance) {
          const node = (idOrInstance && typeof idOrInstance === "object")
            ? idOrInstance
            : this.getNode(idOrInstance);
          if (!node || !this._nodes.includes(node)) return false;
          if (!this._emitCancellable('beforeNodeDelete', node)) return false;
          const incident = this._edges.filter(e => e.fromNode === node.id || e.toNode === node.id);
          if (incident.length) {
            this._edges = this._edges.filter(e => !incident.includes(e));
            for (const e of incident) this._emit('edgeDelete', e);
          }
          this._removeNodeDom(node);
          this._nodes = this._nodes.filter(n => n !== node);
          if (this.selectedNodes.includes(node)) {
            this.selectedNodes = this.selectedNodes.filter(n => n !== node);
            this._maybeEmitSelectionChange();
          }
          this._emit('nodeDelete', node);
          this._markDirty();
          this.requestDraw();
          return true;
        }

        deleteEdge(idOrInstance) {
          const edge = (idOrInstance && typeof idOrInstance === "object")
            ? idOrInstance
            : this._edges.find(e => e.id === idOrInstance);
          if (!edge || !this._edges.includes(edge)) return false;
          if (!this._emitCancellable('beforeEdgeDelete', edge)) return false;
          this._edges = this._edges.filter(e => e !== edge);
          if (this.selectedEdge === edge) {
            this.selectedEdge = null;
            this._maybeEmitSelectionChange();
          }
          this._emit('edgeDelete', edge);
          this._markDirty();
          this.requestDraw();
          return true;
        }

        // Defer dataChange notifications and history snapshots until fn
        // returns. Useful for grouped mutations (bulk styling, programmatic
        // layout, etc.) - they become a single undo step + one dataChange.
        batch(fn) {
          if (typeof fn !== "function") return;
          this._batchDepth = (this._batchDepth || 0) + 1;
          try {
            fn();
          } finally {
            this._batchDepth--;
            if (this._batchDepth === 0) {
              if (this._pendingHistory && this.history && !this.history._suspended) {
                this._pendingHistory = false;
                this.history._recordChange();
              }
              if (this._pendingNotify) {
                this._pendingNotify = false;
                this._notifyChanged();
              }
            }
          }
        }

        // Load-time normalization gate (Phase 4). The single inbound boundary
        // where untrusted .canvas data is made safe to construct from, sharing
        // its rule predicates (finiteOr, edgeKey) with the read-only validate().
        // Returns clean plain { nodes, edges } plus a report of every change so a
        // host can surface data problems. With strict:true it instead throws on
        // any issue, the report attached as err.report.
        //
        // Repairs, in order:
        //  - nodes: generate a missing id, drop a duplicate id (first wins so the
        //    edges referencing it stay consistent), coerce non-finite geometry to
        //    defaults and clamp absurd width/height to MAX_NODE_EXTENT.
        //  - edges: generate a missing id, drop a duplicate edge id (first wins),
        //    drop an orphan whose endpoint id doesn't resolve to a kept node.
        // Edge *key* dedupe (same endpoints/sides under different ids) stays out
        // of the gate - it's opinionated about which to keep - and remains opt-in
        // via cleanup({ dedupeEdges:true }).
        _normalizeData(data) {
          const report = {
            droppedEdges: [], duplicateNodeIds: [], duplicateEdgeIds: [],
            generatedNodeIds: [], generatedEdgeIds: [], repairedGeometry: [],
          };
          const nodes = [];
          const nodeIds = new Set();
          (data.nodes || []).forEach(node => {
            let id = node.id;
            if (id == null || id === "") {
              // Reserve against ids already kept in this batch - they aren't in
              // _nodes/_edges yet, so _idInUse() alone can't see them.
              id = this._newId(nodeIds);
              report.generatedNodeIds.push(id);
            } else if (nodeIds.has(id)) {
              report.duplicateNodeIds.push({ id });
              return;
            }
            nodeIds.add(id);
            const x = finiteOr(node.x, DEFAULT_NODE_GEOMETRY.x);
            const y = finiteOr(node.y, DEFAULT_NODE_GEOMETRY.y);
            let width  = finiteOr(node.width,  DEFAULT_NODE_GEOMETRY.width);
            let height = finiteOr(node.height, DEFAULT_NODE_GEOMETRY.height);
            if (width  <= 0) width  = DEFAULT_NODE_GEOMETRY.width;
            if (height <= 0) height = DEFAULT_NODE_GEOMETRY.height;
            width  = Math.min(width,  MAX_NODE_EXTENT);
            height = Math.min(height, MAX_NODE_EXTENT);
            if (x !== node.x || y !== node.y || width !== node.width || height !== node.height) {
              report.repairedGeometry.push({ id });
            }
            nodes.push({ ...node, id, x, y, width, height });
          });
          const edges = [];
          const edgeIds = new Set();
          (data.edges || []).forEach(edge => {
            let id = edge.id;
            if (id == null || id === "") {
              // Reserve against every id kept in this batch (nodes share the
              // namespace), since none are in _nodes/_edges yet.
              id = this._newId(new Set([...nodeIds, ...edgeIds]));
              report.generatedEdgeIds.push(id);
            } else if (edgeIds.has(id)) {
              report.duplicateEdgeIds.push({ id });
              return;
            }
            // Orphan: an endpoint id that doesn't resolve to a kept node would
            // otherwise throw on every frame (rectBorderPoint dereferences an
            // undefined node) and blank the canvas - common in hand-edited /
            // wiki-merged .canvas files.
            const haveFrom = nodeIds.has(edge.fromNode);
            const haveTo   = nodeIds.has(edge.toNode);
            if (!haveFrom || !haveTo) {
              report.droppedEdges.push({ id, missing: !haveFrom ? edge.fromNode : edge.toNode });
              return;
            }
            edgeIds.add(id);
            edges.push({ ...edge, id });
          });

          if (this._strict) {
            const total = report.droppedEdges.length + report.duplicateNodeIds.length
              + report.duplicateEdgeIds.length + report.generatedNodeIds.length
              + report.generatedEdgeIds.length + report.repairedGeometry.length;
            if (total > 0) {
              const err = new Error("TzaraCanvas: strict mode rejected " + total +
                " normalization issue(s) in loaded data.");
              err.report = report;
              throw err;
            }
          }
          return { nodes, edges, report };
        }

        createNodesAndEdges(data) {
          // Route all inbound data through the single normalization gate, then
          // construct from the clean arrays - no per-item guards needed here.
          const { nodes, edges, report } = this._normalizeData(data);
          nodes.forEach(node => this._nodes.push(new CanvasNode(this, node)));
          edges.forEach(edge => this._edges.push(new CanvasEdge(this, edge)));

          this._lastLoadReport = report;
          if (report.duplicateNodeIds.length) {
            console.warn("TzaraCanvas: dropped " + report.duplicateNodeIds.length +
              " node(s) with duplicate ids at load:", report.duplicateNodeIds.map(n => n.id));
          }
          if (report.droppedEdges.length) {
            console.warn("TzaraCanvas: dropped " + report.droppedEdges.length +
              " edge(s) with unresolved endpoints at load:", report.droppedEdges.map(e => e.id));
          }
          if (report.duplicateEdgeIds.length) {
            console.warn("TzaraCanvas: dropped " + report.duplicateEdgeIds.length +
              " edge(s) with duplicate ids at load:", report.duplicateEdgeIds.map(e => e.id));
          }
          if (report.repairedGeometry.length) {
            console.warn("TzaraCanvas: repaired geometry on " + report.repairedGeometry.length +
              " node(s) at load:", report.repairedGeometry.map(n => n.id));
          }
          // Notify hosts of what the gate changed. The constructor's initial load
          // has no listeners yet (read canvas.lastLoadReport for that); later
          // `data =` reloads do reach subscribers.
          this._emit('load', report);
          // now that every edge is registered in _edges, populate
          // each node's connections summary in one bulk pass. Per-edge
          // refresh wouldn't work from the constructor (edge isn't in
          // _edges yet at that point).
          for (const n of this._nodes) this._refreshA11yNodeSummary(n);
        }

        _markDirty() {
          const wasDirty = this._isDirty;
          this._isDirty = true;
          if (!wasDirty) this._updateSaveResetButtons();
          // History: coalesce within a batch so a bulk operation produces one
          // undo step. Skip entirely while history is suspended (during its
          // own restore/undo/redo).
          if (this._batchDepth > 0) {
            this._pendingHistory = true;
          } else if (this.history && !this.history._suspended) {
            this.history._recordChange();
          }
          this._updateWrapperAriaLabel();
          this._notifyChanged();
        }

        _clearDirty() {
          if (!this._isDirty) return;
          this._isDirty = false;
          this._updateSaveResetButtons();
        }

        _notifyChanged() {
          if (this._batchDepth > 0) {
            this._pendingNotify = true;
            return;
          }
          this._emit('dataChange', this.io.toData());
        }

        // ----------------------------------------------------------------
        // Event bus
        // ----------------------------------------------------------------
        // Subscribe to an event. Returns a disposer function - call it to
        // unsubscribe. The disposer is idempotent (safe to call repeatedly)
        // and safe to call after destroy(). For composing with destroy
        // cleanup, pass it straight to canvas.onDestroy():
        //   canvas.onDestroy(canvas.on('selectionChange', handler));
        // The classic canvas.off(event, fn) form still works if you'd
        // rather hold onto the handler reference.
        on(event, fn) {
          if (typeof fn !== "function") return () => {};
          let set = this._emitter.get(event);
          if (!set) { set = new Set(); this._emitter.set(event, set); }
          set.add(fn);
          return () => { const s = this._emitter.get(event); if (s) s.delete(fn); };
        }

        off(event, fn) {
          const set = this._emitter.get(event);
          if (set) set.delete(fn);
          return this;
        }

        // Subscribe; the handler is removed after its first dispatch.
        // Returns a disposer function - call it to cancel before firing.
        once(event, fn) {
          if (typeof fn !== "function") return () => {};
          const wrapper = (...args) => { this.off(event, wrapper); fn(...args); };
          return this.on(event, wrapper);
        }

        _emit(event, ...args) {
          const set = this._emitter.get(event);
          if (!set || set.size === 0) return;
          for (const fn of [...set]) {
            try { fn(...args); }
            catch (err) { console.error(`TzaraCanvas listener for '${event}' threw:`, err); }
          }
        }

        // Like _emit, but a listener returning literally `false` cancels the
        // pending action. Used for before* events. Returns true if the action
        // should proceed.
        _emitCancellable(event, ...args) {
          const set = this._emitter.get(event);
          if (!set || set.size === 0) return true;
          for (const fn of [...set]) {
            try {
              if (fn(...args) === false) return false;
            } catch (err) {
              console.error(`TzaraCanvas listener for '${event}' threw:`, err);
            }
          }
          return true;
        }

        _updateSaveResetButtons() {
          const ro = this.permissions.isReadOnly();
          const showSave  = this._isDirty && !!this.onSaveRequest && !ro;
          const showReset = this._isDirty && !ro;
          if (this.toolbarSaveButton)        this.toolbarSaveButton.style.display        = showSave  ? "" : "none";
          if (this.toolbarResetCanvasButton) this.toolbarResetCanvasButton.style.display = showReset ? "" : "none";
          if (this.toolbarSaveButton) this.toolbarSaveButton.disabled = this._isSaving;
        }

        async _handleSaveClick() {
          if (!this.onSaveRequest || !this._isDirty || this._isSaving) return;
          this._isSaving = true;
          this._updateSaveResetButtons();
          const data = this.io.toData();
          this._emit('saveRequest', data);
          try {
            const result = this.onSaveRequest(data);
            if (result && typeof result.then === "function") await result;
            this._isSaving = false;
            this._clearDirty();
          } catch (err) {
            console.error("TzaraCanvas onSaveRequest rejected:", err);
            this._isSaving = false;
            this._updateSaveResetButtons();
          }
        }

        _handleResetClick() {
          if (!this._isDirty) return;
          this.resetCanvas();
          this._clearDirty();
          this._notifyChanged();
        }

      }

  // ====================================================================
  // Public surface - single global namespace.
  // ====================================================================
  return { Canvas, CanvasNode, CanvasEdge, easings: EASINGS };
}));
