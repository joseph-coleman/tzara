// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// jupyter.js - Jupyter cell execution with ipywidget support

(function() {
    'use strict';

    // ===== Page-level persistent WebSocket =====
    let pageSocket = null;
    let socketReady = null; // Promise that resolves when connected
    const pendingExecutions = {}; // cell_id -> {button, outputArea, originalText}

    // ===== CodeMirror instance tracking =====
    const cmInstances = new Map(); // container element → { view, cmContainer, themeComp }
    const cmDarkQuery = window.matchMedia("(prefers-color-scheme: dark)");
    function cmGetTheme() {
        return (typeof CMEditor !== 'undefined' && cmDarkQuery.matches)
            ? CMEditor.oneDark
            : (typeof CMEditor !== 'undefined' ? CMEditor.EditorView.baseTheme({}) : null);
    }
    cmDarkQuery.addEventListener("change", () => {
        if (typeof CMEditor === 'undefined') return;
        for (const { view, themeComp } of cmInstances.values()) {
            view.dispatch({ effects: themeComp.reconfigure(cmGetTheme()) });
        }
    });

    // ===== HTML Script Execution Helper =====
    // Scripts inserted via insertAdjacentHTML don't execute.
    // This function activates them by cloning into fresh <script> elements.
    // External scripts (with src) are loaded sequentially so that later
    // inline scripts can depend on them (e.g. Plotly CDN then Plotly.newPlot).
    function activateScripts(container) {
        var scripts = Array.from(container.querySelectorAll('script:not([data-activated])'));
        if (scripts.length === 0) return;
        function processNext(index) {
            if (index >= scripts.length) return;
            var oldScript = scripts[index];
            oldScript.setAttribute('data-activated', '1');
            var newScript = document.createElement('script');
            for (var j = 0; j < oldScript.attributes.length; j++) {
                newScript.setAttribute(oldScript.attributes[j].name, oldScript.attributes[j].value);
            }
            newScript.textContent = oldScript.textContent;
            if (newScript.src) {
                newScript.onload = function() { processNext(index + 1); };
                newScript.onerror = function() { processNext(index + 1); };
            }
            oldScript.parentNode.replaceChild(newScript, oldScript);
            if (!newScript.src) {
                processNext(index + 1);
            }
        }
        // mpld3 output is not self-contained: its inline script fetches d3 +
        // mpld3 from external CDNs at render time (fragile, breaks offline and
        // when the version-locked CDN .js lags PyPI). Detect it and make sure
        // the vendored libs are global first, so mpld3's loader takes its
        // synchronous "already loaded" fast path instead of hitting the network.
        var needsMpld3 = scripts.some(function(s) {
            return s.textContent && s.textContent.indexOf('mpld3.draw_figure') !== -1;
        }) && !(window.mpld3 && window.mpld3._mpld3IsLoaded);
        if (needsMpld3) { ensureMpld3Loaded().then(function() { processNext(0); }); return; }
        processNext(0);
    }

    // ===== KaTeX typesetting for freshly-inserted output =====
    // Cell output arrives after page load, so the initial KaTeX pass never
    // touched it. Re-typeset the just-inserted subtree using the shared
    // bootstrap (window.tzaraRenderMath, from math.js). It self-guards when
    // KaTeX/math.js aren't present, so this is a safe no-op on pages without
    // math loaded. Called alongside activateScripts at every HTML-insert site.
    function renderMathIn(container) {
        if (window.tzaraRenderMath) window.tzaraRenderMath(container);
    }

    // ===== Vendored mpld3 / d3 loading (no CDN) =====
    // Loads the committed d3 + mpld3 bundles once, on first mpld3 output.
    // d3 (UMD) must set window.d3 before mpld3 loads, hence the nested load.
    var _mpld3Ready = null;
    function ensureMpld3Loaded() {
        if (_mpld3Ready) return _mpld3Ready;
        _mpld3Ready = new Promise(function(resolve) {
            if (window.mpld3 && window.mpld3._mpld3IsLoaded) return resolve();
            function load(src, cb) {
                var s = document.createElement('script');
                s.src = src;
                s.onload = cb;
                s.onerror = cb;
                document.head.appendChild(s);
            }
            load('/template/default/d3.v5.min.js', function() {
                load('/template/default/mpld3.v0.5.12.min.js', resolve);
            });
        });
        return _mpld3Ready;
    }

    // ===== Widget Model Registry =====
    const widgetModels = {}; // comm_id -> {comm_id, model_name, state, views[]}

    // ===== anywidget ESM Loading & CSS Injection =====
    var _esmModuleCache = new Map();  // hash -> Promise<Module>
    var _injectedCSSHashes = new Set();

    function _simpleHash(str) {
        var hash = 5381;
        for (var i = 0; i < str.length; i++) {
            hash = ((hash << 5) + hash + str.charCodeAt(i)) & 0xffffffff;
        }
        return hash.toString(36);
    }

    function loadAnyWidgetESM(esmSource) {
        var hash = _simpleHash(esmSource);
        if (_esmModuleCache.has(hash)) return _esmModuleCache.get(hash);
        var promise = (function() {
            var blob = new Blob([esmSource], { type: 'application/javascript' });
            var url = URL.createObjectURL(blob);
            return import(url).finally(function() { URL.revokeObjectURL(url); });
        })();
        _esmModuleCache.set(hash, promise);
        return promise;
    }

    function injectAnyWidgetCSS(cssSource) {
        var hash = _simpleHash(cssSource);
        if (_injectedCSSHashes.has(hash)) return;
        _injectedCSSHashes.add(hash);
        var style = document.createElement('style');
        style.setAttribute('data-anywidget-css', hash);
        style.textContent = '@layer widgets {\n' + cssSource + '\n}';
        document.head.appendChild(style);
    }

    function getOrCreateSocket() {
        if (socketReady) return socketReady;

        socketReady = new Promise((resolve, reject) => {
            const protocol = window.location.protocol === "https:" ? "wss" : "ws";
            const ws = new WebSocket(
                `${protocol}://${window.location.host}/ws/run_jupyter`
            );

            ws.onopen = () => {
                ws.send(JSON.stringify({
                    action: "connect",
                    page_id: window.location.pathname
                }));
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);

                if (data.status === "connected") {
                    pageSocket = ws;
                    resolve(ws);
                    return;
                }

                handleServerMessage(data);
            };

            ws.onerror = (err) => {
                socketReady = null;
                reject(err);
            };

            ws.onclose = () => {
                pageSocket = null;
                socketReady = null;
                // The browser-facing socket has dropped (typically because
                // the kernel was idle-culled and the server invalidated
                // its end). Any cell whose run() is mid-flight will never
                // receive its execution_complete frame, so re-enable each
                // stuck button and tell the user what happened. The next
                // click will reopen the socket and spawn a fresh kernel.
                // Also mark every rendered widget output as disconnected
                // for the case where this close happens without a prior
                // kernel_dead push (e.g. server crash).
                markJupyterOutputsKernelDead();
                var stuckIds = Object.keys(pendingExecutions);
                for (var i = 0; i < stuckIds.length; i++) {
                    var cellId = stuckIds[i];
                    var exec = pendingExecutions[cellId];
                    delete pendingExecutions[cellId];
                    if (!exec) continue;
                    if (exec.outputArea) {
                        exec.outputArea.insertAdjacentHTML(
                            "beforeend",
                            '<pre class="jupyter-kernel-restart-notice">' +
                            '[Kernel restarted - variables and widget state were cleared. Click run again to continue.]' +
                            '</pre>'
                        );
                    }
                    if (exec.button) {
                        exec.button.disabled = false;
                        exec.button.innerText = exec.originalText;
                        exec.button.title = "Run";
                    }
                }
            };
        });

        return socketReady;
    }

    // ===== Dead-kernel indicator =====
    // The kernel can be culled by Jupyter's idle reaper while the page sits
    // idle. Any rendered widget (ipywidget button, anywidget instance,
    // interactive plot) on the page then silently fails on click. This
    // helper prepends a small banner to every visible jupyter-output so the
    // user sees an explicit "disconnected" notice anchored next to the
    // orphaned content. It is idempotent - runJupyterCode() wipes
    // outputArea.innerHTML on each run, so the banner clears naturally
    // when the user kicks the kernel back to life.
    function markJupyterOutputsKernelDead() {
        var outputs = document.querySelectorAll(".jupyter-output");
        for (var i = 0; i < outputs.length; i++) {
            var out = outputs[i];
            if (out.style.display === "none") continue;
            if (out.querySelector(":scope > .kernel-dead-banner")) continue;
            var banner = document.createElement("div");
            banner.className = "kernel-dead-banner";
            banner.textContent =
                "Kernel disconnected - run a cell to restart.";
            out.insertBefore(banner, out.firstChild);
        }
    }

    // ===== Message Router =====

    function handleServerMessage(data) {
        // Proactive dead-kernel notice from the server (listener exit or
        // comm_msg send failure). Mark every visible output so users see
        // the indicator even on cells that aren't mid-execution. The
        // execute-path failure rides along with execution_complete and is
        // handled below so the button is also restored - skip it here.
        if (data.kernel_dead && !data.execution_complete) {
            markJupyterOutputsKernelDead();
            return;
        }


        // Execution output (html)
        if (data.html !== undefined && data.cell_id) {
            const exec = pendingExecutions[data.cell_id];
            if (exec) {
                exec.outputArea.insertAdjacentHTML('beforeend', data.html);
                activateScripts(exec.outputArea);
                renderMathIn(exec.outputArea);
                exec.outputArea.scrollTop = exec.outputArea.scrollHeight;
            }
            return;
        }

        // Input request (Python input())
        if (data.input_request && data.cell_id) {
            const exec = pendingExecutions[data.cell_id];
            if (exec) {
                showInputPrompt(exec.outputArea, data.prompt || "", data.password || false);
            }
            return;
        }

        // Widget view request
        if (data.widget_view && data.cell_id) {
            const modelId = data.widget_view.model_id;
            const exec = pendingExecutions[data.cell_id];
            if (exec) {
                renderWidgetView(modelId, exec.outputArea);
            }
            return;
        }

        // Execution complete
        if (data.execution_complete && data.cell_id) {
            const exec = pendingExecutions[data.cell_id];
            if (exec) {
                if (data.error === "kernel_unavailable" && exec.outputArea) {
                    exec.outputArea.insertAdjacentHTML(
                        "beforeend",
                        '<pre class="jupyter-kernel-restart-notice">' +
                        '[Kernel restarted - variables and widget state were cleared. Click run again to continue.]' +
                        '</pre>'
                    );
                }
                if (data.kernel_dead) {
                    markJupyterOutputsKernelDead();
                }
                exec.button.disabled = false;
                exec.button.innerText = exec.originalText;
                exec.button.title = "Run";
            }
            delete pendingExecutions[data.cell_id];
            return;
        }

        // Kernel comm messages (widget protocol)
        if (data.kernel_msg) {
            handleKernelMessage(data.kernel_msg, data.source_comm_id || "", data.buffers_base64 || null);
            return;
        }
    }

    // ===== Input Prompt (Python input()) =====

    function showInputPrompt(outputArea, prompt, isPassword) {
        var wrapper = document.createElement("div");
        wrapper.className = "jupyter-input-prompt";

        if (prompt) {
            var promptSpan = document.createElement("span");
            promptSpan.className = "jupyter-input-label";
            promptSpan.textContent = prompt;
            wrapper.appendChild(promptSpan);
        }

        var input = document.createElement("input");
        input.type = isPassword ? "password" : "text";
        input.className = "jupyter-input-field";

        var submitBtn = document.createElement("button");
        submitBtn.textContent = "Submit";
        submitBtn.className = "jupyter-input-submit";

        function submitInput() {
            var value = input.value;
            if (pageSocket && pageSocket.readyState === WebSocket.OPEN) {
                pageSocket.send(JSON.stringify({
                    action: "input_reply",
                    value: value
                }));
            }
            // Replace input form with a static record (like a terminal)
            var record = document.createElement("div");
            record.className = "jupyter-input-record";
            var recordPrompt = document.createElement("span");
            recordPrompt.textContent = prompt;
            var recordValue = document.createElement("span");
            recordValue.textContent = isPassword ? "****" : value;
            recordValue.className = "jupyter-input-value";
            record.appendChild(recordPrompt);
            record.appendChild(recordValue);
            wrapper.replaceWith(record);
        }

        input.addEventListener("keydown", function(e) {
            if (e.key === "Enter") {
                e.preventDefault();
                submitInput();
            }
        });
        submitBtn.addEventListener("click", submitInput);

        wrapper.appendChild(input);
        wrapper.appendChild(submitBtn);
        outputArea.appendChild(wrapper);
        outputArea.scrollTop = outputArea.scrollHeight;
        input.focus();
    }

    // ===== Widget Comm Protocol =====

    // Encode an ArrayBuffer (or TypedArray) into a base64 string.
    // Uses 32KiB chunks to avoid call-stack overflow on large files.
    function arrayBufferToBase64(ab) {
        var bytes = ab instanceof Uint8Array ? ab : new Uint8Array(ab.buffer ? ab.buffer : ab);
        var binary = "", chunk = 0x8000;
        for (var i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        return btoa(binary);
    }

    function encodeBuffersToBase64(buffers) {
        var out = [];
        for (var i = 0; i < buffers.length; i++) out.push(arrayBufferToBase64(buffers[i]));
        return out;
    }

    // Reconstitute binary buffers into the state object.
    // ipywidgets extracts binary values from state and sends them as separate
    // buffers, recording their original location in buffer_paths.
    // e.g. buffer_paths=[["value"]] means buffers[0] belongs at state.value.
    function reconstitueBuffers(state, bufferPaths, buffersBase64) {
        for (var i = 0; i < bufferPaths.length && i < buffersBase64.length; i++) {
            var path = bufferPaths[i];
            var target = state;
            for (var j = 0; j < path.length - 1; j++) {
                if (target[path[j]] == null) target[path[j]] = {};
                target = target[path[j]];
            }
            if (target && path.length > 0) {
                target[path[path.length - 1]] = buffersBase64[i];
            }
        }
    }

    function handleKernelMessage(msg, sourceCommId, buffersBase64) {
        const msgType = msg.msg_type;
        const content = msg.content;

        if (msgType === "comm_open") {
            const commId = content.comm_id;
            const targetName = content.target_name;

            if (targetName !== "jupyter.widget.comm" &&
                targetName !== "jupyter.widget") return;

            const state = (content.data && content.data.state) || {};

            // Reconstitute binary buffers into state (e.g. ImageModel value)
            var bufferPaths = (content.data && content.data.buffer_paths) || [];
            if (buffersBase64 && bufferPaths.length > 0) {
                reconstitueBuffers(state, bufferPaths, buffersBase64);
            }

            widgetModels[commId] = {
                comm_id: commId,
                model_name: state._model_name || "",
                model_module: state._model_module || "",
                state: state,
                views: []
            };

            // Auto-setup jslink/jsdlink when Link models are created
            var modelName = state._model_name || "";
            if (modelName === "LinkModel" || modelName === "DirectionalLinkModel") {
                setupLink(widgetModels[commId]);
            }
        }

        else if (msgType === "comm_msg") {
            const commId = content.comm_id;
            const method = content.data && content.data.method;

            if (method === "update") {
                const model = widgetModels[commId];
                if (!model) return;

                const newState = (content.data && content.data.state) || {};
                Object.assign(model.state, newState);

                // Reconstitute binary buffers into state
                var bufferPaths = (content.data && content.data.buffer_paths) || [];
                if (buffersBase64 && bufferPaths.length > 0) {
                    reconstitueBuffers(model.state, bufferPaths, buffersBase64);
                }

                // Fire anywidget change events (ESM listeners handle their own DOM updates)
                if (model._anywidgetWrapper) {
                    model._anywidgetWrapper._fireChange(Object.keys(newState));
                }

                // Re-render all views of this model
                for (let i = 0; i < model.views.length; i++) {
                    updateWidgetView(model, model.views[i]);
                }

                // Propagate jslink/jsdlink for kernel-initiated changes
                propagateLinks(commId);
            }
            else if (method === "custom") {
                const model = widgetModels[commId];
                if (model) {
                    handleCustomMessage(model, content.data.content, buffersBase64);
                }
            }
        }

        else if (msgType === "comm_close") {
            const commId = content.comm_id;
            const model = widgetModels[commId];
            if (model) {
                // Clean up any jslink/jsdlink referencing this widget
                teardownLink(commId);
                for (let i = 0; i < model.views.length; i++) {
                    if (model.views[i]._anywidgetCleanup) {
                        try { model.views[i]._anywidgetCleanup(); } catch(e) { console.warn("anywidget view cleanup error:", e); }
                    }
                    model.views[i].remove();
                }
                if (model._anywidgetWrapper && model._anywidgetWrapper._initCleanup) {
                    try { model._anywidgetWrapper._initCleanup(); } catch(e) { console.warn("anywidget init cleanup error:", e); }
                }
                delete widgetModels[commId];
            }
        }

        // Output messages forwarded from widget callbacks (e.g. button on_click).
        // Route to any Output widget whose msg_id matches the parent_header.
        // If no Output widget captures it, route to the cell containing the
        // source widget (orphaned output from print/display outside 'with out:').
        else if (msgType === "stream" || msgType === "display_data" ||
                 msgType === "execute_result" || msgType === "error") {
            var parentMsgId = (msg.parent_header && msg.parent_header.msg_id) || "";
            var captured = false;
            if (parentMsgId) {
                for (var cid in widgetModels) {
                    var m = widgetModels[cid];
                    if (m.model_name === "OutputModel" && m.state.msg_id === parentMsgId) {
                        appendOutputToWidget(m, msg);
                        captured = true;
                        break;
                    }
                }
            }
            if (!captured && sourceCommId) {
                routeOrphanedOutput(sourceCommId, msg);
            }
        }

        else if (msgType === "clear_output") {
            var parentMsgId = (msg.parent_header && msg.parent_header.msg_id) || "";
            if (parentMsgId) {
                for (var cid in widgetModels) {
                    var m = widgetModels[cid];
                    if (m.model_name === "OutputModel" && m.state.msg_id === parentMsgId) {
                        for (var vi = 0; vi < m.views.length; vi++) {
                            m.views[vi].innerHTML = "";
                        }
                        break;
                    }
                }
            }
        }
    }

    function sendCommMsg(commId, data, buffers) {
        if (!pageSocket || pageSocket.readyState !== WebSocket.OPEN) return;

        var envelope = {
            action: "comm_msg",
            content: {
                comm_id: commId,
                data: data
            }
        };
        if (buffers && buffers.length > 0) {
            envelope.buffers_base64 = encodeBuffersToBase64(buffers);
        }
        pageSocket.send(JSON.stringify(envelope));
    }

    function sendStateUpdate(commId, stateUpdates, bufferPaths, buffers) {
        sendCommMsg(commId, {
            method: "update",
            state: stateUpdates,
            buffer_paths: bufferPaths || []
        }, buffers);
        // Propagate jslink/jsdlink after any user-initiated state change
        propagateLinks(commId);
    }

    function syncOtherViews(model, currentViewEl) {
        for (var i = 0; i < model.views.length; i++) {
            if (model.views[i] !== currentViewEl) {
                updateWidgetView(model, model.views[i]);
            }
        }
    }

    // ===== jslink / jsdlink (LinkModel / DirectionalLinkModel) =====

    var _activeLinks = [];
    var _linkPropagating = false;

    function setupLink(model) {
        // state.source = ["IPY_MODEL_xxx", "traitName"]
        // state.target = ["IPY_MODEL_yyy", "traitName"]
        var source = model.state.source;
        var target = model.state.target;
        if (!source || !target || source.length < 2 || target.length < 2) return;

        var sourceId = source[0].replace("IPY_MODEL_", "");
        var sourceTrait = source[1];
        var targetId = target[0].replace("IPY_MODEL_", "");
        var targetTrait = target[1];
        var bidir = (model.model_name === "LinkModel");

        _activeLinks.push({
            linkCommId: model.comm_id,
            sourceId: sourceId,
            sourceTrait: sourceTrait,
            targetId: targetId,
            targetTrait: targetTrait,
            bidirectional: bidir
        });

        // Initial sync: copy source value to target
        var sourceModel = widgetModels[sourceId];
        var targetModel = widgetModels[targetId];
        if (sourceModel && targetModel && sourceModel.state[sourceTrait] !== undefined) {
            targetModel.state[targetTrait] = sourceModel.state[sourceTrait];
            for (var i = 0; i < targetModel.views.length; i++) {
                updateWidgetView(targetModel, targetModel.views[i]);
            }
        }
    }

    function teardownLink(commId) {
        _activeLinks = _activeLinks.filter(function(l) { return l.linkCommId !== commId; });
    }

    function propagateLinks(changedCommId) {
        if (_linkPropagating || _activeLinks.length === 0) return;
        _linkPropagating = true;
        try {
            for (var i = 0; i < _activeLinks.length; i++) {
                var link = _activeLinks[i];

                // Source changed → update target
                if (link.sourceId === changedCommId) {
                    var src = widgetModels[link.sourceId];
                    var tgt = widgetModels[link.targetId];
                    if (src && tgt) {
                        var val = src.state[link.sourceTrait];
                        if (tgt.state[link.targetTrait] !== val) {
                            tgt.state[link.targetTrait] = val;
                            sendCommMsg(link.targetId, {
                                method: "update",
                                state: {[link.targetTrait]: val},
                                buffer_paths: []
                            });
                            for (var j = 0; j < tgt.views.length; j++) {
                                updateWidgetView(tgt, tgt.views[j]);
                            }
                        }
                    }
                }

                // Target changed → update source (bidirectional only)
                if (link.bidirectional && link.targetId === changedCommId) {
                    var src = widgetModels[link.sourceId];
                    var tgt = widgetModels[link.targetId];
                    if (src && tgt) {
                        var val = tgt.state[link.targetTrait];
                        if (src.state[link.sourceTrait] !== val) {
                            src.state[link.sourceTrait] = val;
                            sendCommMsg(link.sourceId, {
                                method: "update",
                                state: {[link.sourceTrait]: val},
                                buffer_paths: []
                            });
                            for (var j = 0; j < src.views.length; j++) {
                                updateWidgetView(src, src.views[j]);
                            }
                        }
                    }
                }
            }
        } finally {
            _linkPropagating = false;
        }
    }

    // ===== anywidget Model Wrapper =====

    function AnyWidgetModelWrapper(commId) {
        this._commId = commId;
        this._listeners = {};
        this._changed = {};
        this._ready = false;
        this._pendingChanges = [];
        this._initCleanup = null;
        this._initialized = false;
        this._savingChanges = false;
        this._firing = false;

        var self = this;
        this.widget_manager = {
            get_model: function(modelId) {
                var id = modelId.replace("IPY_MODEL_", "");
                var m = widgetModels[id];
                if (m) {
                    if (!m._anywidgetWrapper) {
                        m._anywidgetWrapper = new AnyWidgetModelWrapper(id);
                        m._anywidgetWrapper._ready = true;
                    }
                    return Promise.resolve(m._anywidgetWrapper);
                }
                return Promise.reject(new Error("Model not found: " + modelId));
            }
        };
    }

    AnyWidgetModelWrapper.prototype.get = function(key) {
        var model = widgetModels[this._commId];
        return model ? model.state[key] : undefined;
    };

    AnyWidgetModelWrapper.prototype.set = function(key, value) {
        var model = widgetModels[this._commId];
        if (!model) return;
        model.state[key] = value;
        this._changed[key] = value;
        // Fire change events immediately (matches ipywidgets Backbone.js behavior)
        if (this._ready) {
            this._fireChange([key]);
        }
    };

    AnyWidgetModelWrapper.prototype.save_changes = function() {
        if (this._savingChanges) return;
        this._savingChanges = true;
        try {
            var updates = {};
            var hasChanges = false;
            for (var key in this._changed) {
                updates[key] = this._changed[key];
                hasChanges = true;
            }
            this._changed = {};
            if (hasChanges) {
                sendStateUpdate(this._commId, updates);
            }
        } finally {
            this._savingChanges = false;
        }
    };

    AnyWidgetModelWrapper.prototype.on = function(event, callback) {
        if (!this._listeners[event]) this._listeners[event] = [];
        this._listeners[event].push(callback);
    };

    AnyWidgetModelWrapper.prototype.off = function(event, callback) {
        if (!this._listeners[event]) return;
        if (!callback) { delete this._listeners[event]; return; }
        this._listeners[event] = this._listeners[event].filter(function(cb) { return cb !== callback; });
    };

    AnyWidgetModelWrapper.prototype.send = function(content, callbacks, buffers) {
        sendCommMsg(this._commId, { method: "custom", content: content }, buffers);
    };

    AnyWidgetModelWrapper.prototype._fireChange = function(keys) {
        if (!this._ready) {
            this._pendingChanges.push(keys);
            return;
        }
        if (this._firing) return;
        this._firing = true;
        try {
            for (var i = 0; i < keys.length; i++) {
                var listeners = this._listeners["change:" + keys[i]];
                if (listeners) {
                    for (var j = 0; j < listeners.length; j++) {
                        try { listeners[j](); } catch (e) { console.warn("anywidget change listener error:", e); }
                    }
                }
            }
            var genericListeners = this._listeners["change"];
            if (genericListeners && keys.length > 0) {
                for (var j = 0; j < genericListeners.length; j++) {
                    try { genericListeners[j](); } catch (e) { console.warn("anywidget change listener error:", e); }
                }
            }
        } finally {
            this._firing = false;
        }
    };

    AnyWidgetModelWrapper.prototype._fireCustomMessage = function(content, buffersBase64) {
        var listeners = this._listeners["msg:custom"];
        if (!listeners) return;
        var buffers = [];
        if (buffersBase64) {
            for (var i = 0; i < buffersBase64.length; i++) {
                var binary = atob(buffersBase64[i]);
                var bytes = new Uint8Array(binary.length);
                for (var k = 0; k < binary.length; k++) bytes[k] = binary.charCodeAt(k);
                buffers.push(bytes.buffer);
            }
        }
        for (var i = 0; i < listeners.length; i++) {
            try { listeners[i](content, buffers); } catch (e) { console.warn("anywidget custom message error:", e); }
        }
    };

    AnyWidgetModelWrapper.prototype._markReady = function() {
        this._ready = true;
        for (var i = 0; i < this._pendingChanges.length; i++) {
            this._fireChange(this._pendingChanges[i]);
        }
        this._pendingChanges = [];
    };

    // ===== Widget Renderers =====

    const RENDERERS = {
        "IntSliderModel":    renderIntSlider,
        "FloatSliderModel":  renderFloatSlider,
        "IntTextModel":      renderIntText,
        "FloatTextModel":    renderFloatText,
        "TextModel":         renderText,
        "TextareaModel":     renderTextarea,
        "DropdownModel":     renderDropdown,
        "CheckboxModel":     renderCheckbox,
        "ToggleButtonModel": renderToggleButton,
        "ButtonModel":       renderButton,
        "HTMLModel":         renderHTMLWidget,
        "HTMLMathModel":     renderHTMLWidget,
        "LabelModel":        renderLabel,
        "OutputModel":       renderOutput,
        "VBoxModel":         renderVBox,
        "HBoxModel":         renderHBox,
        "MPLCanvasModel":    renderMPLCanvas,
        "ToolbarModel":      renderMPLToolbar,
        "RadioButtonsModel": renderRadioButtons,
        "SelectModel":       renderSelect,
        "SelectMultipleModel": renderSelectMultiple,
        "ToggleButtonsModel": renderToggleButtons,
        "BoundedIntTextModel": renderBoundedIntText,
        "BoundedFloatTextModel": renderBoundedFloatText,
        "PasswordModel":     renderPassword,
        "ComboboxModel":     renderCombobox,
        "ColorPickerModel":  renderColorPicker,
        "DatePickerModel":   renderDatePicker,
        "DateModel":         renderDatePicker,
        "TimePickerModel":   renderTimePicker,
        "TimeModel":         renderTimePicker,
        "SelectionSliderModel": renderSelectionSlider,
        "IntRangeSliderModel": renderIntRangeSlider,
        "FloatRangeSliderModel": renderFloatRangeSlider,
        "FloatLogSliderModel": renderFloatLogSlider,
        "FileUploadModel":   renderFileUpload,
        "IntProgressModel":  renderIntProgress,
        "FloatProgressModel": renderFloatProgress,
        "ValidModel":        renderValid,
        "ImageModel":        renderImage,
        "VideoModel":        renderVideo,
        "AudioModel":        renderAudio,
        "PlayModel":         renderPlay,
        "TabModel":          renderTab,
        "AccordionModel":    renderAccordion,
        "StackModel":        renderStack,
        "GridBoxModel":      renderGridBox,
        "LinkModel":         renderLink,
        "DirectionalLinkModel": renderLink,
    };

    function renderWidgetView(modelId, parentEl, retries) {
        retries = retries || 0;
        const model = widgetModels[modelId];
        if (!model) {
            if (retries < 20) {
                setTimeout(function() { renderWidgetView(modelId, parentEl, retries + 1); }, 100);
            } else {
                parentEl.insertAdjacentHTML('beforeend', '<pre>[Widget model not found: ' + modelId + ']</pre>');
            }
            return;
        }

        const renderer = RENDERERS[model.model_name];
        if (!renderer) {
            if (model.model_module === "anywidget" && model.state._esm) {
                renderAnyWidget(model, parentEl);
                return;
            }
            parentEl.insertAdjacentHTML('beforeend', '<pre>[Unsupported widget: ' + model.model_name + ']</pre>');
            return;
        }

        const viewEl = renderer(model);
        viewEl.dataset.widgetModelId = modelId;
        viewEl.classList.add("tzara-widget");
        parentEl.appendChild(viewEl);
        model.views.push(viewEl);
    }

    async function renderAnyWidget(model, parentEl) {
        var container = document.createElement("div");
        container.className = "tzara-widget anywidget-container";
        container.dataset.widgetModelId = model.comm_id;
        parentEl.appendChild(container);
        container.textContent = "Loading widget\u2026";

        try {
            if (model.state._css) {
                injectAnyWidgetCSS(model.state._css);
            }

            if (!model._anywidgetWrapper) {
                model._anywidgetWrapper = new AnyWidgetModelWrapper(model.comm_id);
            }
            var wrapper = model._anywidgetWrapper;

            var esmModule = await loadAnyWidgetESM(model.state._esm);
            container.textContent = "";

            // anywidget supports two ESM export shapes:
            //   1. Named exports:  export function render(...) { ... }
            //   2. Default object: export default { render, initialize }
            // The module namespace exposes both; pick whichever defines render.
            var api = esmModule;
            if (typeof api.render !== "function" && api.default && typeof api.default.render === "function") {
                api = api.default;
            }
            if (typeof api.render !== "function") {
                throw new TypeError(
                    "anywidget ESM exports no render() function. Expected either " +
                    "`export function render(...)` or `export default { render }`."
                );
            }

            if (api.initialize && !wrapper._initialized) {
                wrapper._initialized = true;
                var initCleanup = await api.initialize({ model: wrapper });
                if (typeof initCleanup === "function") {
                    wrapper._initCleanup = initCleanup;
                }
            }

            var renderCleanup = await api.render({ model: wrapper, el: container });
            if (typeof renderCleanup === "function") {
                container._anywidgetCleanup = renderCleanup;
            }

            wrapper._markReady();
            model.views.push(container);
        } catch (e) {
            console.error("anywidget render error:", e);
            container.textContent = "";
            container.insertAdjacentHTML('beforeend',
                '<pre style="color:red;">[anywidget error: ' + (e.message || e) + ']</pre>');
        }
    }

    function updateWidgetView(model, viewEl) {
        if (model._anywidgetWrapper) return; // anywidget updates via change events
        const renderer = RENDERERS[model.model_name];
        if (renderer && renderer.update) {
            renderer.update(model, viewEl);
        }
    }

    // --- IntSlider ---
    function renderIntSlider(model) {
        console.log("Render Int Slider")
        console.log(model)
        var el = document.createElement("div");
        el.className = "widget-intslider";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = model.state.min != null ? model.state.min : 0;
        slider.max = model.state.max != null ? model.state.max : 100;
        slider.step = model.state.step != null ? model.state.step : 1;
        slider.value = model.state.value != null ? model.state.value : 0;
        slider.orientation = model.state.orientation != null ? model.state.orientation : "horizontal";
        if (model.state.disabled) slider.disabled = true;
        if (model.state.orientation=="vertical") {
            el.classList.add("widget-vertical")
        }
        console.log(slider.orientation);

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = slider.value;

        slider.addEventListener("input", function() {
            var val = parseInt(slider.value);
            readout.textContent = val;
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(slider);
        el.appendChild(readout);
        return el;
    }
    renderIntSlider.update = function(model, el) {
        var slider = el.querySelector("input[type=range]");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        if (slider && document.activeElement !== slider) slider.value = model.state.value;
        if (readout) readout.textContent = model.state.value;
        if (label) label.textContent = model.state.description || "";
        if (slider) slider.disabled = model.state.disabled || false;
    };

    // --- FloatSlider ---
    function renderFloatSlider(model) {
        var el = document.createElement("div");
        el.className = "widget-floatslider";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = model.state.min != null ? model.state.min : 0.0;
        slider.max = model.state.max != null ? model.state.max : 1.0;
        slider.step = model.state.step != null ? model.state.step : 0.1;
        slider.value = model.state.value != null ? model.state.value : 0.0;
        slider.orientation = model.state.orientation != null ? model.state.orientation : "horizontal";
        if (model.state.disabled) slider.disabled = true;
        if (model.state.orientation=="vertical") {
            el.classList.add("widget-vertical")
        }

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = parseFloat(slider.value).toFixed(2);

        slider.addEventListener("input", function() {
            var val = parseFloat(slider.value);
            readout.textContent = val.toFixed(2);
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(slider);
        el.appendChild(readout);
        return el;
    }
    renderFloatSlider.update = function(model, el) {
        var slider = el.querySelector("input[type=range]");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        if (slider && document.activeElement !== slider) slider.value = model.state.value;
        if (readout) readout.textContent = parseFloat(model.state.value).toFixed(2);
        if (label) label.textContent = model.state.description || "";
        if (slider) slider.disabled = model.state.disabled || false;
    };

    // --- IntText ---
    function renderIntText(model) {
        var el = document.createElement("div");
        el.className = "widget-inttext";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "number";
        input.value = model.state.value != null ? model.state.value : 0;
        input.step = model.state.step != null ? model.state.step : 1;
        if (model.state.disabled) input.disabled = true;

        input.addEventListener("change", function() {
            var val = parseInt(input.value);
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderIntText.update = function(model, el) {
        var input = el.querySelector("input[type=number]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value != null ? model.state.value : 0;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- FloatText ---
    function renderFloatText(model) {
        var el = document.createElement("div");
        el.className = "widget-floattext";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "number";
        input.value = model.state.value != null ? model.state.value : 0.0;
        input.step = model.state.step != null ? model.state.step : 0.01;
        if (model.state.disabled) input.disabled = true;

        input.addEventListener("change", function() {
            var val = parseFloat(input.value);
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderFloatText.update = function(model, el) {
        var input = el.querySelector("input[type=number]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value != null ? model.state.value : 0.0;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- Text ---
    function renderText(model) {
        var el = document.createElement("div");
        el.className = "widget-text";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "text";
        input.value = model.state.value || "";
        input.placeholder = model.state.placeholder || "";
        if (model.state.disabled) input.disabled = true;

        if (model.state.continuous_update !== false) {
            input.addEventListener("input", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        } else {
            input.addEventListener("change", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        }

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderText.update = function(model, el) {
        var input = el.querySelector("input[type=text]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value || "";
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- Textarea ---
    function renderTextarea(model) {
        var el = document.createElement("div");
        el.className = "widget-textarea";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var textarea = document.createElement("textarea");
        textarea.value = model.state.value || "";
        textarea.placeholder = model.state.placeholder || "";
        textarea.rows = model.state.rows || 3;
        if (model.state.disabled) textarea.disabled = true;

        if (model.state.continuous_update !== false) {
            textarea.addEventListener("input", function() {
                model.state.value = textarea.value;
                sendStateUpdate(model.comm_id, {value: textarea.value});
                syncOtherViews(model, el);
            });
        } else {
            textarea.addEventListener("change", function() {
                model.state.value = textarea.value;
                sendStateUpdate(model.comm_id, {value: textarea.value});
                syncOtherViews(model, el);
            });
        }

        el.appendChild(label);
        el.appendChild(textarea);
        return el;
    }
    renderTextarea.update = function(model, el) {
        var textarea = el.querySelector("textarea");
        if (textarea && document.activeElement !== textarea) {
            textarea.value = model.state.value || "";
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (textarea) textarea.disabled = model.state.disabled || false;
    };

    // --- Dropdown ---
    function renderDropdown(model) {
        var el = document.createElement("div");
        el.className = "widget-dropdown";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var select = document.createElement("select");
        if (model.state.disabled) select.disabled = true;

        var options = model.state._options_labels || [];
        for (var i = 0; i < options.length; i++) {
            var o = document.createElement("option");
            o.value = options[i];
            o.textContent = options[i];
            if (model.state.index != null && i === model.state.index) o.selected = true;
            select.appendChild(o);
        }

        select.addEventListener("change", function() {
            var idx = select.selectedIndex;
            model.state.index = idx;
            sendStateUpdate(model.comm_id, {index: idx});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(select);
        return el;
    }
    renderDropdown.update = function(model, el) {
        var select = el.querySelector("select");
        if (select && model.state.index != null) {
            select.selectedIndex = model.state.index;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (select) select.disabled = model.state.disabled || false;
    };

    // --- Checkbox ---
    function renderCheckbox(model) {
        var el = document.createElement("div");
        el.className = "widget-checkbox";

        var labelEl = document.createElement("label");
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = model.state.value || false;
        if (model.state.disabled) cb.disabled = true;

        var span = document.createElement("span");
        span.textContent = model.state.description || "";

        cb.addEventListener("change", function() {
            model.state.value = cb.checked;
            sendStateUpdate(model.comm_id, {value: cb.checked});
            syncOtherViews(model, el);
        });

        labelEl.appendChild(cb);
        labelEl.appendChild(span);
        el.appendChild(labelEl);
        return el;
    }
    renderCheckbox.update = function(model, el) {
        var cb = el.querySelector("input[type=checkbox]");
        if (cb) {
            cb.checked = model.state.value || false;
            cb.disabled = model.state.disabled || false;
        }
        var span = el.querySelector("span");
        if (span) span.textContent = model.state.description || "";
    };

    // --- ToggleButton ---
    function renderToggleButton(model) {
        var btn = document.createElement("button");
        btn.className = "widget-toggle";
        btn.textContent = model.state.description || "Toggle";
        btn.setAttribute("aria-pressed", model.state.value ? "true" : "false");
        if (model.state.disabled) btn.disabled = true;

        btn.addEventListener("click", function() {
            model.state.value = !model.state.value;
            btn.setAttribute("aria-pressed", model.state.value ? "true" : "false");
            sendStateUpdate(model.comm_id, {value: model.state.value});
            syncOtherViews(model, btn);
        });

        return btn;
    }
    renderToggleButton.update = function(model, el) {
        el.setAttribute("aria-pressed", model.state.value ? "true" : "false");
        el.textContent = model.state.description || "Toggle";
        el.disabled = model.state.disabled || false;
    };

    // --- Button ---
    function renderButton(model) {
        var btn = document.createElement("button");
        btn.className = "widget-button";
        btn.textContent = model.state.description || "Button";
        if (model.state.disabled) btn.disabled = true;

        if (model.state.button_style) {
            btn.dataset.buttonStyle = model.state.button_style;
        }

        btn.addEventListener("click", function() {
            sendCommMsg(model.comm_id, {
                method: "custom",
                content: {event: "click"}
            });
        });

        return btn;
    }
    renderButton.update = function(model, el) {
        el.textContent = model.state.description || "Button";
        el.disabled = model.state.disabled || false;
    };

    // --- HTML ---
    function renderHTMLWidget(model) {
        var el = document.createElement("div");
        el.className = "widget-html";
        el.innerHTML = model.state.value || "";
        return el;
    }
    renderHTMLWidget.update = function(model, el) {
        el.innerHTML = model.state.value || "";
    };

    // --- Label ---
    function renderLabel(model) {
        var el = document.createElement("span");
        el.className = "widget-label-display";
        el.textContent = model.state.value || "";
        return el;
    }
    renderLabel.update = function(model, el) {
        el.textContent = model.state.value || "";
    };

    // --- Output ---
    function renderOutput(model) {
        var el = document.createElement("div");
        el.className = "widget-output";
        renderOutputContents(model, el);
        return el;
    }
    renderOutput.update = function(model, el) {
        // Only render from outputs state when it actually has content.
        // When outputs is empty, leave innerHTML alone - it may contain
        // content captured live via the msg_id mechanism (with out:).
        // Explicit clearing is handled by the clear_output message handler.
        var outputs = model.state.outputs || [];
        if (outputs.length > 0) {
            renderOutputContents(model, el);
        }
    };

    function renderOutputContents(model, el) {
        var outputs = model.state.outputs || [];
        el.innerHTML = "";
        for (var i = 0; i < outputs.length; i++) {
            var output = outputs[i];
            if (output.output_type === "stream") {
                var text = (output.text || "").replace(/\n/g, "<br/>");
                el.insertAdjacentHTML('beforeend', text);
            } else if (output.output_type === "display_data" || output.output_type === "execute_result") {
                var data = output.data || {};
                if (data["text/html"]) {
                    el.insertAdjacentHTML('beforeend', data["text/html"]);
                    activateScripts(el);
                    renderMathIn(el);
                } else if (data["image/png"]) {
                    el.insertAdjacentHTML('beforeend', '<img src="data:image/png;base64,' + data["image/png"] + '">');
                } else if (data["image/svg+xml"]) {
                    el.insertAdjacentHTML('beforeend', data["image/svg+xml"]);
                } else if (data["text/plain"]) {
                    el.insertAdjacentHTML('beforeend', "<pre>" + data["text/plain"] + "</pre>");
                }
            } else if (output.output_type === "error") {
                el.insertAdjacentHTML('beforeend', "<pre style='color:red;'>Error: " + (output.evalue || "") + "</pre>");
            }
        }
    }

    function handleCustomMessage(model, content, buffersBase64) {
        if (!content) return;
        // anywidget custom messages dispatched to ESM listeners
        if (model._anywidgetWrapper) {
            model._anywidgetWrapper._fireCustomMessage(content, buffersBase64);
            return;
        }
        // ipympl sends custom messages with JSON data for figure updates
        if (model.model_name === "MPLCanvasModel") {
            handleMPLCustomMessage(model, content, buffersBase64);
        }
    }

    function appendOutputToWidget(model, msg) {
        var msgType = msg.msg_type;
        var content = msg.content;

        for (var i = 0; i < model.views.length; i++) {
            var el = model.views[i];
            if (msgType === "stream") {
                el.insertAdjacentHTML('beforeend', (content.text || "").replace(/\n/g, "<br/>"));
            } else if (msgType === "display_data" || msgType === "execute_result") {
                var data = content.data || {};
                if (data["text/html"]) {
                    el.insertAdjacentHTML('beforeend', data["text/html"]);
                    activateScripts(el);
                    renderMathIn(el);
                } else if (data["image/png"]) {
                    el.insertAdjacentHTML('beforeend', '<img src="data:image/png;base64,' + data["image/png"] + '">');
                } else if (data["image/svg+xml"]) {
                    el.insertAdjacentHTML('beforeend', data["image/svg+xml"]);
                } else if (data["text/plain"]) {
                    el.insertAdjacentHTML('beforeend', "<pre>" + data["text/plain"] + "</pre>");
                }
            } else if (msgType === "error") {
                el.insertAdjacentHTML('beforeend', "<pre style='color:red;'>Error: " + (content.evalue || "") + "</pre>");
            }
        }
    }

    function routeOrphanedOutput(commId, msg) {
        // Route output from a widget callback to the jupyter-output div of
        // the cell containing the widget that triggered the callback.
        var model = widgetModels[commId];
        if (!model || model.views.length === 0) return;

        // Walk up from the widget's first view to find the cell
        var cell = model.views[0].closest('.jupyter-cell');
        if (!cell) return;

        var outputArea = cell.querySelector('.jupyter-output');
        if (!outputArea) return;

        outputArea.style.display = "block";

        var msgType = msg.msg_type;
        var content = msg.content;
        if (msgType === "stream") {
            outputArea.insertAdjacentHTML('beforeend', (content.text || "").replace(/\n/g, "<br/>"));
        } else if (msgType === "display_data" || msgType === "execute_result") {
            var data = content.data || {};
            if (data["text/html"]) {
                outputArea.insertAdjacentHTML('beforeend', data["text/html"]);
                activateScripts(outputArea);
                renderMathIn(outputArea);
            } else if (data["image/png"]) {
                outputArea.insertAdjacentHTML('beforeend', '<img src="data:image/png;base64,' + data["image/png"] + '">');
            } else if (data["image/svg+xml"]) {
                outputArea.insertAdjacentHTML('beforeend', data["image/svg+xml"]);
            } else if (data["text/plain"]) {
                outputArea.insertAdjacentHTML('beforeend', "<pre>" + data["text/plain"] + "</pre>");
            }
        } else if (msgType === "error") {
            outputArea.insertAdjacentHTML('beforeend', "<pre style='color:red;'>Error: " + (content.evalue || "") + "</pre>");
        }
    }

    // --- MPLCanvas (ipympl) ---
    function handleMPLCustomMessage(model, content, buffersBase64) {
        // content comes as a JSON string from ipympl's send({'data': json_str})
        var data;
        if (typeof content === 'string') {
            try { data = JSON.parse(content); } catch(e) { return; }
        } else if (content && typeof content === 'object') {
            // Sometimes content.data holds the JSON string
            if (typeof content.data === 'string') {
                try { data = JSON.parse(content.data); } catch(e) { data = content; }
            } else {
                data = content;
            }
        } else {
            return;
        }

        if (data.type === 'draw') {
            // Backend sent draw_idle notification - echo it back to trigger actual render
            sendMPLEvent(model.comm_id, {type: 'draw'});
            return;
        }

        if (data.type === 'save' && buffersBase64 && buffersBase64.length > 0) {
            var fmt = data.format || 'png';
            var mimeTypes = {png: 'image/png', svg: 'image/svg+xml', pdf: 'application/pdf', jpg: 'image/jpeg', jpeg: 'image/jpeg'};
            var mime = mimeTypes[fmt] || 'application/octet-stream';
            var binary = atob(buffersBase64[0]);
            var bytes = new Uint8Array(binary.length);
            for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            var blob = new Blob([bytes], {type: mime});
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = (model.state._figure_label || 'figure') + '.' + fmt;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            return;
        }

        if (data.type === 'binary' && buffersBase64 && buffersBase64.length > 0) {
            // Canvas image data - draw PNG onto the canvas
            for (var i = 0; i < model.views.length; i++) {
                drawMPLImage(model, model.views[i], buffersBase64[0]);
            }
        } else if (data.type === 'cursor') {
            model.state._cursor = data.cursor;
            for (var i = 0; i < model.views.length; i++) {
                var c = model.views[i].querySelector('.mpl-canvas-top');
                if (c) c.style.cursor = data.cursor;
            }
        } else if (data.type === 'message') {
            model.state._message = data.message;
            for (var i = 0; i < model.views.length; i++) {
                var footer = model.views[i].querySelector('.mpl-footer');
                if (footer) footer.textContent = data.message;
            }
        } else if (data.type === 'figure_label') {
            model.state._figure_label = data.label;
            for (var i = 0; i < model.views.length; i++) {
                var header = model.views[i].querySelector('.mpl-header');
                if (header) header.textContent = data.label;
            }
        } else if (data.type === 'resize') {
            var oldSize = model.state._size;
            model.state._size = data.size;
            var sizeChanged = !oldSize || oldSize[0] !== data.size[0] || oldSize[1] !== data.size[1];
            for (var i = 0; i < model.views.length; i++) {
                if (sizeChanged) {
                    model.views[i]._lastSize = data.size.slice();
                    resizeMPLCanvases(model.views[i], data.size[0], data.size[1]);
                    // Redraw after resize since setting canvas dimensions clears it
                    if (model.state._data_url) {
                        (function(viewEl) {
                            var canvas = viewEl.querySelector('.mpl-canvas-main');
                            if (!canvas) return;
                            var redrawImg = new Image();
                            redrawImg.onload = function() {
                                canvas.getContext('2d').drawImage(redrawImg, 0, 0);
                            };
                            var url = model.state._data_url;
                            redrawImg.src = url.indexOf('data:') === 0 ? url : 'data:image/png;base64,' + url;
                        })(model.views[i]);
                    }
                }
            }
        } else if (data.type === 'image_mode') {
            model.state._image_mode = data.mode;
        } else if (data.type === 'rubberband') {
            for (var i = 0; i < model.views.length; i++) {
                drawRubberband(model.views[i], data.x0, data.y0, data.x1, data.y1);
            }
        }
    }

    function drawMPLImage(model, viewEl, base64Data) {
        var canvas = viewEl.querySelector('.mpl-canvas-main');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        var img = new Image();
        img.onload = function() {
            if (model.state._image_mode === 'full') {
                canvas.width = img.width;
                canvas.height = img.height;
                // Also resize the overlay
                var top = viewEl.querySelector('.mpl-canvas-top');
                if (top) { top.width = img.width; top.height = img.height; }
                ctx.clearRect(0, 0, canvas.width, canvas.height);
            }
            ctx.drawImage(img, 0, 0);
            URL.revokeObjectURL(img.src);
            // Acknowledge receipt so kernel sends next frame
            sendMPLEvent(model.comm_id, {type: 'ack'});
        };
        // Convert base64 to blob URL
        var binary = atob(base64Data);
        var bytes = new Uint8Array(binary.length);
        for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        var blob = new Blob([bytes], {type: 'image/png'});
        img.src = URL.createObjectURL(blob);
    }

    function resizeMPLCanvases(viewEl, width, height) {
        var main = viewEl.querySelector('.mpl-canvas-main');
        var top = viewEl.querySelector('.mpl-canvas-top');
        var ratio = window.devicePixelRatio || 1;
        var cssW = width / ratio;
        var cssH = height / ratio;
        if (main) {
            main.width = width;
            main.height = height;
            main.style.width = cssW + 'px';
            main.style.height = cssH + 'px';
        }
        if (top) {
            top.width = width;
            top.height = height;
            top.style.width = cssW + 'px';
            top.style.height = cssH + 'px';
        }
    }

    function drawRubberband(viewEl, x0, y0, x1, y1) {
        var canvas = viewEl.querySelector('.mpl-canvas-top');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (x1 !== undefined && y1 !== undefined) {
            // Flip y-axis: matplotlib sends y with origin at bottom, canvas has origin at top
            var h = canvas.height;
            ctx.strokeStyle = '#000';
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 2]);
            ctx.strokeRect(x0, h - y0, x1 - x0, -(y1 - y0));
            ctx.setLineDash([]);
        }
    }

    function renderMPLCanvas(model) {
        var el = document.createElement('div');
        el.className = 'mpl-figure';

        // Header (figure label)
        var header = document.createElement('div');
        header.className = 'mpl-header';
        header.textContent = model.state._figure_label || '';
        if (!model.state.header_visible) header.style.display = 'none';

        // Toolbar (rendered via children reference)
        var toolbarArea = document.createElement('div');
        toolbarArea.className = 'mpl-toolbar-area';
        if (model.state.toolbar) {
            var toolbarId = model.state.toolbar.replace('IPY_MODEL_', '');
            // Set canvas comm_id on toolbar model so button events go to canvas
            setTimeout(function() {
                var tbModel = widgetModels[toolbarId];
                if (tbModel) tbModel._canvasCommId = model.comm_id;
                renderWidgetView(toolbarId, toolbarArea);
            }, 50);
        }

        // Canvas container
        var canvasContainer = document.createElement('div');
        canvasContainer.className = 'mpl-canvas-container';

        var canvasDiv = document.createElement('div');
        canvasDiv.className = 'mpl-canvas-div';

        var mainCanvas = document.createElement('canvas');
        mainCanvas.className = 'mpl-canvas-main';
        var topCanvas = document.createElement('canvas');
        topCanvas.className = 'mpl-canvas-top';
        topCanvas.style.cursor = model.state._cursor || 'pointer';

        // Set initial size
        var size = model.state._size || [800, 600];
        var ratio = window.devicePixelRatio || 1;
        mainCanvas.width = size[0];
        mainCanvas.height = size[1];
        mainCanvas.style.width = (size[0] / ratio) + 'px';
        mainCanvas.style.height = (size[1] / ratio) + 'px';
        topCanvas.width = size[0];
        topCanvas.height = size[1];
        topCanvas.style.width = (size[0] / ratio) + 'px';
        topCanvas.style.height = (size[1] / ratio) + 'px';

        canvasDiv.appendChild(mainCanvas);
        canvasDiv.appendChild(topCanvas);
        canvasContainer.appendChild(canvasDiv);

        // Footer (status message)
        var footer = document.createElement('div');
        footer.className = 'mpl-footer';
        footer.textContent = model.state._message || '';
        if (!model.state.footer_visible) footer.style.display = 'none';

        // Assemble based on toolbar position
        var tbPos = model.state.toolbar_position || 'left';
        if (tbPos === 'top') {
            el.appendChild(header);
            el.appendChild(toolbarArea);
            el.appendChild(canvasContainer);
            el.appendChild(footer);
        } else if (tbPos === 'bottom') {
            el.appendChild(header);
            el.appendChild(canvasContainer);
            el.appendChild(toolbarArea);
            el.appendChild(footer);
        } else if (tbPos === 'right') {
            el.appendChild(header);
            var row = document.createElement('div');
            row.style.display = 'flex';
            row.appendChild(canvasContainer);
            row.appendChild(toolbarArea);
            el.appendChild(row);
            el.appendChild(footer);
        } else {
            // left (default)
            el.appendChild(header);
            var row = document.createElement('div');
            row.style.display = 'flex';
            row.appendChild(toolbarArea);
            row.appendChild(canvasContainer);
            el.appendChild(row);
            el.appendChild(footer);
        }

        // Mouse event handlers
        var throttle = model.state.pan_zoom_throttle || 33;
        var lastMotion = 0;

        function getModifiers(e) {
            var mods = [];
            if (e.shiftKey) mods.push('shift');
            if (e.ctrlKey) mods.push('ctrl');
            if (e.altKey) mods.push('alt');
            if (e.metaKey) mods.push('meta');
            return mods;
        }

        function getMouseEvent(e, type) {
            var rect = topCanvas.getBoundingClientRect();
            var ratio = window.devicePixelRatio || 1;
            return {
                type: type,
                x: (e.clientX - rect.left) * ratio,
                y: (e.clientY - rect.top) * ratio,
                button: e.button,
                buttons: e.buttons,
                modifiers: getModifiers(e),
                guiEvent: {}
            };
        }

        topCanvas.addEventListener('mousedown', function(e) {
            e.preventDefault();
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'button_press'));
        });

        topCanvas.addEventListener('mouseup', function(e) {
            e.preventDefault();
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'button_release'));
        });

        topCanvas.addEventListener('dblclick', function(e) {
            e.preventDefault();
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'dblclick'));
        });

        topCanvas.addEventListener('mousemove', function(e) {
            var now = Date.now();
            if (now - lastMotion < throttle) return;
            lastMotion = now;
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'motion_notify'));
        });

        topCanvas.addEventListener('mouseenter', function(e) {
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'figure_enter'));
        });

        topCanvas.addEventListener('mouseleave', function(e) {
            sendMPLEvent(model.comm_id, getMouseEvent(e, 'figure_leave'));
        });

        topCanvas.addEventListener('wheel', function(e) {
            if (!model.state.capture_scroll) return;
            e.preventDefault();
            var evt = getMouseEvent(e, 'scroll');
            evt.step = e.deltaY < 0 ? 1 : -1;
            sendMPLEvent(model.comm_id, evt);
        }, {passive: false});

        // Keyboard events on the canvas
        topCanvas.tabIndex = 0;
        topCanvas.addEventListener('keydown', function(e) {
            sendMPLEvent(model.comm_id, {type: 'key_press', key: e.key, guiEvent: {}});
        });
        topCanvas.addEventListener('keyup', function(e) {
            sendMPLEvent(model.comm_id, {type: 'key_release', key: e.key, guiEvent: {}});
        });

        // If we have a _data_url fallback (late-joiner), render it
        if (model.state._data_url && model.state._data_url !== false) {
            var img = new Image();
            img.onload = function() {
                mainCanvas.width = img.width;
                mainCanvas.height = img.height;
                topCanvas.width = img.width;
                topCanvas.height = img.height;
                var r = window.devicePixelRatio || 1;
                mainCanvas.style.width = (img.width / r) + 'px';
                mainCanvas.style.height = (img.height / r) + 'px';
                topCanvas.style.width = (img.width / r) + 'px';
                topCanvas.style.height = (img.height / r) + 'px';
                mainCanvas.getContext('2d').drawImage(img, 0, 0);
            };
            var dataUrl = model.state._data_url;
            img.src = dataUrl.indexOf('data:') === 0 ? dataUrl : 'data:image/png;base64,' + dataUrl;
            el._lastDataUrl = model.state._data_url;
        }
        el._lastSize = model.state._size ? model.state._size.slice() : null;

        // Send initialized signal after a short delay to let the widget appear
        setTimeout(function() {
            sendMPLEvent(model.comm_id, {type: 'initialized'});
            sendMPLEvent(model.comm_id, {type: 'set_dpi_ratio', dpi_ratio: window.devicePixelRatio || 1});
        }, 100);

        // Watch for DPI changes
        if (window.matchMedia) {
            var mql = window.matchMedia('(resolution: ' + window.devicePixelRatio + 'dppx)');
            mql.addEventListener('change', function() {
                sendMPLEvent(model.comm_id, {type: 'set_dpi_ratio', dpi_ratio: window.devicePixelRatio || 1});
            });
        }

        return el;
    }
    renderMPLCanvas.update = function(model, el) {
        var header = el.querySelector('.mpl-header');
        if (header) header.textContent = model.state._figure_label || '';
        var footer = el.querySelector('.mpl-footer');
        if (footer) footer.textContent = model.state._message || '';
        var top = el.querySelector('.mpl-canvas-top');
        if (top) top.style.cursor = model.state._cursor || 'pointer';

        // Resize from _size state update (only if size actually changed)
        var didResize = false;
        if (model.state._size) {
            var size = model.state._size;
            if (typeof size === 'string') {
                try { size = JSON.parse(size); } catch(e) {}
            }
            if (Array.isArray(size)) {
                var prev = el._lastSize;
                if (!prev || prev[0] !== size[0] || prev[1] !== size[1]) {
                    el._lastSize = size.slice();
                    resizeMPLCanvases(el, size[0], size[1]);
                    didResize = true;
                }
            }
        }

        // Draw image from _data_url state update (or redraw after resize)
        var dataUrlChanged = model.state._data_url && model.state._data_url !== el._lastDataUrl;
        if (dataUrlChanged || (didResize && model.state._data_url)) {
            if (dataUrlChanged) el._lastDataUrl = model.state._data_url;
            var canvas = el.querySelector('.mpl-canvas-main');
            if (canvas) {
                var img = new Image();
                img.onload = function() {
                    canvas.getContext('2d').drawImage(img, 0, 0);
                };
                var dataUrl = model.state._data_url;
                img.src = dataUrl.indexOf('data:') === 0 ? dataUrl : 'data:image/png;base64,' + dataUrl;
            }
        }

        // Rubberband via state
        if (model.state._rubberband_width > 0 && model.state._rubberband_height > 0) {
            drawRubberband(el,
                model.state._rubberband_x, model.state._rubberband_y,
                model.state._rubberband_x + model.state._rubberband_width,
                model.state._rubberband_y + model.state._rubberband_height);
        } else {
            drawRubberband(el, 0, 0, undefined, undefined);
        }
    };

    function sendMPLEvent(commId, eventData) {
        sendCommMsg(commId, {
            method: 'custom',
            content: eventData
        });
    }

    // --- MPL Toolbar ---
    function renderMPLToolbar(model) {
        var el = document.createElement('div');
        el.className = 'mpl-toolbar';
        var orientation = model.state.orientation || 'vertical';
        if (orientation === 'horizontal') {
            el.classList.add('mpl-toolbar-horizontal');
        }

        var toolitems = model.state.toolitems || [];
        for (var i = 0; i < toolitems.length; i++) {
            var item = toolitems[i];
            // item is [tooltip, description, icon_name, action_name]
            var tooltip = item[0] || '';
            var iconName = item[2] || '';
            var actionName = item[3] || '';

            if (actionName === '') {
                // Separator
                var sep = document.createElement('div');
                sep.className = 'mpl-toolbar-separator';
                el.appendChild(sep);
                continue;
            }

            var btn = document.createElement('button');
            btn.className = 'mpl-toolbar-btn';
            btn.title = tooltip;
            btn.textContent = getToolbarIcon(iconName, actionName);
            btn.dataset.action = actionName;

            (function(action, button) {
                button.addEventListener('click', function() {
                    // Toggle pan/zoom buttons
                    if (action === 'pan' || action === 'zoom') {
                        var wasActive = button.classList.contains('active');
                        // Deactivate all toggle buttons
                        var btns = el.querySelectorAll('.mpl-toolbar-btn');
                        for (var j = 0; j < btns.length; j++) {
                            btns[j].classList.remove('active');
                        }
                        if (!wasActive) button.classList.add('active');
                    }
                    sendMPLEvent(model._canvasCommId || model.comm_id, {type: 'toolbar_button', name: action});
                });
            })(actionName, btn);

            el.appendChild(btn);
        }

        return el;
    }
    renderMPLToolbar.update = function(model, el) {
        // Update active state based on _current_action
        var btns = el.querySelectorAll('.mpl-toolbar-btn');
        for (var i = 0; i < btns.length; i++) {
            var action = btns[i].dataset.action;
            if (action === 'pan' || action === 'zoom') {
                btns[i].classList.toggle('active', model.state._current_action === action);
            }
        }
    };

    function getToolbarIcon(iconName, actionName) {
        var icons = {
            'home': '\u2302',
            'arrow-left': '\u25C0',
            'arrow-right': '\u25B6',
            'arrows': '\u2725',
            'square-o': '\uD83D\uDD0D',
            'floppy-o': '\u2B07'
        };
        return icons[iconName] || actionName || iconName;
    }

    // --- VBox ---
    function renderVBox(model) {
        var el = document.createElement("div");
        el.className = "widget-vbox";

        var children = model.state.children || [];
        for (var i = 0; i < children.length; i++) {
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, el);
        }
        return el;
    }
    renderVBox.update = function(model, el) {
        el.innerHTML = "";
        // Remove old views from child models
        var children = model.state.children || [];
        for (var i = 0; i < children.length; i++) {
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, el);
        }
    };

    // --- HBox ---
    function renderHBox(model) {
        var el = document.createElement("div");
        el.className = "widget-hbox";

        var children = model.state.children || [];
        for (var i = 0; i < children.length; i++) {
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, el);
        }
        return el;
    }
    renderHBox.update = renderVBox.update;

    // --- RadioButtons ---
    function renderRadioButtons(model) {
        var el = document.createElement("div");
        el.className = "widget-radiobuttons";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var group = document.createElement("div");
        group.className = "widget-radio-group";
        var options = model.state._options_labels || [];
        var groupName = "radio_" + model.comm_id;
        for (var i = 0; i < options.length; i++) {
            var lbl = document.createElement("label");
            lbl.className = "widget-radio-item";
            var radio = document.createElement("input");
            radio.type = "radio";
            radio.name = groupName;
            radio.value = i;
            if (model.state.index != null && i === model.state.index) radio.checked = true;
            if (model.state.disabled) radio.disabled = true;
            (function(idx) {
                radio.addEventListener("change", function() {
                    model.state.index = idx;
                    sendStateUpdate(model.comm_id, {index: idx});
                    syncOtherViews(model, el);
                });
            })(i);
            var span = document.createElement("span");
            span.textContent = options[i];
            lbl.appendChild(radio);
            lbl.appendChild(span);
            group.appendChild(lbl);
        }

        el.appendChild(label);
        el.appendChild(group);
        return el;
    }
    renderRadioButtons.update = function(model, el) {
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        var radios = el.querySelectorAll("input[type=radio]");
        for (var i = 0; i < radios.length; i++) {
            radios[i].checked = (model.state.index === i);
            radios[i].disabled = model.state.disabled || false;
        }
    };

    // --- Select ---
    function renderSelect(model) {
        var el = document.createElement("div");
        el.className = "widget-select";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var select = document.createElement("select");
        select.size = model.state.rows || 5;
        if (model.state.disabled) select.disabled = true;

        var options = model.state._options_labels || [];
        for (var i = 0; i < options.length; i++) {
            var o = document.createElement("option");
            o.value = i;
            o.textContent = options[i];
            if (model.state.index != null && i === model.state.index) o.selected = true;
            select.appendChild(o);
        }

        select.addEventListener("change", function() {
            var idx = select.selectedIndex;
            model.state.index = idx;
            sendStateUpdate(model.comm_id, {index: idx});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(select);
        return el;
    }
    renderSelect.update = function(model, el) {
        var select = el.querySelector("select");
        if (select && model.state.index != null) {
            select.selectedIndex = model.state.index;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (select) select.disabled = model.state.disabled || false;
    };

    // --- SelectMultiple ---
    function renderSelectMultiple(model) {
        var el = document.createElement("div");
        el.className = "widget-selectmultiple";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var select = document.createElement("select");
        select.multiple = true;
        select.size = model.state.rows || 5;
        if (model.state.disabled) select.disabled = true;

        var options = model.state._options_labels || [];
        var selectedIndices = model.state.index || [];
        for (var i = 0; i < options.length; i++) {
            var o = document.createElement("option");
            o.value = i;
            o.textContent = options[i];
            if (selectedIndices.indexOf(i) !== -1) o.selected = true;
            select.appendChild(o);
        }

        select.addEventListener("change", function() {
            var indices = [];
            for (var j = 0; j < select.options.length; j++) {
                if (select.options[j].selected) indices.push(j);
            }
            model.state.index = indices;
            sendStateUpdate(model.comm_id, {index: indices});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(select);
        return el;
    }
    renderSelectMultiple.update = function(model, el) {
        var select = el.querySelector("select");
        var selectedIndices = model.state.index || [];
        if (select && document.activeElement !== select) {
            for (var i = 0; i < select.options.length; i++) {
                select.options[i].selected = (selectedIndices.indexOf(i) !== -1);
            }
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (select) select.disabled = model.state.disabled || false;
    };

    // --- ToggleButtons ---
    function renderToggleButtons(model) {
        var el = document.createElement("div");
        el.className = "widget-togglebuttons";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var group = document.createElement("div");
        group.className = "widget-togglebutton-group";
        var options = model.state._options_labels || [];
        var icons = model.state.icons || [];
        var tooltips = model.state.tooltips || [];

        for (var i = 0; i < options.length; i++) {
            var btn = document.createElement("button");
            btn.className = "widget-toggle";
            btn.textContent = (icons[i] ? icons[i] + " " : "") + options[i];
            btn.title = tooltips[i] || "";
            btn.setAttribute("aria-pressed", model.state.index === i ? "true" : "false");
            if (model.state.disabled) btn.disabled = true;
            if (model.state.button_style) btn.dataset.buttonStyle = model.state.button_style;
            (function(idx) {
                btn.addEventListener("click", function() {
                    model.state.index = idx;
                    var btns = group.querySelectorAll(".widget-toggle");
                    for (var j = 0; j < btns.length; j++) {
                        btns[j].setAttribute("aria-pressed", j === idx ? "true" : "false");
                    }
                    sendStateUpdate(model.comm_id, {index: idx});
                    syncOtherViews(model, el);
                });
            })(i);
            group.appendChild(btn);
        }

        el.appendChild(label);
        el.appendChild(group);
        return el;
    }
    renderToggleButtons.update = function(model, el) {
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        var btns = el.querySelectorAll(".widget-togglebutton-group .widget-toggle");
        for (var i = 0; i < btns.length; i++) {
            btns[i].setAttribute("aria-pressed", model.state.index === i ? "true" : "false");
            btns[i].disabled = model.state.disabled || false;
        }
    };

    // --- BoundedIntText ---
    function renderBoundedIntText(model) {
        var el = document.createElement("div");
        el.className = "widget-boundedinttext";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "number";
        input.value = model.state.value != null ? model.state.value : 0;
        input.min = model.state.min != null ? model.state.min : 0;
        input.max = model.state.max != null ? model.state.max : 100;
        input.step = model.state.step != null ? model.state.step : 1;
        if (model.state.disabled) input.disabled = true;

        input.addEventListener("change", function() {
            var val = parseInt(input.value);
            var mn = model.state.min != null ? model.state.min : 0;
            var mx = model.state.max != null ? model.state.max : 100;
            val = Math.min(mx, Math.max(mn, val));
            input.value = val;
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderBoundedIntText.update = function(model, el) {
        var input = el.querySelector("input[type=number]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value != null ? model.state.value : 0;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- BoundedFloatText ---
    function renderBoundedFloatText(model) {
        var el = document.createElement("div");
        el.className = "widget-boundedfloattext";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "number";
        input.value = model.state.value != null ? model.state.value : 0.0;
        input.min = model.state.min != null ? model.state.min : 0.0;
        input.max = model.state.max != null ? model.state.max : 100.0;
        input.step = model.state.step != null ? model.state.step : 0.01;
        if (model.state.disabled) input.disabled = true;

        input.addEventListener("change", function() {
            var val = parseFloat(input.value);
            var mn = model.state.min != null ? model.state.min : 0.0;
            var mx = model.state.max != null ? model.state.max : 100.0;
            val = Math.min(mx, Math.max(mn, val));
            input.value = val;
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderBoundedFloatText.update = function(model, el) {
        var input = el.querySelector("input[type=number]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value != null ? model.state.value : 0.0;
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- Password ---
    function renderPassword(model) {
        var el = document.createElement("div");
        el.className = "widget-password";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "password";
        input.value = model.state.value || "";
        input.placeholder = model.state.placeholder || "";
        if (model.state.disabled) input.disabled = true;

        if (model.state.continuous_update !== false) {
            input.addEventListener("input", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        } else {
            input.addEventListener("change", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        }

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderPassword.update = function(model, el) {
        var input = el.querySelector("input[type=password]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value || "";
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- Combobox ---
    function renderCombobox(model) {
        var el = document.createElement("div");
        el.className = "widget-combobox";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var listId = "datalist_" + model.comm_id;
        var input = document.createElement("input");
        input.type = "text";
        input.value = model.state.value || "";
        input.placeholder = model.state.placeholder || "";
        input.setAttribute("list", listId);
        if (model.state.disabled) input.disabled = true;

        var datalist = document.createElement("datalist");
        datalist.id = listId;
        // Combobox extends Text, not a Selection widget - options are in .options, not ._options_labels
        var options = model.state.options || [];
        for (var i = 0; i < options.length; i++) {
            var o = document.createElement("option");
            o.value = options[i];
            datalist.appendChild(o);
        }

        if (model.state.continuous_update !== false) {
            input.addEventListener("input", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        } else {
            input.addEventListener("change", function() {
                model.state.value = input.value;
                sendStateUpdate(model.comm_id, {value: input.value});
                syncOtherViews(model, el);
            });
        }

        el.appendChild(label);
        el.appendChild(input);
        el.appendChild(datalist);
        return el;
    }
    renderCombobox.update = function(model, el) {
        var input = el.querySelector("input[type=text]");
        if (input && document.activeElement !== input) {
            input.value = model.state.value || "";
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- ColorPicker ---
    function renderColorPicker(model) {
        var el = document.createElement("div");
        el.className = "widget-colorpicker";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var colorInput = document.createElement("input");
        colorInput.type = "color";
        colorInput.value = model.state.value || "#000000";
        if (model.state.disabled) colorInput.disabled = true;

        el.appendChild(label);
        el.appendChild(colorInput);

        var hexDisplay = null;
        if (!model.state.concise) {
            hexDisplay = document.createElement("span");
            hexDisplay.className = "widget-color-hex";
            hexDisplay.textContent = model.state.value || "#000000";
            el.appendChild(hexDisplay);
        }

        colorInput.addEventListener("input", function() {
            model.state.value = colorInput.value;
            if (hexDisplay) hexDisplay.textContent = colorInput.value;
            sendStateUpdate(model.comm_id, {value: colorInput.value});
            syncOtherViews(model, el);
        });

        return el;
    }
    renderColorPicker.update = function(model, el) {
        var colorInput = el.querySelector("input[type=color]");
        if (colorInput && document.activeElement !== colorInput) {
            colorInput.value = model.state.value || "#000000";
        }
        var hex = el.querySelector(".widget-color-hex");
        if (hex) hex.textContent = model.state.value || "#000000";
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (colorInput) colorInput.disabled = model.state.disabled || false;
    };

    // --- DatePicker ---
    function renderDatePicker(model) {
        var el = document.createElement("div");
        el.className = "widget-datepicker";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "date";
        if (model.state.disabled) input.disabled = true;

        // Convert {year, month, date} to YYYY-MM-DD
        var val = model.state.value;
        if (val && val.year != null) {
            var mm = String(val.month).padStart(2, "0");
            var dd = String(val.date).padStart(2, "0");
            input.value = val.year + "-" + mm + "-" + dd;
        }

        input.addEventListener("change", function() {
            if (input.value) {
                var parts = input.value.split("-");
                var dateObj = {year: parseInt(parts[0]), month: parseInt(parts[1]), date: parseInt(parts[2])};
                model.state.value = dateObj;
                sendStateUpdate(model.comm_id, {value: dateObj});
            } else {
                model.state.value = null;
                sendStateUpdate(model.comm_id, {value: null});
            }
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderDatePicker.update = function(model, el) {
        var input = el.querySelector("input[type=date]");
        if (input && document.activeElement !== input) {
            var val = model.state.value;
            if (val && val.year != null) {
                var mm = String(val.month).padStart(2, "0");
                var dd = String(val.date).padStart(2, "0");
                input.value = val.year + "-" + mm + "-" + dd;
            } else {
                input.value = "";
            }
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- TimePicker ---
    function renderTimePicker(model) {
        var el = document.createElement("div");
        el.className = "widget-timepicker";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var input = document.createElement("input");
        input.type = "time";
        input.step = "1"; // allow seconds
        if (model.state.disabled) input.disabled = true;

        var val = model.state.value;
        if (val && val.hours != null) {
            var hh = String(val.hours).padStart(2, "0");
            var mi = String(val.minutes || 0).padStart(2, "0");
            var ss = String(val.seconds || 0).padStart(2, "0");
            input.value = hh + ":" + mi + ":" + ss;
        }

        input.addEventListener("change", function() {
            if (input.value) {
                var parts = input.value.split(":");
                var timeObj = {hours: parseInt(parts[0]), minutes: parseInt(parts[1]), seconds: parseInt(parts[2] || 0), microseconds: 0};
                model.state.value = timeObj;
                sendStateUpdate(model.comm_id, {value: timeObj});
            } else {
                model.state.value = null;
                sendStateUpdate(model.comm_id, {value: null});
            }
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(input);
        return el;
    }
    renderTimePicker.update = function(model, el) {
        var input = el.querySelector("input[type=time]");
        if (input && document.activeElement !== input) {
            var val = model.state.value;
            if (val && val.hours != null) {
                var hh = String(val.hours).padStart(2, "0");
                var mi = String(val.minutes || 0).padStart(2, "0");
                var ss = String(val.seconds || 0).padStart(2, "0");
                input.value = hh + ":" + mi + ":" + ss;
            } else {
                input.value = "";
            }
        }
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        if (input) input.disabled = model.state.disabled || false;
    };

    // --- SelectionSlider ---
    function renderSelectionSlider(model) {
        var el = document.createElement("div");
        el.className = "widget-selectionslider";

        var options = model.state._options_labels || [];

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = 0;
        slider.max = Math.max(0, options.length - 1);
        slider.step = 1;
        slider.value = model.state.index != null ? model.state.index : 0;
        if (model.state.disabled) slider.disabled = true;
        if (model.state.orientation === "vertical") {
            el.classList.add("widget-vertical");
        }

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        var idx = model.state.index != null ? model.state.index : 0;
        readout.textContent = options[idx] || "";

        slider.addEventListener("input", function() {
            var val = parseInt(slider.value);
            readout.textContent = options[val] || "";
            model.state.index = val;
            sendStateUpdate(model.comm_id, {index: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(slider);
        el.appendChild(readout);
        return el;
    }
    renderSelectionSlider.update = function(model, el) {
        var slider = el.querySelector("input[type=range]");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var options = model.state._options_labels || [];
        if (slider && document.activeElement !== slider) {
            slider.value = model.state.index != null ? model.state.index : 0;
            slider.max = Math.max(0, options.length - 1);
        }
        var idx = model.state.index != null ? model.state.index : 0;
        if (readout) readout.textContent = options[idx] || "";
        if (label) label.textContent = model.state.description || "";
        if (slider) slider.disabled = model.state.disabled || false;
    };

    // --- IntRangeSlider ---
    function renderIntRangeSlider(model) {
        var el = document.createElement("div");
        el.className = "widget-intrangeslider";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var rangeContainer = document.createElement("div");
        rangeContainer.className = "widget-range-container";

        var lo = document.createElement("input");
        lo.type = "range";
        lo.className = "widget-range-lo";
        lo.min = model.state.min != null ? model.state.min : 0;
        lo.max = model.state.max != null ? model.state.max : 100;
        lo.step = model.state.step != null ? model.state.step : 1;
        var vals = model.state.value || [25, 75];
        lo.value = vals[0];
        if (model.state.disabled) lo.disabled = true;

        var hi = document.createElement("input");
        hi.type = "range";
        hi.className = "widget-range-hi";
        hi.min = lo.min;
        hi.max = lo.max;
        hi.step = lo.step;
        hi.value = vals[1];
        if (model.state.disabled) hi.disabled = true;

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = vals[0] + " - " + vals[1];

        lo.addEventListener("input", function() {
            if (parseInt(lo.value) > parseInt(hi.value)) lo.value = hi.value;
            var pair = [parseInt(lo.value), parseInt(hi.value)];
            readout.textContent = pair[0] + " - " + pair[1];
            model.state.value = pair;
            sendStateUpdate(model.comm_id, {value: pair});
            syncOtherViews(model, el);
        });
        hi.addEventListener("input", function() {
            if (parseInt(hi.value) < parseInt(lo.value)) hi.value = lo.value;
            var pair = [parseInt(lo.value), parseInt(hi.value)];
            readout.textContent = pair[0] + " - " + pair[1];
            model.state.value = pair;
            sendStateUpdate(model.comm_id, {value: pair});
            syncOtherViews(model, el);
        });

        rangeContainer.appendChild(lo);
        rangeContainer.appendChild(hi);
        el.appendChild(label);
        el.appendChild(rangeContainer);
        el.appendChild(readout);
        return el;
    }
    renderIntRangeSlider.update = function(model, el) {
        var lo = el.querySelector(".widget-range-lo");
        var hi = el.querySelector(".widget-range-hi");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var vals = model.state.value || [25, 75];
        if (lo && document.activeElement !== lo) lo.value = vals[0];
        if (hi && document.activeElement !== hi) hi.value = vals[1];
        if (readout) readout.textContent = vals[0] + " - " + vals[1];
        if (label) label.textContent = model.state.description || "";
        if (lo) lo.disabled = model.state.disabled || false;
        if (hi) hi.disabled = model.state.disabled || false;
    };

    // --- FloatRangeSlider ---
    function renderFloatRangeSlider(model) {
        var el = document.createElement("div");
        el.className = "widget-floatrangeslider";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var rangeContainer = document.createElement("div");
        rangeContainer.className = "widget-range-container";

        var lo = document.createElement("input");
        lo.type = "range";
        lo.className = "widget-range-lo";
        lo.min = model.state.min != null ? model.state.min : 0.0;
        lo.max = model.state.max != null ? model.state.max : 1.0;
        lo.step = model.state.step != null ? model.state.step : 0.01;
        var vals = model.state.value || [0.25, 0.75];
        lo.value = vals[0];
        if (model.state.disabled) lo.disabled = true;

        var hi = document.createElement("input");
        hi.type = "range";
        hi.className = "widget-range-hi";
        hi.min = lo.min;
        hi.max = lo.max;
        hi.step = lo.step;
        hi.value = vals[1];
        if (model.state.disabled) hi.disabled = true;

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = parseFloat(vals[0]).toFixed(2) + " - " + parseFloat(vals[1]).toFixed(2);

        lo.addEventListener("input", function() {
            if (parseFloat(lo.value) > parseFloat(hi.value)) lo.value = hi.value;
            var pair = [parseFloat(lo.value), parseFloat(hi.value)];
            readout.textContent = pair[0].toFixed(2) + " - " + pair[1].toFixed(2);
            model.state.value = pair;
            sendStateUpdate(model.comm_id, {value: pair});
            syncOtherViews(model, el);
        });
        hi.addEventListener("input", function() {
            if (parseFloat(hi.value) < parseFloat(lo.value)) hi.value = lo.value;
            var pair = [parseFloat(lo.value), parseFloat(hi.value)];
            readout.textContent = pair[0].toFixed(2) + " - " + pair[1].toFixed(2);
            model.state.value = pair;
            sendStateUpdate(model.comm_id, {value: pair});
            syncOtherViews(model, el);
        });

        rangeContainer.appendChild(lo);
        rangeContainer.appendChild(hi);
        el.appendChild(label);
        el.appendChild(rangeContainer);
        el.appendChild(readout);
        return el;
    }
    renderFloatRangeSlider.update = function(model, el) {
        var lo = el.querySelector(".widget-range-lo");
        var hi = el.querySelector(".widget-range-hi");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var vals = model.state.value || [0.25, 0.75];
        if (lo && document.activeElement !== lo) lo.value = vals[0];
        if (hi && document.activeElement !== hi) hi.value = vals[1];
        if (readout) readout.textContent = parseFloat(vals[0]).toFixed(2) + " - " + parseFloat(vals[1]).toFixed(2);
        if (label) label.textContent = model.state.description || "";
        if (lo) lo.disabled = model.state.disabled || false;
        if (hi) hi.disabled = model.state.disabled || false;
    };

    // --- FloatLogSlider ---
    function renderFloatLogSlider(model) {
        var el = document.createElement("div");
        el.className = "widget-floatlogslider";

        var base = model.state.base || 10;

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        // Slider operates in exponent space: min/max are exponents
        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = model.state.min != null ? model.state.min : -2;
        slider.max = model.state.max != null ? model.state.max : 4;
        slider.step = model.state.step != null ? model.state.step : 0.1;
        // Convert actual value to exponent for the slider
        var currentVal = model.state.value != null ? model.state.value : 1;
        slider.value = Math.log(currentVal) / Math.log(base);
        if (model.state.disabled) slider.disabled = true;
        if (model.state.orientation === "vertical") {
            el.classList.add("widget-vertical");
        }

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = currentVal.toPrecision(4);

        slider.addEventListener("input", function() {
            var exponent = parseFloat(slider.value);
            var val = Math.pow(base, exponent);
            readout.textContent = val.toPrecision(4);
            model.state.value = val;
            sendStateUpdate(model.comm_id, {value: val});
            syncOtherViews(model, el);
        });

        el.appendChild(label);
        el.appendChild(slider);
        el.appendChild(readout);
        return el;
    }
    renderFloatLogSlider.update = function(model, el) {
        var slider = el.querySelector("input[type=range]");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var base = model.state.base || 10;
        var val = model.state.value != null ? model.state.value : 1;
        if (slider && document.activeElement !== slider) {
            slider.value = Math.log(val) / Math.log(base);
        }
        if (readout) readout.textContent = val.toPrecision(4);
        if (label) label.textContent = model.state.description || "";
        if (slider) slider.disabled = model.state.disabled || false;
    };

    // --- FileUpload ---
    function fileUploadStatusText(value) {
        if (!Array.isArray(value) || value.length === 0) return "";
        var names = [];
        for (var i = 0; i < value.length; i++) {
            if (value[i] && value[i].name) names.push(value[i].name);
        }
        return names.join(", ");
    }

    function renderFileUpload(model) {
        var el = document.createElement("div");
        el.className = "widget-fileupload";

        var btn = document.createElement("button");
        btn.className = "widget-button";
        btn.textContent = model.state.description || "Upload";
        if (model.state.disabled) btn.disabled = true;
        if (model.state.button_style) btn.dataset.buttonStyle = model.state.button_style;

        var fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.style.display = "none";
        if (model.state.accept) fileInput.accept = model.state.accept;
        if (model.state.multiple) fileInput.multiple = true;

        var status = document.createElement("span");
        status.className = "widget-fileupload-status";
        // Pre-populate for late-joiners whose state.value is already set.
        status.textContent = fileUploadStatusText(model.state.value);

        btn.addEventListener("click", function() {
            fileInput.click();
        });

        fileInput.addEventListener("change", function() {
            var files = fileInput.files;
            if (files.length === 0) return;
            var names = [];
            for (var i = 0; i < files.length; i++) names.push(files[i].name);
            status.textContent = "Uploading: " + names.join(", ");

            var reads = [];
            for (var i = 0; i < files.length; i++) {
                (function(file) {
                    reads.push(new Promise(function(resolve, reject) {
                        var r = new FileReader();
                        r.onload = function() { resolve({ file: file, buf: r.result }); };
                        r.onerror = function() { reject(r.error || new Error("read failed")); };
                        r.readAsArrayBuffer(file);
                    }));
                })(files[i]);
            }

            Promise.all(reads).then(function(results) {
                // Build ipywidgets 8 FileUpload.value entries. `content` is a
                // buffer_paths placeholder - the kernel's _put_buffers will
                // inject the raw bytes at [value, i, content]. last_modified
                // is milliseconds since epoch; the kernel converts via
                // datetime.fromtimestamp(js['last_modified'] / 1000).
                var value = results.map(function(r) {
                    return {
                        name: r.file.name,
                        type: r.file.type || "",
                        size: r.file.size,
                        last_modified: r.file.lastModified,
                        content: null
                    };
                });
                var bufferPaths = results.map(function(_, i) {
                    return ["value", i, "content"];
                });
                var buffers = results.map(function(r) { return r.buf; });
                model.state.value = value;
                sendStateUpdate(model.comm_id, { value: value }, bufferPaths, buffers);
                status.textContent = names.join(", ");
            }).catch(function(err) {
                status.textContent = "Upload failed: " + (err && err.message ? err.message : err);
            });
        });

        el.appendChild(btn);
        el.appendChild(fileInput);
        el.appendChild(status);
        return el;
    }
    renderFileUpload.update = function(model, el) {
        var btn = el.querySelector("button");
        if (btn) {
            btn.textContent = model.state.description || "Upload";
            btn.disabled = model.state.disabled || false;
        }
        var status = el.querySelector(".widget-fileupload-status");
        if (status) {
            // Don't clobber transient "Uploading..." / "Upload failed: ..."
            // states on the uploader's own browser - leave those alone and
            // only refresh when we have a value to display.
            var text = fileUploadStatusText(model.state.value);
            if (text) status.textContent = text;
        }
    };

    // --- IntProgress ---
    function renderIntProgress(model) {
        var el = document.createElement("div");
        el.className = "widget-intprogress";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var container = document.createElement("div");
        container.className = "widget-progress-bar-container";

        var fill = document.createElement("div");
        fill.className = "widget-progress-bar-fill";
        if (model.state.bar_style) fill.dataset.barStyle = model.state.bar_style;

        var mn = model.state.min != null ? model.state.min : 0;
        var mx = model.state.max != null ? model.state.max : 100;
        var val = model.state.value != null ? model.state.value : 0;
        var pct = mx > mn ? ((val - mn) / (mx - mn)) * 100 : 0;

        if (model.state.orientation === "vertical") {
            el.classList.add("widget-progress-vertical");
            fill.style.height = pct + "%";
        } else {
            fill.style.width = pct + "%";
        }

        container.appendChild(fill);

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = val;

        el.appendChild(label);
        el.appendChild(container);
        el.appendChild(readout);
        return el;
    }
    renderIntProgress.update = function(model, el) {
        var fill = el.querySelector(".widget-progress-bar-fill");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var mn = model.state.min != null ? model.state.min : 0;
        var mx = model.state.max != null ? model.state.max : 100;
        var val = model.state.value != null ? model.state.value : 0;
        var pct = mx > mn ? ((val - mn) / (mx - mn)) * 100 : 0;
        if (fill) {
            if (el.classList.contains("widget-progress-vertical")) {
                fill.style.height = pct + "%";
            } else {
                fill.style.width = pct + "%";
            }
            if (model.state.bar_style) fill.dataset.barStyle = model.state.bar_style;
        }
        if (readout) readout.textContent = val;
        if (label) label.textContent = model.state.description || "";
    };

    // --- FloatProgress ---
    function renderFloatProgress(model) {
        var el = document.createElement("div");
        el.className = "widget-floatprogress";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var container = document.createElement("div");
        container.className = "widget-progress-bar-container";

        var fill = document.createElement("div");
        fill.className = "widget-progress-bar-fill";
        if (model.state.bar_style) fill.dataset.barStyle = model.state.bar_style;

        var mn = model.state.min != null ? model.state.min : 0.0;
        var mx = model.state.max != null ? model.state.max : 1.0;
        var val = model.state.value != null ? model.state.value : 0.0;
        var pct = mx > mn ? ((val - mn) / (mx - mn)) * 100 : 0;

        if (model.state.orientation === "vertical") {
            el.classList.add("widget-progress-vertical");
            fill.style.height = pct + "%";
        } else {
            fill.style.width = pct + "%";
        }

        container.appendChild(fill);

        var readout = document.createElement("span");
        readout.className = "widget-readout";
        readout.textContent = parseFloat(val).toFixed(2);

        el.appendChild(label);
        el.appendChild(container);
        el.appendChild(readout);
        return el;
    }
    renderFloatProgress.update = function(model, el) {
        var fill = el.querySelector(".widget-progress-bar-fill");
        var readout = el.querySelector(".widget-readout");
        var label = el.querySelector(".widget-label");
        var mn = model.state.min != null ? model.state.min : 0.0;
        var mx = model.state.max != null ? model.state.max : 1.0;
        var val = model.state.value != null ? model.state.value : 0.0;
        var pct = mx > mn ? ((val - mn) / (mx - mn)) * 100 : 0;
        if (fill) {
            if (el.classList.contains("widget-progress-vertical")) {
                fill.style.height = pct + "%";
            } else {
                fill.style.width = pct + "%";
            }
            if (model.state.bar_style) fill.dataset.barStyle = model.state.bar_style;
        }
        if (readout) readout.textContent = parseFloat(val).toFixed(2);
        if (label) label.textContent = model.state.description || "";
    };

    // --- Valid ---
    function renderValid(model) {
        var el = document.createElement("div");
        el.className = "widget-valid";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var icon = document.createElement("span");
        icon.className = "widget-valid-icon";
        icon.textContent = model.state.value ? "\u2714" : "\u2716";
        icon.style.color = model.state.value ? "#28a745" : "#dc3545";

        var readout = document.createElement("span");
        readout.className = "widget-valid-readout";
        readout.textContent = model.state.readout || "";

        el.appendChild(label);
        el.appendChild(icon);
        el.appendChild(readout);
        return el;
    }
    renderValid.update = function(model, el) {
        var icon = el.querySelector(".widget-valid-icon");
        if (icon) {
            icon.textContent = model.state.value ? "\u2714" : "\u2716";
            icon.style.color = model.state.value ? "#28a745" : "#dc3545";
        }
        var readout = el.querySelector(".widget-valid-readout");
        if (readout) readout.textContent = model.state.readout || "";
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
    };

    // --- Image (requires buffer reconstitution from Phase 0) ---
    function renderImage(model) {
        var el = document.createElement("div");
        el.className = "widget-image";

        var img = document.createElement("img");
        var format = model.state.format || "png";
        if (model.state.value) {
            img.src = "data:image/" + format + ";base64," + model.state.value;
        }
        if (model.state.width) img.width = parseInt(model.state.width);
        if (model.state.height) img.height = parseInt(model.state.height);

        el.appendChild(img);
        return el;
    }
    renderImage.update = function(model, el) {
        var img = el.querySelector("img");
        if (img) {
            var format = model.state.format || "png";
            if (model.state.value) {
                img.src = "data:image/" + format + ";base64," + model.state.value;
            }
            if (model.state.width) img.width = parseInt(model.state.width);
            if (model.state.height) img.height = parseInt(model.state.height);
        }
    };

    // --- Video (requires buffer reconstitution from Phase 0) ---
    function renderVideo(model) {
        var el = document.createElement("div");
        el.className = "widget-video";

        var video = document.createElement("video");
        if (model.state.controls !== false) video.controls = true;
        if (model.state.loop) video.loop = true;
        if (model.state.autoplay) { video.autoplay = true; video.muted = true; }
        if (model.state.width) video.width = parseInt(model.state.width);
        if (model.state.height) video.height = parseInt(model.state.height);

        if (model.state.value) {
            var format = model.state.format || "mp4";
            var binary = atob(model.state.value);
            var bytes = new Uint8Array(binary.length);
            for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            var blob = new Blob([bytes], {type: "video/" + format});
            video.src = URL.createObjectURL(blob);
            el._blobUrl = video.src;
        }

        el.appendChild(video);
        return el;
    }
    renderVideo.update = function(model, el) {
        var video = el.querySelector("video");
        if (video && model.state.value) {
            // Revoke old blob URL to prevent memory leaks
            if (el._blobUrl) URL.revokeObjectURL(el._blobUrl);
            var format = model.state.format || "mp4";
            var binary = atob(model.state.value);
            var bytes = new Uint8Array(binary.length);
            for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            var blob = new Blob([bytes], {type: "video/" + format});
            video.src = URL.createObjectURL(blob);
            el._blobUrl = video.src;
        }
    };

    // --- Audio (requires buffer reconstitution from Phase 0) ---
    function renderAudio(model) {
        var el = document.createElement("div");
        el.className = "widget-audio";

        var audio = document.createElement("audio");
        if (model.state.controls !== false) audio.controls = true;
        if (model.state.loop) audio.loop = true;
        if (model.state.autoplay) { audio.autoplay = true; }

        if (model.state.value) {
            var format = model.state.format || "mp3";
            var binary = atob(model.state.value);
            var bytes = new Uint8Array(binary.length);
            for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            var blob = new Blob([bytes], {type: "audio/" + format});
            audio.src = URL.createObjectURL(blob);
            el._blobUrl = audio.src;
        }

        el.appendChild(audio);
        return el;
    }
    renderAudio.update = function(model, el) {
        var audio = el.querySelector("audio");
        if (audio && model.state.value) {
            if (el._blobUrl) URL.revokeObjectURL(el._blobUrl);
            var format = model.state.format || "mp3";
            var binary = atob(model.state.value);
            var bytes = new Uint8Array(binary.length);
            for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            var blob = new Blob([bytes], {type: "audio/" + format});
            audio.src = URL.createObjectURL(blob);
            el._blobUrl = audio.src;
        }
    };

    // --- Play (animation controller) ---
    function renderPlay(model) {
        var el = document.createElement("div");
        el.className = "widget-play";

        var label = document.createElement("span");
        label.className = "widget-label";
        label.textContent = model.state.description || "";

        var controls = document.createElement("div");
        controls.className = "widget-play-controls";

        var playBtn = document.createElement("button");
        playBtn.className = "widget-play-btn";
        playBtn.textContent = "\u25B6";
        playBtn.title = "Play";
        if (model.state.disabled) playBtn.disabled = true;

        var pauseBtn = document.createElement("button");
        pauseBtn.className = "widget-play-btn";
        pauseBtn.textContent = "\u23F8";
        pauseBtn.title = "Pause";
        if (model.state.disabled) pauseBtn.disabled = true;

        var stopBtn = document.createElement("button");
        stopBtn.className = "widget-play-btn";
        stopBtn.textContent = "\u23F9";
        stopBtn.title = "Stop";
        if (model.state.disabled) stopBtn.disabled = true;

        var valueDisplay = document.createElement("span");
        valueDisplay.className = "widget-play-value";
        valueDisplay.textContent = model.state.value != null ? model.state.value : 0;

        function startPlaying() {
            if (el._playInterval) return;
            var interval = model.state.interval || 100;
            var step = model.state.step || 1;
            var mn = model.state.min != null ? model.state.min : 0;
            var mx = model.state.max != null ? model.state.max : 100;
            el._playInterval = setInterval(function() {
                var val = (model.state.value != null ? model.state.value : mn) + step;
                if (val > mx) {
                    if (model.state._repeat) {
                        val = mn;
                    } else {
                        clearInterval(el._playInterval);
                        el._playInterval = null;
                        model.state._playing = false;
                        sendStateUpdate(model.comm_id, {_playing: false});
                        syncOtherViews(model, el);
                        return;
                    }
                }
                model.state.value = val;
                valueDisplay.textContent = val;
                sendStateUpdate(model.comm_id, {value: val});
                syncOtherViews(model, el);
            }, interval);
        }

        function stopPlaying() {
            if (el._playInterval) {
                clearInterval(el._playInterval);
                el._playInterval = null;
            }
        }

        playBtn.addEventListener("click", function() {
            model.state._playing = true;
            sendStateUpdate(model.comm_id, {_playing: true});
            startPlaying();
        });

        pauseBtn.addEventListener("click", function() {
            model.state._playing = false;
            sendStateUpdate(model.comm_id, {_playing: false});
            stopPlaying();
        });

        stopBtn.addEventListener("click", function() {
            stopPlaying();
            var mn = model.state.min != null ? model.state.min : 0;
            model.state._playing = false;
            model.state.value = mn;
            valueDisplay.textContent = mn;
            sendStateUpdate(model.comm_id, {_playing: false, value: mn});
            syncOtherViews(model, el);
        });

        // Start playing if kernel says so
        if (model.state._playing) startPlaying();

        controls.appendChild(playBtn);
        controls.appendChild(pauseBtn);
        controls.appendChild(stopBtn);
        el.appendChild(label);
        el.appendChild(controls);
        el.appendChild(valueDisplay);
        return el;
    }
    renderPlay.update = function(model, el) {
        var valueDisplay = el.querySelector(".widget-play-value");
        if (valueDisplay) valueDisplay.textContent = model.state.value != null ? model.state.value : 0;
        var label = el.querySelector(".widget-label");
        if (label) label.textContent = model.state.description || "";
        // Sync play/pause state from kernel
        if (model.state._playing && !el._playInterval) {
            // Kernel started play - need a reference to startPlaying logic
            var interval = model.state.interval || 100;
            var step = model.state.step || 1;
            var mn = model.state.min != null ? model.state.min : 0;
            var mx = model.state.max != null ? model.state.max : 100;
            el._playInterval = setInterval(function() {
                var val = (model.state.value != null ? model.state.value : mn) + step;
                if (val > mx) {
                    if (model.state._repeat) { val = mn; } else {
                        clearInterval(el._playInterval);
                        el._playInterval = null;
                        model.state._playing = false;
                        sendStateUpdate(model.comm_id, {_playing: false});
                        syncOtherViews(model, el);
                        return;
                    }
                }
                model.state.value = val;
                if (valueDisplay) valueDisplay.textContent = val;
                sendStateUpdate(model.comm_id, {value: val});
                syncOtherViews(model, el);
            }, interval);
        } else if (!model.state._playing && el._playInterval) {
            clearInterval(el._playInterval);
            el._playInterval = null;
        }
        var btns = el.querySelectorAll(".widget-play-btn");
        for (var i = 0; i < btns.length; i++) {
            btns[i].disabled = model.state.disabled || false;
        }
    };

    // --- Tab ---
    function renderTab(model) {
        var el = document.createElement("div");
        el.className = "widget-tab";

        var tabBar = document.createElement("div");
        tabBar.className = "widget-tab-bar";

        var contentArea = document.createElement("div");
        contentArea.className = "widget-tab-content";

        var children = model.state.children || [];
        var titles = model.state.titles || [];
        var selectedIndex = model.state.selected_index != null ? model.state.selected_index : 0;

        for (var i = 0; i < children.length; i++) {
            var btn = document.createElement("button");
            btn.className = "widget-tab-btn" + (i === selectedIndex ? " active" : "");
            btn.textContent = titles[i] || ("Tab " + i);
            (function(idx) {
                btn.addEventListener("click", function() {
                    model.state.selected_index = idx;
                    // Update tab bar active state
                    var btns = tabBar.querySelectorAll(".widget-tab-btn");
                    for (var j = 0; j < btns.length; j++) {
                        btns[j].classList.toggle("active", j === idx);
                    }
                    // Show/hide panels
                    var panels = contentArea.children;
                    for (var j = 0; j < panels.length; j++) {
                        panels[j].classList.toggle("widget-tab-visible", j === idx);
                    }
                    sendStateUpdate(model.comm_id, {selected_index: idx});
                    syncOtherViews(model, el);
                });
            })(i);
            tabBar.appendChild(btn);

            var panel = document.createElement("div");
            panel.className = "widget-tab-panel" + (i === selectedIndex ? " widget-tab-visible" : "");
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, panel);
            contentArea.appendChild(panel);
        }

        el.appendChild(tabBar);
        el.appendChild(contentArea);
        return el;
    }
    renderTab.update = function(model, el) {
        var selectedIndex = model.state.selected_index != null ? model.state.selected_index : 0;
        var btns = el.querySelectorAll(".widget-tab-btn");
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle("active", i === selectedIndex);
            var titles = model.state.titles || [];
            btns[i].textContent = titles[i] || ("Tab " + i);
        }
        var panels = el.querySelectorAll(".widget-tab-panel");
        for (var i = 0; i < panels.length; i++) {
            panels[i].classList.toggle("widget-tab-visible", i === selectedIndex);
        }
    };

    // --- Accordion ---
    function renderAccordion(model) {
        var el = document.createElement("div");
        el.className = "widget-accordion";

        var children = model.state.children || [];
        var titles = model.state.titles || [];
        var selectedIndex = model.state.selected_index;

        for (var i = 0; i < children.length; i++) {
            var panel = document.createElement("div");
            panel.className = "widget-accordion-panel";

            var header = document.createElement("button");
            header.className = "widget-accordion-header";
            header.textContent = titles[i] || ("Section " + i);

            var body = document.createElement("div");
            body.className = "widget-accordion-body" + (i === selectedIndex ? " open" : "");

            (function(idx, hdr) {
                hdr.addEventListener("click", function() {
                    // Toggle: close if same, open if different
                    var newIdx = (model.state.selected_index === idx) ? null : idx;
                    model.state.selected_index = newIdx;
                    var bodies = el.querySelectorAll(".widget-accordion-body");
                    for (var j = 0; j < bodies.length; j++) {
                        bodies[j].classList.toggle("open", j === newIdx);
                    }
                    sendStateUpdate(model.comm_id, {selected_index: newIdx});
                    syncOtherViews(model, el);
                });
            })(i, header);

            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, body);

            panel.appendChild(header);
            panel.appendChild(body);
            el.appendChild(panel);
        }

        return el;
    }
    renderAccordion.update = function(model, el) {
        var selectedIndex = model.state.selected_index;
        var headers = el.querySelectorAll(".widget-accordion-header");
        var bodies = el.querySelectorAll(".widget-accordion-body");
        var titles = model.state.titles || [];
        for (var i = 0; i < headers.length; i++) {
            headers[i].textContent = titles[i] || ("Section " + i);
        }
        for (var i = 0; i < bodies.length; i++) {
            bodies[i].classList.toggle("open", i === selectedIndex);
        }
    };

    // --- Stack ---
    function renderStack(model) {
        var el = document.createElement("div");
        el.className = "widget-stack";

        var children = model.state.children || [];
        var selectedIndex = model.state.selected_index != null ? model.state.selected_index : 0;

        for (var i = 0; i < children.length; i++) {
            var panel = document.createElement("div");
            panel.className = "widget-stack-panel" + (i === selectedIndex ? " widget-stack-visible" : "");
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, panel);
            el.appendChild(panel);
        }

        return el;
    }
    renderStack.update = function(model, el) {
        var selectedIndex = model.state.selected_index != null ? model.state.selected_index : 0;
        var panels = el.querySelectorAll(".widget-stack-panel");
        for (var i = 0; i < panels.length; i++) {
            panels[i].classList.toggle("widget-stack-visible", i === selectedIndex);
        }
    };

    // --- GridBox ---
    function applyLayoutStyles(el, layoutRef) {
        // Look up the LayoutModel by stripping IPY_MODEL_ prefix
        if (!layoutRef) return;
        var layoutId = layoutRef.replace("IPY_MODEL_", "");
        var layoutModel = widgetModels[layoutId];
        if (!layoutModel) return;
        var s = layoutModel.state;
        // Map ipywidgets layout properties (underscored) to CSS (hyphenated)
        var mappings = {
            grid_template_columns: "gridTemplateColumns",
            grid_template_rows: "gridTemplateRows",
            grid_gap: "gap",
            grid_template_areas: "gridTemplateAreas",
            width: "width",
            height: "height",
            overflow: "overflow",
            border: "border",
            margin: "margin",
            padding: "padding"
        };
        for (var key in mappings) {
            if (s[key]) el.style[mappings[key]] = s[key];
        }
    }

    function renderGridBox(model) {
        var el = document.createElement("div");
        el.className = "widget-gridbox";

        applyLayoutStyles(el, model.state.layout);

        var children = model.state.children || [];
        for (var i = 0; i < children.length; i++) {
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, el);
        }
        return el;
    }
    renderGridBox.update = function(model, el) {
        applyLayoutStyles(el, model.state.layout);
        el.innerHTML = "";
        var children = model.state.children || [];
        for (var i = 0; i < children.length; i++) {
            var childId = children[i].replace("IPY_MODEL_", "");
            renderWidgetView(childId, el);
        }
    };

    // --- Link / DirectionalLink (invisible - jslink/jsdlink) ---
    function renderLink(model) {
        var el = document.createElement("span");
        el.style.display = "none";
        return el;
    }
    renderLink.update = function() {};

    // ===== Cell Execution =====

    window.runJupyterCode = async function(button) {
        var container = button.parentElement.parentElement;
        var codeContainer = container.querySelector('.jupyter-code');
        var outputArea = container.querySelector('.jupyter-output');
        var formattedCode = container.querySelector(".jupyter-formatted");

        // Sync CM → textarea if edit mode is active BEFORE reading code value
        var editButton = container.querySelector(".jupyter-edit");
        if (editButton.ariaPressed === "true") {
            var editEntry = cmInstances.get(container);
            if (editEntry) {
                codeContainer.value = editEntry.view.state.doc.toString();
                editEntry.cmContainer.style.display = "none";
            }
            editButton.ariaPressed = "false";
            update_formatted_code(button);
        }

        var code = codeContainer.value;

        // cell_id only routes execution results back to THIS cell's output area;
        // it must be unique per DOM element, not per code text. Two cells with
        // identical code (common once the same page is embedded, or a shared
        // boilerplate cell is transcluded onto a page that already has one) would
        // otherwise share a single pendingExecutions slot and cross-wire their
        // output / stick a run button. Assign a stable unique id on first run
        // (content hash for readability + a monotonic suffix) and reuse it for
        // every later run of the same cell. The kernel just echoes cell_id back,
        // so its format is free to change and nothing persists on it.
        var cellId = container.dataset.execId;
        if (!cellId) {
            window.__jupyterCellSeq = (window.__jupyterCellSeq || 0) + 1;
            cellId = (container.dataset.cellHash || "cell") + "-" + window.__jupyterCellSeq;
            container.dataset.execId = cellId;
        }

        // Clear and show output
        outputArea.innerHTML = "";
        formattedCode.style.display = "none";
        outputArea.style.display = "block";
        codeContainer.style.display = "none";

        // Disable button
        button.disabled = true;
        var originalText = button.innerText;
        button.innerText = "\u{1F680}";
        button.title = "Running...";

        // Register pending execution
        pendingExecutions[cellId] = {
            button: button,
            outputArea: outputArea,
            originalText: originalText
        };

        try {
            var ws = await getOrCreateSocket();
            if (ws.readyState !== WebSocket.OPEN) {
                throw new Error("WebSocket not open");
            }
            ws.send(JSON.stringify({
                action: "execute",
                code: code,
                cell_id: cellId
            }));
        } catch (err) {
            outputArea.insertAdjacentHTML('beforeend', "<pre>[Connection Error]</pre>");
            button.disabled = false;
            button.innerText = originalText;
            button.title = "Run";
            delete pendingExecutions[cellId];
        }
    };

    // ===== Existing functions (preserved) =====

    window.runJupyterEdit = function(button) {
        var container = button.parentElement.parentElement;
        var codeContainer = container.querySelector('.jupyter-code');
        var outputArea = container.querySelector('.jupyter-output');
        var formattedCode = container.querySelector(".jupyter-formatted");

        if (button.ariaPressed === "true") {
            // Toggle OFF: sync CM → textarea, show formatted code
            button.ariaPressed = "false";
            var offEntry = cmInstances.get(container);
            if (offEntry) {
                codeContainer.value = offEntry.view.state.doc.toString();
                offEntry.cmContainer.style.display = "none";
            }
            update_formatted_code(button);
            formattedCode.style.display = "block";
            outputArea.style.display = "none";
            codeContainer.style.display = "none";
        } else {
            // Toggle ON: show CM editor (create on first use)
            button.ariaPressed = "true";
            formattedCode.style.display = "none";
            outputArea.style.display = "none";
            codeContainer.style.display = "none"; // keep textarea always hidden

            if (typeof CMEditor === 'undefined') {
                // Graceful fallback if bundle not loaded
                codeContainer.style.display = "block";
                return;
            }

            var onEntry = cmInstances.get(container);
            if (!onEntry) {
                // First open: create CM instance
                var themeComp = new CMEditor.Compartment();
                var cmContainer = document.createElement('div');
                cmContainer.className = 'jupyter-cm-editor';
                cmContainer.style.cssText = 'resize:vertical; overflow:hidden; height:200px; min-height:60px; border:1px solid #888;';
                codeContainer.insertAdjacentElement('afterend', cmContainer);

                var view = new CMEditor.EditorView({
                    parent: cmContainer,
                    state: CMEditor.EditorState.create({
                        doc: codeContainer.value,
                        extensions: [
                            CMEditor.lineNumbers(),
                            CMEditor.highlightActiveLine(),
                            CMEditor.drawSelection(),
                            CMEditor.bracketMatching(),
                            CMEditor.closeBrackets(),
                            CMEditor.indentUnit.of("    "),
                            CMEditor.indentOnInput(),
                            CMEditor.history(),
                            CMEditor.highlightSelectionMatches(),
                            CMEditor.syntaxHighlighting(CMEditor.defaultHighlightStyle, { fallback: true }),
                            CMEditor.python(),
                            CMEditor.keymap.of([
                                CMEditor.indentWithTab,
                                ...CMEditor.closeBracketsKeymap,
                                ...CMEditor.defaultKeymap,
                                ...CMEditor.searchKeymap,
                                ...CMEditor.historyKeymap,
                            ]),
                            themeComp.of(cmGetTheme()),
                            CMEditor.EditorView.theme({
                                "&": { height: "100%" },
                                ".cm-scroller": { overflow: "auto" },
                                ".cm-content": { fontFamily: "'Courier New', Courier,monospace", fontSize: "13pt" },
                                ".cm-gutters": { fontFamily: "'Courier New', Courier,monospace", fontSize: "13pt" },
                            }),
                        ],
                    }),
                });
                onEntry = { view, cmContainer, themeComp };
                cmInstances.set(container, onEntry);
                new ResizeObserver(() => view.requestMeasure()).observe(cmContainer);
            } else {
                // Subsequent open: just show existing CM
                onEntry.cmContainer.style.display = "block";
            }
            onEntry.view.focus();
        }
    };

    window.runJupyterClear = function(button) {
        var container = button.parentElement.parentElement;
        var codeContainer = container.querySelector('.jupyter-code');
        var outputArea = container.querySelector('.jupyter-output');
        var formattedCode = container.querySelector(".jupyter-formatted");
        outputArea.innerHTML = "";

        var editButton = container.querySelector(".jupyter-edit");
        editButton.ariaPressed = "false";

        // Reset CM to original code if instance exists
        var clearEntry = cmInstances.get(container);
        if (clearEntry) {
            clearEntry.view.dispatch({
                changes: { from: 0, to: clearEntry.view.state.doc.length, insert: codeContainer.defaultValue }
            });
            clearEntry.cmContainer.style.display = "none";
        }

        formattedCode.style.display = "block";
        outputArea.style.display = "none";
        codeContainer.style.display = "none";

        codeContainer.value = codeContainer.defaultValue;
        update_formatted_code(button);
    };

})();

async function update_formatted_code(button) {
    const container = button.parentElement.parentElement;
    const codeContainer = container.querySelector('.jupyter-code');
    const formattedCode = container.querySelector(".jupyter-formatted");
    const code = codeContainer.value;

    const payload = {
        code: code,
        attrs: {class: "language-python"}
    };

    const response = await fetch("/api/markdown/code/", {
        method: "POST",
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload)
    });

    const new_formatted_code = await response.text();
    formattedCode.innerHTML = new_formatted_code;
}
