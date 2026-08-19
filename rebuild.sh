#!/usr/bin/env bash
# Robust rebuild for Tzara's code-bearing containers (macOS / Linux).
# Windows: use rebuild.bat, which does the same thing.
#
# WHY this shape: the WORKER and the JUPYTER images BAKE the app code into the
# image at build time - only the browser server bind-mounts src/. So any change
# to worker code (agents, the agent-API, the editor-kernel broker, tasks) needs
# a real IMAGE rebuild, not just a container recreate.
#
# Rebuilding by compose SERVICE name with `docker-compose build` rebuilds the
# image from the Dockerfile against the current source: no dependence on image
# names, and no `docker rmi` (which fails anyway while the image is still in
# use, silently leaving every container on STALE code). Then force-recreate the
# containers from the fresh images. It is cache-aware: unchanged heavy layers
# (pip installs) stay cached; changed source busts only the COPY layer onward.
#
# pgserver / redisserver / ollamaserver are left running (their data + pulled
# models persist; no reason to churn them on a code rebuild).
#
# Keep this file LF-only: a CR in the shebang breaks the interpreter under WSL.
set -euo pipefail
cd "$(dirname "$0")"

echo "[rebuild] Building images for the code services (cache-aware)..."
if ! docker-compose build tzaraserver tzaraworker jupyterserver; then
    echo "[rebuild] BUILD FAILED - aborting. Containers left running OLD code." >&2
    exit 1
fi

echo "[rebuild] Recreating containers from the freshly built images..."
if ! docker-compose up -d --force-recreate tzaraserver tzaraworker jupyterserver jupyterserver-agent; then
    echo "[rebuild] up FAILED - check 'docker-compose ps' / logs." >&2
    exit 1
fi

echo "[rebuild] Done. server + worker + jupyter (both) now running current code."
