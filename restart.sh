#!/usr/bin/env bash
# Restart (do NOT rebuild) the app to pick up .env changes (macOS / Linux).
# Windows: use restart.bat, which does the same thing.
#
# env_file values are read only when a container is created, so a plain
# `restart` would keep the old environment. --force-recreate makes new
# containers from the SAME image; --no-build guarantees no rebuild.
# Only tzaraserver/tzaraworker consume .env, so only those are recreated.
#
# Keep this file LF-only: a CR in the shebang breaks the interpreter under WSL.
set -euo pipefail
cd "$(dirname "$0")"

docker-compose up -d --force-recreate --no-build tzaraserver tzaraworker

echo "[restart] Done. server + worker recreated with the current .env."
