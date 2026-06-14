#!/usr/bin/env bash
# Load secrets from env files and start the webhook listener
set -euo pipefail

# Export all variables from sourced files
set -a

# Matrix credentials
source ~/.secrets/matrix-forge.env

# Plane API token (for project cache)
source ~/.secrets/plane.env

# Webhook-specific config (overrides MATRIX_HOMESERVER, adds PLANE_WEBHOOK_SECRET etc.)
source ~/.secrets/plane-webhook.env

set +a

# Remap variable names to what the app expects
export MATRIX_HOMESERVER="${MATRIX_HOMESERVER:-${MATRIX_HOMESERVER_URL:-}}"
export PLANE_API_TOKEN="${PLANE_API_TOKEN:-${PLANE_TOKEN_DEVELOPER:-}}"
export PORT="${PORT:-3006}"

exec python3 /home/ted/repos/personal/plane-webhook-listener/main.py
