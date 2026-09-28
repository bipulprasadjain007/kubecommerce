#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - run the k6 order-flow load test.
#
# Environment:
#   BASE_URL            gateway base URL        (default http://localhost:8080)
#   CATALOG_URL         catalog direct URL      (default http://localhost:8002)
#   INTERNAL_API_TOKEN  internal mutation token (default from .env or dev value)
#   VUS                 peak virtual users      (default 20)
#   DURATION            sustained stage length  (default 2m)
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v k6 >/dev/null 2>&1; then
  echo "ERROR: k6 is not installed." >&2
  echo "Install it (no sudo needed for the release binary):" >&2
  echo "  curl -sSL https://github.com/grafana/k6/releases/latest/download/k6-v0.54.0-linux-amd64.tar.gz \\" >&2
  echo "    | tar -xz -C /tmp && install /tmp/k6-*/k6 \"\$HOME/.local/bin/k6\"" >&2
  echo "  # or, if available:  sudo apt-get install k6" >&2
  echo "  # verify:            k6 version" >&2
  exit 1
fi

BASE_URL="${BASE_URL:-http://localhost:8080}"
CATALOG_URL="${CATALOG_URL:-http://localhost:8002}"
VUS="${VUS:-20}"
DURATION="${DURATION:-2m}"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN:-dev-internal-token-change-me}"

export BASE_URL CATALOG_URL VUS DURATION

echo "[load-test] BASE_URL=$BASE_URL VUS=$VUS DURATION=$DURATION"
mkdir -p artifacts
SUMMARY="artifacts/k6-order-flow-summary.json"

set +e
# The script's handleSummary() writes $SUMMARY itself.
k6 run \
  -e BASE_URL="$BASE_URL" \
  -e CATALOG_URL="$CATALOG_URL" \
  -e INTERNAL_API_TOKEN="$INTERNAL_API_TOKEN" \
  -e VUS="$VUS" \
  -e DURATION="$DURATION" \
  tests/load/order-flow.js
rc=$?
set -e

if [ -f "$SUMMARY" ]; then
  echo "[load-test] result summary written to $SUMMARY"
else
  echo "[load-test] no summary file produced"
fi
exit "$rc"
