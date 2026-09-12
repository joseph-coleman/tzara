// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Unsaved-edit drafts for the /edit/ view, kept in this browser's localStorage.

   While the buffer differs from the text the page opened with, it is written to
   the page's slot about a second after the last edit, and again when the tab is
   hidden or closed. Opening the page later - in this tab, another tab, or after
   a crash - offers the draft back (edit.html's banner); nothing is restored
   unasked. Drafts are per browser: localStorage belongs to one origin and
   profile, so another browser or machine does not see them.

   One slot per page, and the newest EDIT owns it: a tab writes only when its
   last edit is newer than what the slot holds, so a forgotten tab never
   overwrites newer work from another tab, nor a save or a discard (which leave
   a short-lived tombstone). While a draft is on offer the tab leaves the slot
   alone, so an unanswered offer survives the visit. Tabs hear each other's
   writes through the `storage` event; a tab's own text is never touched.

   The pure helpers are top-level named functions so app/.test/test_edit_draft.js
   can slice them out by name.
*/

// localStorage key for a page's draft. A historical revision being edited gets
// its own slot: its text is not the page's.
function draftKey(vault, source, revision) {
  return "tzara:draft:" + vault + ":" + source + (revision ? "@" + revision : "");
}

// What to do with the slot's record when the page opens:
//   "none"         nothing stored
//   "discard"      a tombstone, or a draft identical to the page's text - drop it
//   "offer"        a draft started from the page as it is now
//   "offer-stale"  a draft started from an older version of the page
function draftDecision(record, initialDoc, baseHash) {
  if (!record || typeof record !== "object") return "none";
  if (record.state !== "draft" || typeof record.text !== "string") return "discard";
  if (record.text === initialDoc) return "discard";
  return record.base === baseHash ? "offer" : "offer-stale";
}

// Whether this tab may write its buffer over the slot's record: its own record
// always, anyone else's only with a newer edit. A tombstone's `at` is when the
// save or discard happened, so edits made before it cannot resurrect a draft.
function mayWrite(record, tab, lastEditAt) {
  if (!record || typeof record !== "object") return true;
  if (record.tab === tab) return true;
  return (record.at || 0) < lastEditAt;
}

// How another tab's write to this page's slot concerns this tab (null: it doesn't):
//   "saved"      the page was saved there, so this copy is out of date
//   "taken"      newer unsaved edits there, while this tab has edits of its own
//   "discarded"  the draft was discarded there, while this tab still has edits
function remoteChange(record, tab, dirty) {
  if (!record || typeof record !== "object" || record.tab === tab) return null;
  if (record.state === "saved") return "saved";
  if (!dirty) return null;
  return record.state === "discarded" ? "discarded" : "taken";
}

// "just now", "5 minutes ago", "3 hours ago", "2 days ago".
function describeAge(ms) {
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
  const days = Math.floor(hours / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

(function () {
  const PREFIX = "tzara:draft:";
  // Quiet period after the last edit before the draft is written.
  const WRITE_DELAY_MS = 1000;
  // Tombstones only need to outlive the tabs that might still read them.
  const TOMBSTONE_TTL_MS = 24 * 60 * 60 * 1000;

  function parse(json) {
    try { return JSON.parse(json); } catch (e) { return null; }
  }

  function pruneTombstones(now) {
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith(PREFIX)) keys.push(key);
    }
    for (const key of keys) {
      const record = parse(localStorage.getItem(key));
      if (!record || (record.state !== "draft" && now - (record.at || 0) > TOMBSTONE_TTL_MS)) {
        localStorage.removeItem(key);
      }
    }
  }

  // Per page load. Not crypto.randomUUID: that needs a secure context, and a LAN
  // http:// address is not one.
  function newTabId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  // opts: {key, initialDoc, baseHash, onEvent(kind)}. Kinds: remoteChange's, plus
  // "withdrawn" (the draft on offer went away) and "full" (storage quota).
  // Returns null when localStorage is unavailable; the editor then works without.
  function create(opts) {
    try {
      localStorage.getItem(PREFIX);
    } catch (e) {
      console.warn("edit_draft: localStorage unavailable - drafts disabled", e);
      return null;
    }
    const { key, initialDoc } = opts;
    const onEvent = opts.onEvent || (() => {});
    const tab = newTabId();
    let baseHash = opts.baseHash;
    let view = null;
    let lastEditAt = 0;
    let timer = null;
    let done = false;   // saved or deleted: the page is being left, write nothing more

    pruneTombstones(Date.now());
    let offered = parse(localStorage.getItem(key));
    const decision = draftDecision(offered, initialDoc, baseHash);
    if (decision === "discard") localStorage.removeItem(key);
    if (!decision.startsWith("offer")) offered = null;

    function isDirty() {
      return !!view && view.state.doc.toString() !== initialDoc;
    }

    function store(record) {
      try {
        localStorage.setItem(key, JSON.stringify(record));
      } catch (e) {
        console.warn("edit_draft:", e);
        onEvent("full");
      }
    }

    // Bring the slot in line with this tab's buffer.
    function flush() {
      clearTimeout(timer);
      timer = null;
      if (!view || offered || done) return;
      const stored = parse(localStorage.getItem(key));
      const text = view.state.doc.toString();
      if (text === initialDoc) {
        if (stored && stored.state === "draft" && stored.tab === tab) localStorage.removeItem(key);
        return;
      }
      if (stored && stored.tab === tab && stored.text === text && stored.base === baseHash) return;
      if (!mayWrite(stored, tab, lastEditAt)) return;
      store({ state: "draft", text, base: baseHash, tab, at: lastEditAt });
    }

    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flush();
    });
    window.addEventListener("storage", (event) => {
      if (event.key !== key) return;
      const record = parse(event.newValue);
      if (offered) {
        // Keep offering the newest version of the draft; drop the offer when the
        // draft is saved or discarded elsewhere.
        if (record && record.state === "draft") {
          offered = record;
          return;
        }
        offered = null;
        onEvent(record && record.state === "saved" ? "saved" : "withdrawn");
        return;
      }
      const kind = remoteChange(record, tab, isDirty());
      if (kind) onEvent(kind);
    });

    const extension = window.CMEditor.EditorView.updateListener.of((update) => {
      if (!update.docChanged) return;
      lastEditAt = Date.now();
      clearTimeout(timer);
      timer = setTimeout(flush, WRITE_DELAY_MS);
    });

    return {
      decision,
      extension,
      bind(v) { view = v; },
      // The draft on offer (the newest seen), or null.
      offered() { return offered; },
      // Hand over the offered draft for restoring; this tab's edits own the slot after.
      takeOffer() {
        const record = offered;
        offered = null;
        return record;
      },
      discard() {
        offered = null;
        store({ state: "discarded", tab, at: Date.now() });
      },
      markSaved() {
        done = true;
        clearTimeout(timer);
        store({ state: "saved", tab, at: Date.now() });
      },
      // The page itself is going away (delete).
      forget() {
        done = true;
        clearTimeout(timer);
        localStorage.removeItem(key);
      },
      // A save conflict rebased the editor onto the file on disk now.
      setBase(hash) { baseHash = hash; },
    };
  }

  window.EditDraft = { create, draftKey, describeAge };
})();
