#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - local kind cluster bootstrap (Phase 4).
#
# Idempotent and safe to re-run. It NEVER deletes an existing cluster.
#
#   make kind-up                 # create/reuse the cluster and install platform
#   scripts/kind-up.sh --with-argocd
#
# Steps:
#   1. preflight docker (daemon included), kind, kubectl, helm
#   2. create the kind cluster (skipped if it already exists)
#   3. when CNI=calico, install the pinned Calico operator + Installation and
#      wait for calico-system to become available (default CNI=kindnet)
#   4. install Gateway API CRDs (pinned) and wait for Established
#   5. install Envoy Gateway via its Helm OCI chart (pinned); GatewayClass/
#      Gateway are owned by kubecommerce-gitops (Phase D lane 3), not here
#   6. install metrics-server (pinned) with --kubelet-insecure-tls for kind
#   7. create/relabel namespaces and apply Pod Security Admission labels
#   8. optionally bootstrap Argo CD (Phase 8) with --with-argocd
#
# Every external artifact version below is pinned and overridable via env.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# -----------------------------------------------------------------------------
# Pinned external artifacts (ALL overridable via environment).
#
# Verified 2026-09-26 against upstream releases:
#   * KIND_NODE_IMAGE  : kind v0.33-shipped node image (kindest/node v1.36.4,
#                        digest-pinned). kube-prometheus-stack is tested only
#                        through Kubernetes 1.36, so k8s 1.37 is intentionally
#                        not used. OVERRIDE if your kind version documents a
#                        different default.
#   * GATEWAY_API_VERSION          : Gateway API latest stable release v1.6.2.
#   * ENVOY_GATEWAY_CHART_VERSION  : Envoy Gateway Helm chart v1.9.1.
#   * METRICS_SERVER_CHART_VERSION : metrics-server Helm chart 3.14.0.
#   * CALICO_VERSION               : Calico stable release v3.32.2 (only used
#                                    when CNI=calico; see the Calico block).
# -----------------------------------------------------------------------------
CLUSTER_NAME="${CLUSTER_NAME:-kubecommerce}"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed}"
WORKERS="${WORKERS:-2}"
GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.6.2}"
ENVOY_GATEWAY_CHART_VERSION="${ENVOY_GATEWAY_CHART_VERSION:-v1.9.1}"
METRICS_SERVER_CHART_VERSION="${METRICS_SERVER_CHART_VERSION:-3.14.0}"

# Calico CNI profile (opt-in; see CNI below). The version is pinned explicitly:
# v3.32.2 was the latest stable Calico release on 2026-09-26 (GitHub releases
# page and the v3.32.2 manifests both resolved). Bump deliberately and re-test.
CALICO_VERSION="${CALICO_VERSION:-v3.32.2}"
CALICO_OPERATOR_URL="${CALICO_OPERATOR_URL:-https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml}"
CALICO_POD_CIDR="${CALICO_POD_CIDR:-10.244.0.0/16}"

# CNI selects the cluster CNI. The kind default (kindnet) does NOT enforce
# NetworkPolicy; CNI=calico installs an enforcing CNI (see the notice below).
CNI="${CNI:-kindnet}"

# --- Fixed names (not artifact versions; still overridable) -------------------
ENVOY_GATEWAY_NAMESPACE="${ENVOY_GATEWAY_NAMESPACE:-envoy-gateway-system}"
METRICS_SERVER_REPO_NAME="${METRICS_SERVER_REPO_NAME:-metrics-server}"
METRICS_SERVER_REPO_URL="${METRICS_SERVER_REPO_URL:-https://kubernetes-sigs.github.io/metrics-server/}"
ARGOCD_INSTALL="${REPO_ROOT}/kubecommerce-gitops/bootstrap/argocd/install.sh"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"

WITH_ARGOCD=0

log()  { printf '[kind-up] %s\n' "$*"; }
warn() { printf '[kind-up] WARNING: %s\n' "$*" >&2; }
die()  { printf '[kind-up] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: scripts/kind-up.sh [--with-argocd] [-h|--help]

Creates (or reuses) the local kind cluster and installs the Phase 4 platform:
Gateway API CRDs, Envoy Gateway and metrics-server, plus the KubeCommerce
namespaces with Pod Security Admission labels.

Environment overrides (all optional):
  CLUSTER_NAME                   cluster name                     (kubecommerce)
  KIND_NODE_IMAGE                kindest/node image (pinned, k8s 1.36) (kind v0.33 default)
  WORKERS                        number of worker nodes            (2)
  CNI                            kindnet (no NetworkPolicy) | calico (kindnet)
  CALICO_VERSION                 Calico release pinned for CNI=calico (v3.32.2)
  CALICO_POD_CIDR                Calico pod CIDR; must match kind  (10.244.0.0/16)
  GATEWAY_API_VERSION            Gateway API CRDs release          (v1.6.2)
  ENVOY_GATEWAY_CHART_VERSION    Envoy Gateway OCI chart           (v1.9.1)
  METRICS_SERVER_CHART_VERSION   metrics-server chart              (3.14.0)
  ENVOY_GATEWAY_NAMESPACE        Envoy Gateway namespace           (envoy-gateway-system)
  ROLLOUT_TIMEOUT                kubectl rollout/apply timeout     (300s)

  The default kindnet CNI does NOT enforce NetworkPolicy. Run
  'CNI=calico make kind-up' for a NetworkPolicy-enforcing local cluster.

Options:
  --with-argocd   Run kubecommerce-gitops/bootstrap/argocd/install.sh when present
                  (Argo CD itself is Phase 8; otherwise a note is printed).
  -h, --help      Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-argocd) WITH_ARGOCD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

if ! [[ "$WORKERS" =~ ^[0-9]+$ ]]; then
  die "WORKERS must be a non-negative integer (got '$WORKERS')."
fi

case "$CNI" in
  kindnet|calico) ;;
  *) die "CNI must be 'kindnet' or 'calico' (got '$CNI')." ;;
esac

# --- temp config cleanup ------------------------------------------------------
TEMP_CONFIG=""
cleanup() { [ -n "${TEMP_CONFIG:-}" ] && rm -f "$TEMP_CONFIG" || true; }
trap cleanup EXIT

# --- tool preflight -----------------------------------------------------------
require_cmd() {
  local cmd="$1" hint="$2"
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found. $hint"
}

log "preflight: docker, kind, kubectl, helm"
require_cmd docker "Install Docker Engine or Docker Desktop: https://docs.docker.com/get-docker/"
require_cmd kind   "Install kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
require_cmd kubectl "Install kubectl: https://kubernetes.io/docs/tasks/tools/"
require_cmd helm   "Install Helm 3: https://helm.sh/docs/intro/install/"

if ! docker info >/dev/null 2>&1; then
  die "Docker daemon is not reachable ('docker info' failed). Start Docker and re-run 'make kind-up'."
fi

# --- render the kind config (worker count + cluster name overrides) ----------
render_kind_config() {
  local template="${SCRIPT_DIR}/kind-config.yaml"
  [ -f "$template" ] || die "kind config template not found: $template"
  local out
  out="$(mktemp "${TMPDIR:-/tmp}/kubecommerce-kind-XXXXXX.yaml")"
  awk -v workers="$WORKERS" -v name="$CLUSTER_NAME" -v cni="$CNI" -v cidr="$CALICO_POD_CIDR" '
    /^  # --- WORKER_NODES_BEGIN/ { skip=1; next }
    /^  # --- WORKER_NODES_END/   { skip=0; next }
    skip { next }
    /^name:[[:space:]]/ {
      print "name: " name
      if (cni == "calico") {
        print "networking:"
        print "  disableDefaultCNI: true"
        print "  podSubnet: " cidr
      }
      next
    }
    { print }
    END {
      for (i = 0; i < workers; i++) {
        print "  - role: worker"
        print "    labels:"
        print "      kubecommerce.io/node-role: worker"
      }
    }
  ' "$template" > "$out"
  printf '%s' "$out"
}

# --- NetworkPolicy / CNI notice (Oracle ruling) ------------------------------
# The default kind CNI (kindnet) does not enforce NetworkPolicy, so policies in
# this repo are inert locally until an enforcing CNI is installed.
if [ "$CNI" = "kindnet" ]; then
  cat >&2 <<'EOF'

===========================================================================
[NOTE] Default kind CNI (kindnet) does NOT enforce NetworkPolicy.
       Any NetworkPolicy applied to this cluster is INERT locally; it only
       takes effect on a policy-enforcing CNI (Calico/Cilium, EKS VPC CNI +
       network policy agent). For a local cluster that DOES enforce policies:
           CNI=calico make kind-up
===========================================================================

EOF
else
  log "CNI=calico: installing an enforcing CNI (default kindnet does not enforce NetworkPolicy)"
fi

# --- 1. cluster (idempotent; never deletes) ----------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "kind cluster '$CLUSTER_NAME' already exists - skipping creation"
else
  TEMP_CONFIG="$(render_kind_config)"
  log "creating kind cluster '$CLUSTER_NAME' ($WORKERS worker node(s), CNI=$CNI)"
  log "node image: $KIND_NODE_IMAGE"
  if [ "$CNI" = "calico" ]; then
    # disableDefaultCNI keeps nodes NotReady until Calico is up, so
    # 'kind create --wait' (which waits for node Ready) would time out.
    log "CNI=calico: creating without --wait (nodes stay NotReady until Calico is installed)"
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --image "$KIND_NODE_IMAGE" \
      --config "$TEMP_CONFIG"
  else
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --image "$KIND_NODE_IMAGE" \
      --config "$TEMP_CONFIG" \
      --wait 120s
  fi
fi

kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
log "kubeconfig context: $(kubectl config current-context)"

# --- 1b. Calico CNI (opt-in via CNI=calico) ----------------------------------
if [ "$CNI" = "calico" ]; then
  # Pinned tigera-operator manifest. `create` is guarded so a re-run (cluster
  # reused) does not fail with AlreadyExists.
  if kubectl get namespace tigera-operator >/dev/null 2>&1; then
    log "Calico operator already installed; skipping manifest create"
  else
    log "installing Calico ${CALICO_VERSION} operator from ${CALICO_OPERATOR_URL}"
    kubectl create -f "$CALICO_OPERATOR_URL"
  fi
  # The operator manifest alone does not install Calico: an Installation CR
  # triggers the calico-system DaemonSets/Deployments. Its pod CIDR must match
  # networking.podSubnet in the generated kind config (${CALICO_POD_CIDR}).
  log "applying Calico Installation (pod CIDR ${CALICO_POD_CIDR})"
  kubectl apply -f - <<EOF
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: ${CALICO_POD_CIDR}
        encapsulation: VXLANCrossSubnet
        natOutgoing: Enabled
        nodeSelector: all()
EOF

  log "waiting for calico-system deployments to become available (timeout ${ROLLOUT_TIMEOUT})"
  # Wait for the operator to create the deployments before waiting on them.
  for _ in $(seq 1 60); do
    if kubectl -n calico-system get deploy -o name 2>/dev/null | grep -q .; then break; fi
    sleep 5
  done
  kubectl -n calico-system wait --for=condition=Available --all deployment --timeout="$ROLLOUT_TIMEOUT" \
    || die "Calico deployments did not become available; check 'kubectl -n calico-system get pods'"
  log "waiting for nodes to become Ready (CNI now present)"
  kubectl wait --for=condition=Ready node --all --timeout="$ROLLOUT_TIMEOUT" \
    || die "nodes did not become Ready after installing Calico; check 'kubectl get nodes -o wide'"
  log "Calico CNI is available and nodes are Ready"
fi

# --- 2. Gateway API CRDs ------------------------------------------------------
log "installing Gateway API CRDs ${GATEWAY_API_VERSION}"
kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
log "waiting for Gateway API CRDs to be Established"
mapfile -t GW_CRDS < <(kubectl get crd -o name | grep 'gateway\.networking\.k8s\.io' || true)
if [ "${#GW_CRDS[@]}" -gt 0 ]; then
  kubectl wait --for=condition=Established --timeout=180s "${GW_CRDS[@]}"
else
  warn "no gateway.networking.k8s.io CRDs found after apply; check the Gateway API release URL"
fi

# --- 3. Envoy Gateway (Helm OCI chart) ---------------------------------------
# GatewayClass/Gateway are owned by kubecommerce-gitops (Phase D lane 3); the
# Envoy Gateway chart does not create them, and kind-up must not duplicate them.
log "installing Envoy Gateway chart ${ENVOY_GATEWAY_CHART_VERSION} into ${ENVOY_GATEWAY_NAMESPACE}"
helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
  --version "$ENVOY_GATEWAY_CHART_VERSION" \
  --namespace "$ENVOY_GATEWAY_NAMESPACE" \
  --create-namespace \
  --set crds.enabled=true

log "waiting for the Envoy Gateway controller deployment"
kubectl -n "$ENVOY_GATEWAY_NAMESPACE" rollout status deployment/envoy-gateway --timeout="$ROLLOUT_TIMEOUT"

# --- 4. metrics-server --------------------------------------------------------
log "installing metrics-server chart ${METRICS_SERVER_CHART_VERSION}"
helm repo add "$METRICS_SERVER_REPO_NAME" "$METRICS_SERVER_REPO_URL" --force-update >/dev/null
helm repo update "$METRICS_SERVER_REPO_NAME" >/dev/null
helm upgrade --install metrics-server "${METRICS_SERVER_REPO_NAME}/metrics-server" \
  --version "$METRICS_SERVER_CHART_VERSION" \
  --namespace kube-system \
  --set 'args={--kubelet-insecure-tls}'

log "waiting for the metrics-server deployment"
kubectl -n kube-system rollout status deployment/metrics-server --timeout="$ROLLOUT_TIMEOUT"

# --- 5. namespaces + Pod Security Admission ----------------------------------
ensure_namespace() {
  local ns="$1" env="${2:-}"
  local extra=""
  if [ -n "$env" ]; then
    extra="    environment: ${env}
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted"
  fi
  log "namespace ${ns}${env:+ (environment=${env})}"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${ns}
  labels:
    app.kubernetes.io/part-of: kubecommerce
${extra}
EOF
}

ensure_namespace kubecommerce-dev dev
ensure_namespace kubecommerce-staging staging
ensure_namespace kubecommerce-prod prod
ensure_namespace platform-system
ensure_namespace monitoring
ensure_namespace observability
ensure_namespace argocd

# --- 6. optional Argo CD bootstrap (Phase 8) ---------------------------------
if [ "$WITH_ARGOCD" -eq 1 ]; then
  if [ -f "$ARGOCD_INSTALL" ]; then
    log "running Argo CD bootstrap: $ARGOCD_INSTALL"
    bash "$ARGOCD_INSTALL"
  else
    log "Argo CD bootstrap not found at kubecommerce-gitops/bootstrap/argocd/install.sh"
    log "Argo CD is Phase 8 (GitOps); skipping --with-argocd."
  fi
fi

# --- 7. summary ---------------------------------------------------------------
cat <<EOF

================= kind cluster '${CLUSTER_NAME}' is ready =================
Context:          kind-${CLUSTER_NAME}
Node image:       ${KIND_NODE_IMAGE}
Worker nodes:     ${WORKERS}
CNI:              ${CNI}$([ "$CNI" = "kindnet" ] && printf ' (does NOT enforce NetworkPolicy; use CNI=calico)' || printf ' (enforces NetworkPolicy)')
Gateway API:      ${GATEWAY_API_VERSION} CRDs
Envoy Gateway:    chart ${ENVOY_GATEWAY_CHART_VERSION} (ns ${ENVOY_GATEWAY_NAMESPACE})
metrics-server:   chart ${METRICS_SERVER_CHART_VERSION} (ns kube-system)
Namespaces:       kubecommerce-dev, kubecommerce-staging, kubecommerce-prod,
                  platform-system, monitoring, observability, argocd
                   (kubecommerce-* enforce PSA baseline; warn/audit restricted)

Next steps:
  kubectl get nodes
  kubectl get ns
  kubectl -n kubecommerce-dev get pods
  make helm-install                                    # Phase 5 chart -> kubecommerce-dev
  kubectl -n kubecommerce-dev get gateway,httproute    # after the GitOps lane applies them
  make kind-down                                       # delete the cluster when done
===========================================================================
EOF
