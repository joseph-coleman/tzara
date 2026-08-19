// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Async code block highlighter for wiki pages.
//
// Server-rendered markdown ships code blocks as plain <pre><code>; this walks
// them after load and swaps each for a server-highlighted version via
// /api/markdown/code/. Runs unconditionally on any page that includes it and
// is a no-op on pages without code blocks. No template variables -- pure DOM.
(function () {
  'use strict';

  const API_ENDPOINT = '/api/markdown/code/';

  /**
   * Extract all attributes from an element as an object
   */
  function getAttributes(element) {
    const attrs = {};
    for (let i = 0; i < element.attributes.length; i++) {
      const attr = element.attributes[i];
      attrs[attr.name] = attr.value;
    }
    return attrs;
  }

  /**
   * Send code block to API for highlighting
   */
  async function highlightCodeBlock(preElement) {
    const codeElement = preElement.querySelector('code');
    if (!codeElement) return;

    // Extract code content and attributes
    const code = codeElement.textContent;
    const preAttrs = getAttributes(preElement);
    const codeAttrs = getAttributes(codeElement);

    // Prepare payload
    const payload = {
      code: code,
      pre_attrs: preAttrs,
      code_attrs: codeAttrs
    };

    try {
      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        console.warn('Code highlighting failed with status:', response.status);
        return;
      }

      const html = await response.text();

      // Create a temporary container to parse the HTML
      const temp = document.createElement('div');
      temp.innerHTML = html;

      // Replace the old pre element with the new one
      const newPreElement = temp.firstElementChild;
      if (newPreElement) {
        preElement.replaceWith(newPreElement);
      }
    } catch (error) {
      // Fail silently - keep original code block
      console.warn('Code highlighting error:', error);
    }
  }

  /**
   * Process all code blocks on the page
   */
  function highlightAllCodeBlocks() {
    // Find all pre elements containing code elements
    const preElements = document.querySelectorAll('pre:has(code)');

    // Process each code block, excluding those already highlighted
    preElements.forEach(preElement => {
      // Skip if this pre is inside a div.codehilite
      if (preElement.closest('div.codehilite')) {
        return;
      }

      highlightCodeBlock(preElement);
    });
  }

  // Run when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', highlightAllCodeBlocks);
  } else {
    highlightAllCodeBlocks();
  }
})();
