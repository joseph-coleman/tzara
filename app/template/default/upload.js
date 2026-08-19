// Copyright (C) 2026 Joseph E. Coleman
// This file is part of Tzara, licensed under the GNU Affero General
// Public License v3.0 or later. See LICENSE.txt.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Drag-and-drop upload queue for the editor page.
//
// Wires the #dropZone / #fileInput controls, streams each file to
// /upload-stream with a per-file progress bar, and inserts the returned
// markdown at the CodeMirror cursor (window.cmView). Top-level functions
// (queueUpload, insertAtCursor) are intentionally global so edit.html's
// CodeMirror drop handler can hand dropped files straight to queueUpload().

let uploadQueue = [];
let isUploading = false;
let uploadIdCounter = 0;

document.addEventListener("DOMContentLoaded",  () => {

    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');

    // Nothing to wire on pages without the upload UI (defensive; this module
    // is only loaded on the editor page).
    if (!dropZone || !fileInput) return;

    // Click to browse
    dropZone.addEventListener('click', () => {
        fileInput.click();
    });

    // File selected via browse
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            for (const file of e.target.files) {
                queueUpload(file);
            }
            fileInput.value = ''; // Reset input
        }
    });

    // Drag and drop events
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        dropZone.classList.add('dragover');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');

        if (e.dataTransfer.files.length > 0) {
            for (const file of e.dataTransfer.files) {
                queueUpload(file);
            }
        }
    });
});

function insertAtCursor(text) {
    const view = window.cmView;
    const cursor = view.state.selection.main.head;
    view.dispatch({
        changes: { from: cursor, to: cursor, insert: text },
        selection: { anchor: cursor + text.length },
    });
    view.focus();
}

function queueUpload(file) {
    const uploadsContainer = document.getElementById('uploadsContainer');
    const uploadId = 'upload-' + (uploadIdCounter++);

    // Create UI element for this upload
    const uploadItem = document.createElement('div');
    uploadItem.className = 'upload-item';
    uploadItem.id = uploadId;
    uploadItem.innerHTML = `
        <div class="upload-filename">${file.name}</div>
        <div class="upload-progress-bar">
            <div class="upload-progress-fill"></div>
            <div class="upload-progress-text">Queued ...</div>
        </div>
    `;
    uploadsContainer.appendChild(uploadItem);

    // Add to queue
    uploadQueue.push({ id: uploadId, file: file });
    updateQueueInfo();

    // Start processing if not already uploading
    if (!isUploading) {
        processQueue();
    }
}

function updateQueueInfo() {
    const queueInfo = document.getElementById('queueInfo');
    if (uploadQueue.length > 0) {
        queueInfo.textContent = `${uploadQueue.length} file(s) in queue`;
    } else {
        queueInfo.textContent = '';
    }
}

async function processQueue() {
    if (uploadQueue.length === 0) {
        isUploading = false;
        updateQueueInfo();
        return;
    }

    isUploading = true;
    const { id, file } = uploadQueue.shift();
    updateQueueInfo();

    await uploadFile(id, file);

    // Process next file
    processQueue();
}

function uploadFile(uploadId, file) {
    return new Promise((resolve) => {
        const uploadItem = document.getElementById(uploadId);
        const progressFill = uploadItem.querySelector('.upload-progress-fill');

        const statusText = uploadItem.querySelector('.upload-progress-text');

        const edit_form = document.querySelector("form[name='edit_document_form']");
        const input_document_name = edit_form.querySelector("input[name='document_name']");
        const document_name = input_document_name.value;

        uploadItem.classList.add('uploading');

        // Create form data
        const formData = new FormData();
        formData.append('file', file);
        formData.append("document_name", document_name);

        // Create XMLHttpRequest for progress tracking
        const xhr = new XMLHttpRequest();

        // Track upload progress
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const percentComplete = Math.round((e.loaded / e.total) * 100);
                progressFill.style.width = percentComplete + '%';

                const mbLoaded = (e.loaded / (1024 * 1024)).toFixed(2);
                const mbTotal = (e.total / (1024 * 1024)).toFixed(2);
                statusText.textContent = ` Uploading: ${mbLoaded} MB / ${mbTotal} MB `;

                if (percentComplete < 50) {
                    statusText.classList.add("progress-text-right")
                    statusText.classList.remove("progress-text-left")
                } else {
                    statusText.classList.remove("progress-text-right")
                    statusText.classList.add("progress-text-left")
                }

            }
        });

        // Handle completion
        xhr.addEventListener('load', () => {
            uploadItem.classList.remove('uploading');

            if (xhr.status === 200) {
                progressFill.style.width = '100%';
                statusText.textContent = ' ✓ Upload complete! ';
                uploadItem.classList.add('complete');

                try {
                    const response = JSON.parse(xhr.responseText);
                    console.log('Server response:', response);

                    insertAtCursor(response.markdownText);

                } catch (e) {
                    console.log('Raw response:', xhr.responseText);
                }
            } else {
                statusText.textContent = ` ✗ Upload failed (${xhr.status}) `;
                uploadItem.classList.add('error');
            }

            resolve();
        });

        // Handle errors
        xhr.addEventListener('error', () => {
            uploadItem.classList.remove('uploading');
            statusText.textContent =  ' ✗ Upload failed - Network error ';
            uploadItem.classList.add('error');
            resolve();
        });

        // Send request
        xhr.open('POST', '/upload-stream');
        xhr.send(formData);
    });
}
