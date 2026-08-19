// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Index-page file manager: multi-select + destination picker + drag-and-drop,
// moving files/folders via POST /api/batch-move. Every selectable row is an
// <li data-path data-type> emitted by index_document (data-type "dir" for
// folders, otherwise the file extension). No build step -- plain ES, committed.
(function () {
  'use strict';

  function init() {
  const container = document.getElementById('document_container');
  if (!container) return;

  const rows = Array.from(container.querySelectorAll('li[data-path]'));
  if (!rows.length) return;

  let lastClickedIdx = null;   // anchor for shift-click range selection
  let dragPaths = [];          // paths being dragged in the current DnD

  // ---- selection -----------------------------------------------------------

  function selectedRows() {
    return rows.filter((li) => li.__check && li.__check.checked);
  }

  function selectedPaths() {
    return selectedRows().map((li) => li.dataset.path);
  }

  function updateToolbar() {
    const n = selectedRows().length;
    countEl.textContent = n + ' selected';
    toolbar.classList.toggle('idx-has-sel', n > 0);
  }

  rows.forEach((li, idx) => {
    li.classList.add('idx-row');
    // Native anchor dragging would shadow our row drag; disable it.
    li.querySelectorAll('a').forEach((a) => (a.draggable = false));

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'idx-check';
    li.insertBefore(cb, li.firstChild);
    li.__check = cb;

    cb.addEventListener('click', (e) => {
      if (e.shiftKey && lastClickedIdx !== null) {
        const [a, b] = [lastClickedIdx, idx].sort((x, y) => x - y);
        for (let i = a; i <= b; i++) rows[i].__check.checked = cb.checked;
      }
      lastClickedIdx = idx;
      updateToolbar();
    });

    // Drag-and-drop: rows are draggable, folders are drop targets.
    li.setAttribute('draggable', 'true');
    li.addEventListener('dragstart', (e) => {
      // Rows nest (folders contain their children), so dragstart bubbles to
      // ancestor rows whose handlers would clobber dragPaths with the parent
      // folder's path. Stop here so only the dragged row sets the payload.
      e.stopPropagation();
      // Drag the whole selection if this row is part of it, else just this row.
      dragPaths = cb.checked ? selectedPaths() : [li.dataset.path];
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragPaths.join('\n'));
      li.classList.add('idx-dragging');
      // Signal page-wide "drag mode" so drop targets (root box + folder rows)
      // light up; otherwise they look inert until something hovers them.
      document.body.classList.add('idx-dnd-active');
    });
    li.addEventListener('dragend', () => {
      li.classList.remove('idx-dragging');
      document.body.classList.remove('idx-dnd-active');
    });

    if (li.dataset.type === 'dir') registerDropTarget(li, () => li.dataset.path);
  });

  function registerDropTarget(el, destFn) {
    el.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = 'move';
      el.classList.add('idx-drop-hover');
    });
    el.addEventListener('dragleave', () => el.classList.remove('idx-drop-hover'));
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      el.classList.remove('idx-drop-hover');
      doMove(dragPaths, destFn());
    });
  }

  // ---- toolbar -------------------------------------------------------------

  const toolbar = document.createElement('div');
  toolbar.id = 'idx-toolbar';
  toolbar.innerHTML =
    '<span id="idx-count">0 selected</span>' +
    '<button type="button" id="idx-move-btn">Move to…</button>' +
    '<button type="button" id="idx-delete-btn">Delete</button>' +
    '<button type="button" id="idx-clear-btn">Clear</button>' +
    '<button type="button" id="idx-collapse-all" title="Collapse every folder">Collapse all</button>' +
    '<button type="button" id="idx-expand-all" title="Expand every folder">Expand all</button>' +
    '<span class="idx-spacer"></span>' +
    '<span class="idx-root-drop" title="Drop here to move to the wiki root">📂 / (root)</span>';
  container.parentNode.insertBefore(toolbar, container);

  const countEl = toolbar.querySelector('#idx-count');
  toolbar.querySelector('#idx-move-btn').addEventListener('click', openPicker);
  toolbar.querySelector('#idx-delete-btn').addEventListener('click', () =>
    doDelete(selectedPaths())
  );
  toolbar.querySelector('#idx-clear-btn').addEventListener('click', () => {
    rows.forEach((li) => (li.__check.checked = false));
    lastClickedIdx = null;
    updateToolbar();
  });
  registerDropTarget(toolbar.querySelector('.idx-root-drop'), () => '');

  // ---- collapsible folders (the folder EMOJI is the toggle) ----------------
  // Every folder row (li[data-type="dir"]) with children gets a clickable folder
  // emoji - 📂 open / 📁 collapsed - that shows/hides its nested <ul> and REPLACES
  // the ::marker (CSS .idx-collapsible::marker { content: none }), so no duplicate
  // icon and no extra arrow/whitespace. State persists per-vault in localStorage,
  // so long index pages stay tamed across reloads. General: works for any folder.
  const COLLAPSE_KEY = 'tzara-idx-collapsed:' + (window.WIKI_VAULT || '');
  let collapsedSet;
  try {
    collapsedSet = new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]'));
  } catch (e) {
    collapsedSet = new Set();
  }
  const saveCollapsed = () => {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsedSet]));
    } catch (e) {
      /* storage blocked/full: degrade to non-persistent, still works this session */
    }
  };

  const folderRecs = [];
  function applyCollapse(rec) {
    const isCol = collapsedSet.has(rec.path);
    rec.sub.hidden = isCol;
    rec.li.classList.toggle('idx-collapsed', isCol);
    rec.emoji.textContent = isCol ? '📁' : '📂';
    rec.emoji.setAttribute('aria-expanded', String(!isCol));
    rec.emoji.title = isCol ? 'Expand folder' : 'Collapse folder';
  }
  container.querySelectorAll('li[data-type="dir"]').forEach((li) => {
    const sub = li.querySelector(':scope > ul');
    if (!sub) return; // empty folder: keep its ::marker, nothing to collapse
    li.classList.add('idx-collapsible'); // CSS drops the ::marker; our emoji stands in
    const emoji = document.createElement('span');
    emoji.className = 'idx-diremoji';
    emoji.setAttribute('role', 'button');
    emoji.setAttribute('tabindex', '0');
    li.insertBefore(emoji, li.firstChild); // before the checkbox
    const rec = { li, sub, emoji, path: li.dataset.path };
    folderRecs.push(rec);
    const toggle = (e) => {
      e.preventDefault();
      e.stopPropagation(); // don't trip row selection / drag
      if (collapsedSet.has(rec.path)) collapsedSet.delete(rec.path);
      else collapsedSet.add(rec.path);
      saveCollapsed();
      applyCollapse(rec);
    };
    emoji.addEventListener('click', toggle);
    emoji.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') toggle(e);
    });
    applyCollapse(rec);
  });
  function setAllCollapsed(state) {
    folderRecs.forEach((rec) => {
      if (state) collapsedSet.add(rec.path);
      else collapsedSet.delete(rec.path);
      applyCollapse(rec);
    });
    saveCollapsed();
  }
  toolbar
    .querySelector('#idx-collapse-all')
    .addEventListener('click', () => setAllCollapsed(true));
  toolbar
    .querySelector('#idx-expand-all')
    .addEventListener('click', () => setAllCollapsed(false));

  // ---- destination picker --------------------------------------------------

  function allFolders() {
    const set = new Set(['']); // '' == wiki root
    container.querySelectorAll('li[data-type="dir"]').forEach((li) =>
      set.add(li.dataset.path)
    );
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }

  function openPicker() {
    if (!selectedRows().length) {
      toast('Select at least one item first.');
      return;
    }
    const overlay = document.createElement('div');
    overlay.className = 'idx-modal-overlay';
    const allDests = allFolders();
    const folders = allDests
      .map(
        (f) =>
          `<li class="idx-pick" data-dest="${escapeAttr(f)}">${
            f === '' ? '📂 / (root)' : '📂 ' + escapeHtml(f)
          }</li>`
      )
      .join('');
    overlay.innerHTML =
      '<div class="idx-modal"><div class="idx-modal-head">Move ' +
      selectedRows().length +
      ' item(s) to…</div>' +
      '<input type="text" class="idx-pick-filter" ' +
      'placeholder="Filter or type a new folder name…" autocomplete="off">' +
      '<ul class="idx-pick-list">' +
      '<li class="idx-pick idx-pick-create" hidden></li>' +
      folders +
      '</ul><button type="button" class="idx-modal-cancel">Cancel</button></div>';
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
    });
    overlay.querySelector('.idx-modal-cancel').addEventListener('click', close);

    // Existing-folder rows: move the selection into that folder.
    overlay.querySelectorAll('.idx-pick:not(.idx-pick-create)').forEach((li) =>
      li.addEventListener('click', () => {
        close();
        doMove(selectedPaths(), li.dataset.dest);
      })
    );

    // Filter box doubles as a "new folder" name. Typing filters the existing
    // list; a non-matching name surfaces a create row that moves into the new
    // (auto-created) folder.
    const input = overlay.querySelector('.idx-pick-filter');
    const createRow = overlay.querySelector('.idx-pick-create');
    const existing = new Set(allDests);

    const createInto = (dest) => {
      close();
      doMove(selectedPaths(), dest);
    };
    createRow.addEventListener('click', () => {
      if (createRow.dataset.dest) createInto(createRow.dataset.dest);
    });

    input.addEventListener('input', () => {
      const raw = input.value.trim();
      const q = raw.toLowerCase();
      overlay.querySelectorAll('.idx-pick:not(.idx-pick-create)').forEach((li) => {
        li.hidden = q !== '' && !li.dataset.dest.toLowerCase().includes(q);
      });
      if (raw && !existing.has(raw)) {
        createRow.dataset.dest = raw;
        createRow.innerHTML = '➕ Create folder “' + escapeHtml(raw) + '”';
        createRow.hidden = false;
      } else {
        createRow.hidden = true;
        delete createRow.dataset.dest;
      }
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && createRow.dataset.dest) {
        e.preventDefault();
        createInto(createRow.dataset.dest);
      }
    });
    input.focus();
  }

  // ---- move execution ------------------------------------------------------

  async function doMove(items, destination) {
    items = (items || []).filter(Boolean);
    if (!items.length) return;

    // Refuse moving a folder into itself or its own descendant (server guards
    // too, but failing fast avoids a wasted round trip and a confusing skip).
    const bad = items.find(
      (p) => destination === p || destination.startsWith(p + '/')
    );
    if (bad) {
      toast(`Can't move “${bad}” into itself.`);
      return;
    }

    try {
      const res = await fetch('/api/batch-move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, destination, vault: window.WIKI_VAULT }),
      });
      const data = await res.json();
      if (!res.ok || (data.status !== 'ok' && data.status !== 'noop')) {
        toast('Move failed: ' + (data.reason || res.status));
        return;
      }
      const moved = (data.moved || []).length;
      const skipped = data.skipped || [];
      if (skipped.length) {
        toast(
          `Moved ${moved}; skipped ${skipped.length} (${skipped[0].reason}…)`
        );
        setTimeout(() => location.reload(), 1400);
      } else {
        location.reload();
      }
    } catch (err) {
      toast('Move error: ' + err.message);
    }
  }

  // ---- delete execution ----------------------------------------------------

  async function doDelete(items) {
    items = (items || []).filter(Boolean);
    if (!items.length) {
      toast('Select at least one item first.');
      return;
    }
    const msg =
      items.length === 1
        ? `Delete “${items[0]}”? This cannot be undone.`
        : `Delete ${items.length} items? This cannot be undone.`;
    if (!window.confirm(msg)) return;

    try {
      const res = await fetch('/api/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, vault: window.WIKI_VAULT }),
      });
      const data = await res.json();
      if (!res.ok || (data.status !== 'ok' && data.status !== 'noop')) {
        toast('Delete failed: ' + (data.reason || res.status));
        return;
      }
      const deleted = (data.deleted || []).length;
      const skipped = data.skipped || [];
      if (skipped.length) {
        toast(
          `Deleted ${deleted}; skipped ${skipped.length} (${skipped[0].reason}…)`
        );
        setTimeout(() => location.reload(), 1400);
      } else {
        location.reload();
      }
    } catch (err) {
      toast('Delete error: ' + err.message);
    }
  }

  // ---- small helpers -------------------------------------------------------

  let toastTimer = null;
  function toast(msg) {
    let t = document.getElementById('idx-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'idx-toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('idx-toast-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('idx-toast-show'), 3000);
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])
    );
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }

  updateToolbar();
  }

  // base_header scripts run during <head> parsing, before #document_container
  // exists in the body, so defer init until the DOM is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
