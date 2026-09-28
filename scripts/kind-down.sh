#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - delete the local kind cluster (Phase 4).
#
# Safe to re-run: prints a message and exits 0 when the cluster is absent.
#
#   make kind-down
#   CLUSTER_NAME=<name> scripts/kind-down.sh
# =============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-kubecommerce}"

log()  { printf '[kind-down] %s\n' "$*"; }
die()  { printf '[kind-down] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: scripts/kind-down.sh [-h|--help]

Deletes the local kind cluster. No-op (exit 0) when it does not exist.

Environment:
  CLUSTER_NAME   cluster name to delete (default kubecommerce)
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $arg (try --help)" ;;
  esac
done

command -v kind >/dev/null 2>&1 \
  || die "kind not found. Install kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "deleting kind cluster '$CLUSTER_NAME'"
  kind delete cluster --name "$CLUSTER_NAME"
  log "kind cluster '$CLUSTER_NAME' deleted"
else
  log "kind cluster '$CLUSTER_NAME' not found; nothing to do"
fi
