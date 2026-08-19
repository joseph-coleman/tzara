// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Shared KaTeX auto-render bootstrap. Loaded by any page that pulls in the
// KaTeX CDN scripts (view pages gate this on has_latex; the editor loads it
// unconditionally for live preview). Renders math on load, and exposes
// window.tzaraRenderMath(el) so the editor's Preview button can re-render
// without duplicating the delimiter config.
(function () {
  const DELIMITERS = [
    { left: '\\(', right: '\\)', display: false },
    { left: '\\[', right: '\\]', display: true },
  ];

  window.tzaraRenderMath = function (el) {
    if (typeof renderMathInElement !== 'function') return;
    renderMathInElement(el || document.body, {
      delimiters: DELIMITERS,
      throwOnError: false,
    });
  };

  document.addEventListener('DOMContentLoaded', function () {
    window.tzaraRenderMath(document.body);
  });
})();
