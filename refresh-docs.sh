#!/usr/bin/env bash
# Force-refresh the shipped help documentation into your system vault.
#
# Runs inside the SERVER container, which bind-mounts app/ and so sees the live
# seed tree (the worker bakes its copy at build time). Running the script on the
# host instead would have to re-derive vault paths from .env -- inside the
# container it is just config.vault_abs_root(SYSTEM_VAULT).
#
#   ./refresh-docs.sh                      # dry run: report what would change
#   ./refresh-docs.sh --apply              # refresh docs that already exist
#   ./refresh-docs.sh --apply --restore-missing   # also re-add absent docs
#
# Nothing is ever deleted, and every overwrite commits its pre-image first, so
# the run is revertable from the vault's git history.
set -euo pipefail

CONTAINER="${TZARA_CONTAINER:-tzara-tzaraserver-1}"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
    echo "Container '$CONTAINER' is not running. Start the stack first" >&2
    echo "(docker-compose up -d), or set TZARA_CONTAINER to override." >&2
    exit 2
fi

exec docker exec "$CONTAINER" python -u scripts/refresh_seed_docs.py "$@"
