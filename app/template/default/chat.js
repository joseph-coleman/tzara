// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// ---- Streaming Chat UI ----
// Shared chat module used by both document-level and global corpus chat.
// Call initChat(config) to initialize.
//
// config.urlPath   - document URL path (empty string for global chat)
// config.mode      - null = use toggle (document page), 'wiki' = always wiki mode
// config.vault     - vault slug this chat is scoped to (hard isolation)
// config.showDocPath - true to emphasize document paths in action cards (global mode)
// config.revision  - git sha when viewing a historical revision, else empty

function initChat(config) {
  'use strict';

  let chatSessionId = null;
  const urlPath = config.urlPath || '';
  const vault = config.vault || 'main';
  // Mutable: cleared once an edit is applied, since what we just wrote becomes
  // the current version.
  let currentRevision = config.revision || '';
  let statusTimerInterval = null;
  let statusStartTime = null;
  let activityLog = null;
  let actionHotkeyListener = null;

  function getChatMode() {
    return config.mode || 'document';
  }

  function appendMessage(role, html) {
    const history = document.getElementById('chat_history');
    if (!history) return;
    const msg = document.createElement('div');
    msg.className = 'chat-msg chat-msg-' + role;
    msg.innerHTML = html;
    history.appendChild(msg);
    scrollHistory(history);
  }

  function appendSources(sources) {
    if (!sources || sources.length === 0) return;
    const history = document.getElementById('chat_history');
    const el = document.createElement('div');
    el.className = 'chat-sources';
    el.innerHTML = '<strong>Sources:</strong> ' + sources.map(
      s => '<a href="/wiki/' + s.url_path + '">' + s.title + '</a>'
    ).join(', ');
    history.appendChild(el);
    scrollHistory(history);
  }

  function showResetButton() {
    const btn = document.getElementById('chat_reset_btn');
    if (btn) btn.classList.remove('chat-hidden');
  }

  function escapeHtml(text) {
    return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function scrollHistory(historyEl) {
    historyEl.scrollTop = historyEl.scrollHeight;
    var chatInput = document.getElementById('chat_input');
    if (chatInput && document.activeElement === chatInput) {
      chatInput.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function renderDiffPreview(diffText) {
    if (!diffText) return '';
    const lines = diffText.split('\n');
    const htmlLines = lines.map(function(line) {
      if (line.startsWith('---') || line.startsWith('+++')) {
        return '<span class="diff-ctx">' + escapeHtml(line) + '</span>';
      } else if (line.startsWith('@@')) {
        return '<span class="diff-hunk">' + escapeHtml(line) + '</span>';
      } else if (line.startsWith('-')) {
        return '<span class="diff-del">' + escapeHtml(line) + '</span>';
      } else if (line.startsWith('+')) {
        return '<span class="diff-add">' + escapeHtml(line) + '</span>';
      }
      return '<span class="diff-ctx">' + escapeHtml(line) + '</span>';
    });
    return '<div class="chat-diff">' + htmlLines.join('\n') + '</div>';
  }

  function appendActionCard(action) {
    const history = document.getElementById('chat_history');
    if (!history) return;
    const card = document.createElement('div');
    card.className = 'chat-action-card callout callout-info';
    card.setAttribute('data-callout', 'info');

    const actionLabels = {
      'scratchpad_changes': 'Document Changes',
      'suggest_edit': 'Edit Section',
      'create_document': 'Create Document',
      'append_to_document': 'Append to Document',
      'replace_section': 'Replace/Add Section',
      'multi_document_changes': 'Multi-Document Changes',
      'run_python': 'Run Python'
    };

    let previewHtml = '';

    // Agent-authored code awaiting approval (run_python tool)
    if (action.action === 'run_python') {
      previewHtml = '<pre class="chat-code-proposal"><code>' +
                    escapeHtml(action.code || '') + '</code></pre>';
    // Multi-document diffs (global mode)
    } else if (action.diffs && Array.isArray(action.diffs)) {
      previewHtml = action.diffs.map(function(d) {
        return '<div class="chat-diff-doc-header"><code>' + escapeHtml(d.path) + '</code></div>' +
               renderDiffPreview(d.diff_preview);
      }).join('');
    } else if (action.diff_preview) {
      // Single document diff - show path prominently in global mode
      if (config.showDocPath && action.path) {
        previewHtml = '<div class="chat-diff-doc-header"><code>' + escapeHtml(action.path) + '</code></div>';
      }
      // Editing an old revision: the diff below is the agent's change against
      // THAT revision, but applying overwrites the current file. Say what gets
      // discarded and offer the diff that actually lands.
      if (action.head_drift) {
        const drift = action.head_drift;
        const n = drift.commits_since || 0;
        previewHtml +=
          '<div class="chat-action-warning callout callout-warning" data-callout="warning">' +
            '<strong>&#9888; Editing revision ' + escapeHtml(drift.short_sha || '') +
            (drift.date_str ? ' (' + escapeHtml(drift.date_str) + ')' : '') + '</strong>' +
            '<div>Applying will overwrite the current version' +
            (n ? ', discarding ' + n + ' later commit' + (n === 1 ? '' : 's') : '') + '.</div>' +
            '<details><summary>Show full diff vs current</summary>' +
            renderDiffPreview(drift.head_diff) +
            '</details>' +
          '</div>';
      }
      previewHtml += renderDiffPreview(action.diff_preview);
    } else {
      previewHtml = '<pre>' + escapeHtml(action.preview || '') + '</pre>';
    }

    card.innerHTML =
      '<div class="chat-action-header">' +
        '<span class="chat-action-icon">&#9998;</span> ' +
        '<strong>' + (actionLabels[action.action] || action.action) + '</strong>' +
        (action.path && !action.diffs ? ' &mdash; <code>' + escapeHtml(action.path) + '</code>' : '') +
      '</div>' +
      '<div class="chat-action-preview">' + previewHtml + '</div>' +
      '<div class="chat-action-buttons">' +
        '<button class="chat-action-btn chat-action-allow btn-sm" onclick="confirmAction(true)">Allow (Y)</button>' +
        '<button class="chat-action-btn chat-action-deny btn-sm" onclick="confirmAction(false)">Deny (N)</button>' +
      '</div>';
    history.appendChild(card);
    scrollHistory(history);

    // Auto-focus the Allow button so Enter confirms immediately - but not when
    // approving also discards later commits; that deserves a deliberate click.
    const allowBtn = card.querySelector('.chat-action-allow');
    if (allowBtn && !action.head_drift) allowBtn.focus();

    // Y/N hotkeys when chat input is not focused
    if (actionHotkeyListener) {
      document.removeEventListener('keydown', actionHotkeyListener);
    }
    actionHotkeyListener = function(e) {
      if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') return;
      if (e.key === 'y' || e.key === 'Y') { e.preventDefault(); confirmAction(true); }
      if (e.key === 'n' || e.key === 'N') { e.preventDefault(); confirmAction(false); }
    };
    document.addEventListener('keydown', actionHotkeyListener);
  }

  // Strip ?revision= from the address bar and from every link the server rendered
  // with it (header/footer View-Edit-Raw, page title). Note window.history: the
  // callers shadow `history` with the chat history element.
  function dropRevisionFromPage() {
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.has('revision')) {
        url.searchParams.delete('revision');
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
      document.querySelectorAll('a[href*="revision="]').forEach(function(a) {
        const href = new URL(a.getAttribute('href'), window.location.origin);
        href.searchParams.delete('revision');
        a.setAttribute('href', href.pathname + href.search + href.hash);
      });
    } catch(e) { /* URL parsing failure must not break the action card */ }
  }

  function appendActionResult(result) {
    const history = document.getElementById('chat_history');
    if (!history) return;
    const card = document.createElement('div');
    const calloutType = result.success ? 'success' : 'failure';
    card.className = 'chat-action-result callout callout-' + calloutType;
    card.setAttribute('data-callout', calloutType);
    let html = '<strong>' + (result.success ? '&#10003;' : '&#10007;') + ' ' + result.message + '</strong>';
    if (result.success && result.revision_cleared) {
      // We were viewing history and just wrote a new current version. Drop the
      // revision from the address bar and from the links the page was rendered
      // with, so nothing keeps asserting a revision that is no longer on screen.
      // Done in place rather than by navigating: a reload would cut the confirm
      // stream (the agent loop may still be resuming) and lose the conversation,
      // which lives only in this page's session id.
      currentRevision = '';
      dropRevisionFromPage();
    }
    if (result.success && result.url) {
      // Live-refresh document content if viewing the affected page
      (async function() {
        const container = document.getElementById('document_container');
        if (!container) return;
        const currentUrl = window.location.pathname;
        const expectedUrl = '/wiki/' + urlPath.replace(/^wiki\//, '');
        if (result.url !== currentUrl && result.url !== expectedUrl) return;

        try {
          const rawPath = result.url.replace(/^\/wiki\//, '');
          const rawResp = await fetch('/raw/' + rawPath);
          if (!rawResp.ok) return;
          const markdown = await rawResp.text();

          const formData = new FormData();
          formData.append('markdown', markdown);
          formData.append('format_code', true);
          formData.append('document_name', rawPath);
          const htmlResp = await fetch('/api/markdown/', { method: 'POST', body: formData });
          if (!htmlResp.ok) return;
          container.innerHTML = await htmlResp.text();

          // Typeset any math in the freshly injected response (shared config
          // in math.js; self-guards when KaTeX isn't loaded on the page).
          if (window.tzaraRenderMath) window.tzaraRenderMath(container);
          var chatInput = document.getElementById('chat_input');
          if (chatInput) {
            chatInput.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            chatInput.focus();
          }
        } catch(e) { html += '<a href="javascript:location.reload()">Reload page</a> '; }
      })();

      html += ' <a href="' + result.url + '">View document</a>';
      // // If editing current page, offer reload
      // if (result.url === window.location.pathname || result.url === '/wiki/' + urlPath.replace(/^wiki\//, '')) {
      // }
    }
    // Multi-document results (global mode)
    if (result.success && result.paths && Array.isArray(result.paths)) {
      html += '<div class="chat-action-paths">';
      result.paths.forEach(function(p) {
        var docUrl = '/wiki/' + p.replace(/^wiki\//, '');
        html += ' <a href="' + docUrl + '">' + escapeHtml(p) + '</a>';
      });
      html += '</div>';
    }
    card.innerHTML = html;
    history.appendChild(card);
    scrollHistory(history);
  }

  function appendContinueCard(title, message) {
    title = title || 'Reached the step limit';
    message = message || 'The assistant ran out of steps before finishing. ' +
      'Continue to give it another round.';
    const history = document.getElementById('chat_history');
    if (!history) return;
    const card = document.createElement('div');
    card.className = 'chat-action-card chat-continue-card callout callout-info';
    card.setAttribute('data-callout', 'info');
    card.innerHTML =
      '<div class="chat-action-header">' +
        '<span class="chat-action-icon">&#8987;</span> ' +
        '<strong>' + title + '</strong>' +
      '</div>' +
      '<div class="chat-action-preview">' + message + '</div>' +
      '<div class="chat-action-buttons">' +
        '<button class="chat-action-btn chat-action-allow btn-sm" onclick="continueAgent()">Continue (C)</button>' +
      '</div>';
    history.appendChild(card);
    scrollHistory(history);

    // Auto-focus the Continue button so Enter resumes immediately
    const contBtn = card.querySelector('.chat-action-allow');
    if (contBtn) contBtn.focus();

    // C hotkey when chat input is not focused (mirrors the Y/N action hotkeys)
    if (actionHotkeyListener) {
      document.removeEventListener('keydown', actionHotkeyListener);
    }
    actionHotkeyListener = function(e) {
      if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') return;
      if (e.key === 'c' || e.key === 'C') { e.preventDefault(); continueAgent(); }
    };
    document.addEventListener('keydown', actionHotkeyListener);
  }

  /**
   * Read an SSE stream and dispatch events to the chat UI.
   * Used by both sendChatMessage and confirmAction.
   * @param {ReadableStreamDefaultReader} reader
   * @param {HTMLElement|null} assistantMsg - container for streamed tokens (created lazily if null)
   * @returns {Promise<{fullText: string, assistantMsg: HTMLElement|null}>}
   */
  async function processSSEStream(reader, assistantMsg) {
    const history = document.getElementById('chat_history');
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch(e) { continue; }

        if (evt.status) {
          // Lazily create assistant message container for continuation events
          if (!assistantMsg) {
            assistantMsg = document.createElement('div');
            assistantMsg.className = 'chat-msg chat-msg-assistant chat-msg-streaming';
            assistantMsg.textContent = '';
            history.appendChild(assistantMsg);
          }
          clearInterval(statusTimerInterval);
          statusStartTime = Date.now();
          assistantMsg.innerHTML = '';
          assistantMsg.classList.add('chat-msg-status');
          const statusText = document.createElement('span');
          statusText.textContent = evt.status;
          const timerSpan = document.createElement('span');
          timerSpan.className = 'chat-status-timer';
          timerSpan.textContent = ' (0s)';
          assistantMsg.appendChild(statusText);
          assistantMsg.appendChild(timerSpan);
          statusTimerInterval = setInterval(() => {
            const elapsed = Math.floor((Date.now() - statusStartTime) / 1000);
            timerSpan.textContent = ` (${elapsed}s)`;
          }, 1000);
          scrollHistory(history);
        }
        if (evt.activity) {
          if (!activityLog) {
            activityLog = document.createElement('div');
            activityLog.className = 'chat-activity-log';
            if (assistantMsg) {
              history.insertBefore(activityLog, assistantMsg);
            } else {
              history.appendChild(activityLog);
            }
          }
          const entry = document.createElement('div');
          entry.className = 'chat-activity-entry';
          entry.textContent = evt.activity;
          activityLog.appendChild(entry);
          activityLog.scrollTop = activityLog.scrollHeight;  // pin inner log to newest
          scrollHistory(history);
        }
        if (evt.token) {
          if (!assistantMsg) {
            assistantMsg = document.createElement('div');
            assistantMsg.className = 'chat-msg chat-msg-assistant chat-msg-streaming';
            assistantMsg.textContent = '';
            history.appendChild(assistantMsg);
          }
          clearInterval(statusTimerInterval);
          statusTimerInterval = null;
          assistantMsg.classList.remove('chat-msg-status');
          fullText += evt.token;
          assistantMsg.textContent = fullText;
          scrollHistory(history);
        }
        if (evt.retract) {
          fullText = '';
          if (assistantMsg) {
            assistantMsg.textContent = '';
            assistantMsg.classList.remove('chat-msg-status');
          }
        }
        if (evt.sources) {
          appendSources(evt.sources);
        }
        if (evt.action_proposed) {
          appendActionCard(evt.action_proposed);
        }
        if (evt.artifact && evt.artifact.type === 'image' && evt.artifact.data) {
          // A figure produced by the agent's run_python tool. Rendered as a
          // base64 data: URL - same as the inline cell-output path; nothing is
          // written to disk.
          const history = document.getElementById('chat_history');
          if (history) {
            const fig = document.createElement('div');
            fig.className = 'chat-msg chat-msg-assistant chat-artifact';
            const img = document.createElement('img');
            img.src = 'data:image/png;base64,' + evt.artifact.data;
            img.alt = 'Generated figure';
            fig.appendChild(img);
            history.appendChild(fig);
            scrollHistory(history);
          }
        }
        if (evt.max_steps) {
          appendContinueCard();
        }
        if (evt.stream_error) {
          appendContinueCard('The model returned a malformed response',
            'The assistant’s reply could not be parsed after several retries. ' +
            'Continue to try again.');
        }
        if (evt.action_executed) {
          appendActionResult(evt.action_executed);
        }
        if (evt.action_rejected) {
          appendMessage('system', '<em>Action cancelled.</em>');
        }
        if (evt.done) {
          clearInterval(statusTimerInterval);
          statusTimerInterval = null;
          chatSessionId = evt.session_id;
          showResetButton();
        }
        if (evt.error) {
          clearInterval(statusTimerInterval);
          statusTimerInterval = null;
          if (assistantMsg) {
            assistantMsg.classList.remove('chat-msg-streaming');
            assistantMsg.innerHTML = '<em>Error: ' + evt.error + '</em>';
          } else {
            appendMessage('system', '<em>Error: ' + evt.error + '</em>');
          }
        }
      }
    }

    return { fullText, assistantMsg };
  }

  async function confirmAction(confirmed) {
    if (!chatSessionId) return;
    // Remove hotkey listener
    if (actionHotkeyListener) {
      document.removeEventListener('keydown', actionHotkeyListener);
      actionHotkeyListener = null;
    }
    // Disable and remove action buttons immediately
    document.querySelectorAll('.chat-action-btn').forEach(b => b.disabled = true);
    document.querySelectorAll('.chat-action-buttons').forEach(el => el.remove());

    try {
      const resp = await fetch('/api/chat/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: chatSessionId,
          confirmed: confirmed
        })
      });

      // Now an SSE stream - may include continuation events after confirmation
      const reader = resp.body.getReader();
      const { fullText, assistantMsg } = await processSSEStream(reader, null);

      if (fullText && assistantMsg) {
        await renderMarkdown(fullText, assistantMsg);
      }
    } catch(e) {
      appendMessage('system', '<em>Confirmation failed: ' + e.message + '</em>');
    }

    activityLog = null;
  }

  window.confirmAction = confirmAction;

  async function continueAgent() {
    if (!chatSessionId) return;
    // Remove hotkey listener
    if (actionHotkeyListener) {
      document.removeEventListener('keydown', actionHotkeyListener);
      actionHotkeyListener = null;
    }
    // Disable and remove the Continue button immediately
    document.querySelectorAll('.chat-action-btn').forEach(b => b.disabled = true);
    document.querySelectorAll('.chat-continue-card .chat-action-buttons').forEach(el => el.remove());

    try {
      const resp = await fetch('/api/chat/continue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: chatSessionId })
      });

      // Reuses the same SSE rendering path as the initial send / confirm flows
      const reader = resp.body.getReader();
      const { fullText, assistantMsg } = await processSSEStream(reader, null);

      if (fullText && assistantMsg) {
        await renderMarkdown(fullText, assistantMsg);
      }
    } catch(e) {
      appendMessage('system', '<em>Continue failed: ' + e.message + '</em>');
    }

    activityLog = null;
  }

  window.continueAgent = continueAgent;

  /**
   * Copy text to the system clipboard.
   * navigator.clipboard is undefined on insecure origins (reaching the wiki over
   * plain http:// from another machine), so fall back to an off-screen textarea.
   * @returns {Promise<boolean>} true when the copy succeeded
   */
  async function copyTextToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch(e) { /* fall through to the textarea path */ }
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  /**
   * Append the hover-revealed control row to a finished assistant reply.
   * Copies container.dataset.markdown, which renderMarkdown stashes before the
   * HTML swap discards the raw text.
   */
  function attachCopyButton(container) {
    if (container.querySelector(':scope > .chat-msg-tools')) return;
    const tools = document.createElement('div');
    tools.className = 'chat-msg-tools';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-copy-btn btn-ghost btn-sm';
    btn.title = 'Copy markdown';
    btn.setAttribute('aria-label', 'Copy markdown');
    btn.textContent = '📋';
    btn.addEventListener('click', async function() {
      const ok = await copyTextToClipboard(container.dataset.markdown || '');
      btn.textContent = ok ? '✅' : '❌';
      setTimeout(function() { btn.textContent = '📋'; }, 1500);
    });
    tools.appendChild(btn);
    container.appendChild(tools);
  }

  async function renderMarkdown(text, container) {
    // Stash the source before the HTML swap destroys it - the copy button reads
    // it back from here. Set before the fetch so the failure path keeps it too.
    container.dataset.markdown = text;
    // Convert final markdown response to HTML via the existing API
    const formData = new FormData();
    formData.append('markdown', text);
    formData.append('format_code', true);
    if (urlPath) {
      formData.append('document_name', urlPath);
    } else if (vault) {
      // Global/wiki chat has no specific document, but the answer's wikilinks must
      // still resolve within the scoped vault -- not DEFAULT_VAULT. Pass a vault-only
      // document_name (from_url_with_vault parses the vault, empty doc path) so
      // /api/markdown/ builds hrefs as /wiki/{vault}/... instead of /wiki/main/...
      formData.append('document_name', 'wiki/' + vault);
    }
    try {
      const resp = await fetch('/api/markdown/', { method: 'POST', body: formData });
      if (resp.ok) {
        container.innerHTML = await resp.text();
        container.classList.remove('chat-msg-streaming');
        // Typeset any math in the freshly injected response (shared config
        // in math.js; self-guards when KaTeX isn't loaded on the page).
        if (window.tzaraRenderMath) window.tzaraRenderMath(container);
        var history = document.getElementById('chat_history');
        if (history) scrollHistory(history);
      }
    } catch(e) {
      // Keep raw text on failure
    }
    // User bubbles route through here too (sendChatMessage), so gate on role.
    if (container.classList.contains('chat-msg-assistant')) attachCopyButton(container);
  }

  async function sendChatMessage() {
    const input = document.getElementById('chat_input');
    if (!input) return;
    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    input.style.height = '';  // drop the inline height so it falls back to the rows=2 default
    const sendBtn = document.getElementById('chat_send_btn');
    if (sendBtn) sendBtn.disabled = true;

    // Show user message (rendered as markdown)
    const history = document.getElementById('chat_history');
    const userMsg = document.createElement('div');
    userMsg.className = 'chat-msg chat-msg-user chat-msg-streaming';
    userMsg.textContent = message;
    history.appendChild(userMsg);
    scrollHistory(history);
    renderMarkdown(message, userMsg);

    // Create assistant message placeholder
    const assistantMsg = document.createElement('div');
    assistantMsg.className = 'chat-msg chat-msg-assistant chat-msg-streaming';
    assistantMsg.textContent = '';
    history.appendChild(assistantMsg);

    try {
      const resp = await fetch('/api/chat/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: message,
          session_id: chatSessionId,
          document_url_path: urlPath,
          vault: vault,
          mode: getChatMode(),
          // Exact kernel key for the run_python tool: the page the chat panel is
          // open on shares its kernel with this same pathname (see jupyter.js).
          // Deliberately revision-free - the kernel is shared across revisions.
          page_id: window.location.pathname,
          // Makes the chat's working copy the version on screen, not HEAD.
          revision: currentRevision
        })
      });

      const reader = resp.body.getReader();
      const { fullText } = await processSSEStream(reader, assistantMsg);

      // Final render: convert markdown to HTML
      if (fullText) {
        await renderMarkdown(fullText, assistantMsg);
      }

    } catch(e) {
      clearInterval(statusTimerInterval);
      statusTimerInterval = null;
      assistantMsg.classList.remove('chat-msg-streaming');
      assistantMsg.innerHTML = '<em>Connection error: ' + e.message + '</em>';
    }

    activityLog = null;
    if (sendBtn) sendBtn.disabled = false;
    // Don't steal focus from the Allow button when an action is pending
    if (!actionHotkeyListener) input.focus();
  }

  async function resetChat() {
    if (chatSessionId) {
      try {
        await fetch('/api/chat/reset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: chatSessionId })
        });
      } catch(e) {}
    }
    chatSessionId = null;
    activityLog = null;
    clearInterval(statusTimerInterval);
    statusTimerInterval = null;
    const history = document.getElementById('chat_history');
    if (history) history.innerHTML = '';
    const resetBtn = document.getElementById('chat_reset_btn');
    if (resetBtn) resetBtn.classList.add('chat-hidden');
  }

  // Expose to DOM
  window.sendChatMessage = sendChatMessage;
  window.resetChat = resetChat;

  // Allow Enter to send (Shift+Enter for newline)
  const input = document.getElementById('chat_input');
  if (input) {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });
    // The whole row (textarea + send button) we keep on screen when the box
    // grows -- so its bottom edge and the Ask button never slip below the fold.
    var inputRow = input.closest('.chat-input-row');
    // Both constant, so read once. borderY: .chat-input is border-box and
    // scrollHeight excludes the border, so assigning height = scrollHeight would
    // leave the content 2px short and show a permanent scrollbar; add it back.
    // maxH: past this the box scrolls internally -- we must clamp to it (below).
    var _cs = getComputedStyle(input);
    var borderY = parseFloat(_cs.borderTopWidth) + parseFloat(_cs.borderBottomWidth);
    var maxH = parseFloat(_cs.maxHeight);  // NaN if 'none'

    // Grow the textarea to fit its text, up to the CSS max-height (after which it
    // scrolls). Standard reset-to-auto then measure the real textarea, so
    // wrapping is always exact; the two style writes are synchronous with no
    // paint between them, so there's no visible collapse.
    function autoGrow() {
      var prev = input.style.height;
      input.style.height = 'auto';
      var h = input.scrollHeight + borderY;
      // Clamp to max-height. Without this, scrollHeight keeps growing past the
      // cap so `next` changes every keystroke -- firing the scroll below on each
      // one, which on Firefox fights the native caret-scroll and yanks the page.
      if (!isNaN(maxH) && h > maxH) h = maxH;
      var next = h + 'px';
      input.style.height = next;
      // Keep the row's bottom edge in view only when the box actually grew AND
      // the caret is at the end (the user is appending). When editing mid-text
      // we defer to the browser's native caret-scroll rather than compete with
      // it -- competing is what pulled Firefox to a mid-text cursor.
      
      // inputRow.scrollIntoView({ block: 'nearest' });
      inputRow.scrollIntoView({ block: "nearest", inline:"nearest" });
      
      // if (next !== prev && inputRow &&
      //     input.selectionStart === input.value.length) {
      //   inputRow.scrollIntoView({ block: 'nearest' });
      // }
    }
    input.addEventListener('input', autoGrow);
  }
}
