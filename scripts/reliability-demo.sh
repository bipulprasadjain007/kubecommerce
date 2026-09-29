#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - reliability / self-healing demo evidence collector.
#
# Drives the guide Phase 11 demos against a local kind cluster and writes
# evidence under artifacts/reliability/<UTC timestamp>/:
#
#   11.1 rolling     rolling restart of one deployment with a zero-failure
#                    availability loop running throughout
#   11.4 hpa         drive load with k6 and capture the HPA replica
#                    trajectory, asserting a scale-up occurred
#   11.5 crash       delete a pod (self-healing) and `kill 1` (liveness
#                    restart), asserting the workload becomes Ready again
#   11.6 disruption  cordon + drain one worker node, record PDB behaviour
#                    and assert the service stays available, then uncordon
#   11.1 rollback    restart to create a new revision, then `rollout undo`
#                    and assert the previous revision becomes Ready
#
# Every mode restores the cluster state it changed (background load stopped,
# drained nodes uncordoned) through an EXIT trap. Namespaces and PVCs are
# NEVER deleted. Running against any `kubecommerce-prod*` namespace requires
# FORCE=1.
#
# Usage:
#   scripts/reliability-demo.sh <mode>
#   scripts/reliability-demo.sh --help
#
# Modes:
#   rolling | hpa | crash | disruption | rollback | all
#
# Environment:
#   NAMESPACE           target namespace            (default kubecommerce-dev)
#   RELEASE             Helm release name           (default kubecommerce)
#   BASE_URL            gateway base URL            (default http://localhost/)
#   TARGET_DEPLOY       workload for rolling/crash/rollback (default gateway-api)
#   HPA_NAME            HPA to watch                (default: first in namespace)
#   K6_VUS / K6_DURATION  k6 passthrough            (default 20 / 2m)
#   CATALOG_URL         direct catalog URL for k6   (passthrough, optional)
#   ARTIFACTS_DIR       evidence root               (default artifacts/
#                                                    reliability/<UTC ts>)
#   HPA_WATCH_SECONDS   seconds to watch the HPA    (default 180)
#   ROLLOUT_TIMEOUT     kubectl rollout timeout     (default 180s)
#   POD_READY_TIMEOUT   kubectl wait timeout        (default 120s)
#   DRAIN_TIMEOUT       kubectl drain timeout       (default 150s)
#   NODE_NAME           node to drain               (default: first worker)
#   FORCE               set FORCE=1 to allow any kubecommerce-prod* namespace
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# --- configuration -----------------------------------------------------------
NAMESPACE="${NAMESPACE:-kubecommerce-dev}"
RELEASE="${RELEASE:-kubecommerce}"
BASE_URL="${BASE_URL:-http://localhost/}"
BASE_URL="${BASE_URL%/}" # drop a trailing slash so "${BASE_URL}/path" is clean
TARGET_DEPLOY="${TARGET_DEPLOY:-gateway-api}"
HPA_NAME="${HPA_NAME:-}"
K6_VUS="${K6_VUS:-20}"
K6_DURATION="${K6_DURATION:-2m}"
CATALOG_URL="${CATALOG_URL:-}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-}"
HPA_WATCH_SECONDS="${HPA_WATCH_SECONDS:-180}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
POD_READY_TIMEOUT="${POD_READY_TIMEOUT:-120s}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-150s}"
NODE_NAME="${NODE_NAME:-}"
FORCE="${FORCE:-0}"

# --- state restored by the EXIT trap ----------------------------------------
BACKGROUND_PIDS=()
CORDONED_NODES=()
STEP_RESULTS=()
LAST_BG_PID=""

# --- logging -----------------------------------------------------------------
log() { printf '[reliability] %s\n' "$*"; }
warn() { printf '[reliability][WARN] %s\n' "$*" >&2; }
err() { printf '[reliability][ERROR] %s\n' "$*" >&2; }

# --- cleanup / background helpers -------------------------------------------
stop_bg() {
  local pid="${1:-}"
  [ -n "$pid" ] || return 0
  # Negative PID targets the process group when the child was started with setsid.
  kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local pid node
  for pid in "${BACKGROUND_PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    stop_bg "$pid"
  done
  if command -v kubectl >/dev/null 2>&1; then
    for node in "${CORDONED_NODES[@]:-}"; do
      [ -n "$node" ] || continue
      kubectl uncordon "$node" >/dev/null 2>&1 || true
    done
  fi
  # Backstop for a k6 child that escaped the process group.
  if command -v pkill >/dev/null 2>&1; then
    pkill -f 'tests/load/order-flow.js' 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_bg() {
  # Shell functions (the availability loop) cannot go through setsid, which
  # execs a binary. Run them in a background subshell and kill the PID directly;
  # external commands are started with setsid so their children (e.g. the k6
  # process spawned by load-test.sh) can be reaped as one process group.
  if declare -F "$1" >/dev/null 2>&1; then
    "$@" &
    LAST_BG_PID=$!
    BACKGROUND_PIDS+=("$LAST_BG_PID")
    return 0
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" &
  else
    "$@" &
  fi
  LAST_BG_PID=$!
  BACKGROUND_PIDS+=("$LAST_BG_PID")
}

# --- preflight ---------------------------------------------------------------
preflight() {
  if ! command -v kubectl >/dev/null 2>&1; then
    err "kubectl was not found in PATH."
    err "Install it (see docs/runbook.md section 3) and re-run."
    exit 1
  fi
  if ! kubectl cluster-info >/dev/null 2>&1; then
    err "cannot reach a Kubernetes cluster for the current kubeconfig context."
    err "Start the local cluster with 'make kind-up' and check KUBECONFIG."
    exit 1
  fi
  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    err "namespace '$NAMESPACE' does not exist in the cluster."
    err "Run 'make kind-up' (and the Helm/GitOps install) or set NAMESPACE."
    exit 1
  fi
  log "cluster reachable; namespace '$NAMESPACE' present"
}

# --- artifact setup ----------------------------------------------------------
setup_artifacts() {
  if [ -z "$ARTIFACTS_DIR" ]; then
    ARTIFACTS_DIR="artifacts/reliability/$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  case "$ARTIFACTS_DIR" in
    /*) ;;
    *) ARTIFACTS_DIR="$ROOT_DIR/$ARTIFACTS_DIR" ;;
  esac
  mkdir -p "$ARTIFACTS_DIR"
}

# --- availability loop -------------------------------------------------------
# Convert a kubectl-style duration (120s, 2m, 1h; bare number = seconds) to an
# integer number of seconds for the bash polling loops.
to_seconds() {
  local v="${1:-}"
  case "$v" in
    *s) v="${v%s}" ;;
    *m) v=$(( ${v%m} * 60 )) ;;
    *h) v=$(( ${v%h} * 3600 )) ;;
  esac
  [ -n "$v" ] || v=0
  printf '%s' "$v"
}

availability_loop() {
  local log_file="$1"
  local -a paths=("/health/live")
  local code ts path

  # Only include the catalogue probe when it is actually reachable, so an
  # environment without catalog-service does not manufacture fake failures.
  code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "${BASE_URL}/api/catalog/products" 2>/dev/null || true)"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    paths+=("/api/catalog/products")
  fi

  while :; do
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for path in "${paths[@]}"; do
      code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "${BASE_URL}${path}" 2>/dev/null || true)"
      [ -n "$code" ] || code=000
      printf '%s %s %s\n' "$ts" "$code" "$path" >> "$log_file"
    done
    sleep 0.2
  done
}

summarize_availability() {
  local log_file="$1" out="$2"
  local total ok c4xx s5xx other start_ts end_ts rate

  if [ ! -s "$log_file" ]; then
    printf 'requests=0\nresult=FAIL (no samples)\n' > "$out"
    return 1
  fi
  total="$(wc -l < "$log_file" | tr -d ' ')"
  ok="$(awk '$2 ~ /^2[0-9][0-9]$/ {n++} END {print n+0}' "$log_file")"
  c4xx="$(awk '$2 ~ /^4[0-9][0-9]$/ {n++} END {print n+0}' "$log_file")"
  # A failure is a server error (5xx) or a connection error (curl prints 000).
  # 4xx (a 404 on a probing route, for example) is informative, not a failure.
  s5xx="$(awk '$2 ~ /^5[0-9][0-9]$/ || $2 == "000" {n++} END {print n+0}' "$log_file")"
  other=$(( total - ok - c4xx - s5xx ))
  start_ts="$(head -n1 "$log_file" | awk '{print $1}')"
  end_ts="$(tail -n1 "$log_file" | awk '{print $1}')"
  rate="$(awk -v f="$s5xx" -v t="$total" 'BEGIN { if (t > 0) printf "%.4f", (f/t)*100; else print "0.0000" }')"

  {
    printf 'window_start=%s\n' "$start_ts"
    printf 'window_end=%s\n' "$end_ts"
    printf 'url=%s\n' "$BASE_URL"
    printf 'requests=%s\n' "$total"
    printf 'success_2xx=%s\n' "$ok"
    printf 'client_4xx=%s\n' "$c4xx"
    printf 'server_errors_5xx_or_connection=%s\n' "$s5xx"
    printf 'other=%s\n' "$other"
    printf 'failure_rate_pct=%s\n' "$rate"
    if [ "$s5xx" -eq 0 ]; then
      printf 'result=PASS\n'
    else
      printf 'result=FAIL\n'
    fi
  } > "$out"

  [ "$s5xx" -eq 0 ]
}

# --- pod helpers -------------------------------------------------------------
selector_for_deploy() {
  local d="$1"
  kubectl get deploy "$d" -n "$NAMESPACE" -o json 2>/dev/null | python3 -c '
import json, sys
ml = json.load(sys.stdin)["spec"]["selector"]["matchLabels"]
print(",".join(f"{k}={v}" for k, v in sorted(ml.items())))
'
}

ready_pod_records() {
  # One "name uid" line per Running + Ready pod for the selector.
  local selector="$1"
  kubectl get pods -n "$NAMESPACE" -l "$selector" -o json 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
for pod in data.get("items", []):
    status = pod.get("status", {})
    if status.get("phase") != "Running":
        continue
    if any(c.get("ready") for c in (status.get("containerStatuses") or [])):
        print(pod["metadata"]["name"], pod["metadata"].get("uid", ""))
'
}

ready_pods() {
  ready_pod_records "$1" | awk 'NF {printf "%s ", $1}'
}

first_ready_pod() {
  local selector="$1"
  ready_pods "$selector" | awk 'NF {print $1; exit}'
}

select_node() {
  local node="${NODE_NAME}"
  if [ -n "$node" ]; then
    printf '%s' "$node"
    return 0
  fi
  node="$(kubectl get nodes -l kubecommerce.io/node-role=worker -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "$node" ]; then
    node="$(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  fi
  [ -n "$node" ] && printf '%s' "$node"
}

# =============================================================================
# Modes
# =============================================================================

# 11.1 - rolling update: restart a deployment while traffic must stay clean.
run_rolling() {
  local dir="$ARTIFACTS_DIR/rolling"
  mkdir -p "$dir"
  local d="$TARGET_DEPLOY"
  if ! kubectl get deploy "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
    err "deployment '$d' not found in namespace '$NAMESPACE'"
    return 1
  fi

  kubectl get deploy "$d" -n "$NAMESPACE" -o yaml > "$dir/deployment-before.yaml" 2>/dev/null || true
  log "rolling: starting availability loop against ${BASE_URL}/health/live"
  start_bg availability_loop "$dir/availability.log"
  local avail_pid="$LAST_BG_PID"

  kubectl rollout restart "deploy/$d" -n "$NAMESPACE" > "$dir/rollout-restart.txt" 2>&1 || true
  local roll_ok=1
  if ! kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" > "$dir/rollout-status.txt" 2>&1; then
    roll_ok=0
  fi
  stop_bg "$avail_pid"

  local sum_ok=0
  if summarize_availability "$dir/availability.log" "$dir/availability-summary.txt"; then
    sum_ok=1
  fi
  kubectl get deploy "$d" -n "$NAMESPACE" -o wide > "$dir/deployment-after.txt" 2>&1 || true

  if [ "$roll_ok" -eq 1 ] && [ "$sum_ok" -eq 1 ]; then
    log "rolling: PASS"
    return 0
  fi
  err "rolling: FAIL (rollout_status_ok=$roll_ok availability_ok=$sum_ok)"
  return 1
}

# 11.4 - HPA demo: load must move the HPA above its starting replica count.
run_hpa() {
  local dir="$ARTIFACTS_DIR/hpa"
  mkdir -p "$dir"

  if ! command -v k6 >/dev/null 2>&1; then
    err "k6 is required for the hpa demo (scripts/load-test.sh)."
    err "Install k6 - see scripts/load-test.sh for the pinned download command."
    return 1
  fi
  if [ ! -f "$ROOT_DIR/scripts/load-test.sh" ]; then
    err "scripts/load-test.sh not found"
    return 1
  fi

  local hpa="$HPA_NAME"
  if [ -z "$hpa" ]; then
    hpa="$(kubectl get hpa -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  fi
  if [ -z "$hpa" ]; then
    err "no HorizontalPodAutoscaler found in namespace '$NAMESPACE'."
    err "dev has HPA disabled; run against staging/prod values or a custom overlay."
    return 1
  fi

  local target initial
  target="$(kubectl get hpa "$hpa" -n "$NAMESPACE" -o jsonpath='{.spec.scaleTargetRef.name}' 2>/dev/null || true)"
  [ -n "$target" ] || target="$TARGET_DEPLOY"
  initial="$(kubectl get hpa "$hpa" -n "$NAMESPACE" -o jsonpath='{.status.currentReplicas}' 2>/dev/null || true)"
  [ -n "$initial" ] || initial=0
  log "hpa: watching '$hpa' (target '$target', initial replicas=$initial) for ${HPA_WATCH_SECONDS}s"

  : > "$dir/replica-trajectory.txt"

  local -a load_env=(env BASE_URL="$BASE_URL" VUS="$K6_VUS" DURATION="$K6_DURATION")
  [ -n "$CATALOG_URL" ] && load_env+=(CATALOG_URL="$CATALOG_URL")
  start_bg "${load_env[@]}" bash "$ROOT_DIR/scripts/load-test.sh" > "$dir/load.log" 2>&1
  local load_pid="$LAST_BG_PID"
  start_bg kubectl get hpa -n "$NAMESPACE" -w > "$dir/hpa-watch.log" 2>&1
  local watch_pid="$LAST_BG_PID"

  local end=$(( SECONDS + HPA_WATCH_SECONDS ))
  while [ "$SECONDS" -lt "$end" ]; do
    local ts desired current minr maxr
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    read -r desired current minr maxr < <(
      kubectl get hpa "$hpa" -n "$NAMESPACE" \
        -o jsonpath='{.status.desiredReplicas} {.status.currentReplicas} {.spec.minReplicas} {.spec.maxReplicas}' 2>/dev/null || true
    ) || true
    printf '%s desired=%s current=%s min=%s max=%s\n' \
      "$ts" "${desired:-?}" "${current:-?}" "${minr:-?}" "${maxr:-?}" >> "$dir/replica-trajectory.txt"
    sleep 5
  done
  stop_bg "$watch_pid"
  stop_bg "$load_pid"

  local peak
  peak="$(awk -F'current=' '{ split($2, a, " "); if (a[1]+0 > m) m = a[1]+0 } END { print m+0 }' "$dir/replica-trajectory.txt")"

  log "hpa: observed replica trajectory:"
  while IFS= read -r line; do
    printf '  %s\n' "$line"
  done < "$dir/replica-trajectory.txt"

  local scale_ok=0
  if [ "${peak:-0}" -gt "${initial:-0}" ]; then
    scale_ok=1
  fi
  {
    printf 'hpa=%s\n' "$hpa"
    printf 'target=%s\n' "$target"
    printf 'initial_replicas=%s\n' "$initial"
    printf 'peak_replicas=%s\n' "$peak"
    if [ "$scale_ok" -eq 1 ]; then printf 'result=PASS scale-up observed\n'; else printf 'result=FAIL no scale-up observed\n'; fi
  } > "$dir/hpa-summary.txt"

  if [ "$scale_ok" -eq 1 ]; then
    log "hpa: PASS (replicas ${initial} -> ${peak})"
    return 0
  fi
  err "hpa: FAIL (no scale-up: initial=${initial}, peak=${peak})"
  return 1
}

# 11.5 - crash recovery: pod deletion (self-healing) and liveness restart.
run_crash() {
  local dir="$ARTIFACTS_DIR/crash"
  mkdir -p "$dir"
  local d="$TARGET_DEPLOY"
  if ! kubectl get deploy "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
    err "deployment '$d' not found in namespace '$NAMESPACE'"
    return 1
  fi
  local selector
  selector="$(selector_for_deploy "$d" 2>/dev/null || true)"
  if [ -z "$selector" ]; then
    err "could not read the pod selector for deployment '$d'"
    return 1
  fi

  # --- self-healing: delete a pod, the controller recreates it --------------
  # Capture name AND UID so we can prove the exact object disappeared and a
  # genuinely new pod (different name and UID) took its place.
  local pod="" pod_uid=""
  pod="$(first_ready_pod "$selector")"
  if [ -z "$pod" ]; then
    err "no Ready pod found for deployment '$d'"
    return 1
  fi
  pod_uid="$(kubectl get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
  if [ -z "$pod_uid" ]; then
    err "could not read the UID of pod '$pod'"
    return 1
  fi
  printf 'name=%s\nuid=%s\n' "$pod" "$pod_uid" > "$dir/deleted-pod.txt"

  log "crash: deleting pod '$pod' (uid $pod_uid); expecting a NEW pod to become Ready"
  kubectl get pods -n "$NAMESPACE" -l "$selector" -o wide > "$dir/pods-before-delete.txt" 2>&1 || true
  if ! kubectl delete pod "$pod" -n "$NAMESPACE" > "$dir/delete-pod.txt" 2>&1; then
    err "failed to delete pod '$pod'"
    return 1
  fi

  local delete_ok=1
  kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" > "$dir/rollout-after-delete.txt" 2>&1 || delete_ok=0
  kubectl wait --for=condition=available "deploy/$d" -n "$NAMESPACE" --timeout="${POD_READY_TIMEOUT}" > "$dir/deployment-available.txt" 2>&1 || delete_ok=0
  kubectl get pods -n "$NAMESPACE" -l "$selector" -o wide > "$dir/pods-after-delete.txt" 2>&1 || true

  # Assert: the deleted UID is gone AND a *different* Ready pod (different name
  # and UID) exists for the same deployment, within the timeout.
  local uid_lines="" deadline=0 old_gone=0 replacement_ok=0 replacement_name="" replacement_uid=""
  deadline=$(( SECONDS + $(to_seconds "$POD_READY_TIMEOUT") ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    uid_lines="$(kubectl get pods -n "$NAMESPACE" -l "$selector" -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' 2>/dev/null || true)"
    if [ -n "$uid_lines" ] && printf '%s\n' "$uid_lines" | grep -qxF "$pod_uid"; then
      old_gone=0
    else
      old_gone=1
    fi
    replacement_name=""
    replacement_uid=""
    while read -r n u; do
      [ -n "$n" ] || continue
      if [ "$n" != "$pod" ] && [ "$u" != "$pod_uid" ] && [ -n "$u" ]; then
        replacement_name="$n"
        replacement_uid="$u"
        break
      fi
    done < <(ready_pod_records "$selector")
    if [ "$old_gone" -eq 1 ] && [ -n "$replacement_uid" ]; then
      replacement_ok=1
      break
    fi
    sleep 3
  done
  {
    printf 'old_uid_gone=%s\n' "$old_gone"
    printf 'replacement_name=%s\n' "$replacement_name"
    printf 'replacement_uid=%s\n' "$replacement_uid"
  } > "$dir/self-healing.txt"

  # --- liveness: kill PID 1 of the replacement pod; kubelet must restart the
  #     SAME pod object (UID unchanged) and its restartCount must increment ---
  local pod2="" pod2_uid="" before="" after="" cur_uid="" liveness_ok=0 ldeadline=0
  pod2="$replacement_name"
  if [ -z "$pod2" ]; then
    err "no Ready replacement pod available for the liveness restart"
  else
    pod2_uid="$(kubectl get pod "$pod2" -n "$NAMESPACE" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    if [ -z "$pod2_uid" ]; then
      err "could not read the UID of liveness pod '$pod2'"
    else
      log "crash: sending SIGTERM to PID 1 of pod '$pod2' (uid $pod2_uid)"
      before="$(kubectl get pod "$pod2" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)"
      : > "$dir/kill1-exec.txt"
      kubectl exec "$pod2" -n "$NAMESPACE" -- kill 1 >> "$dir/kill1-exec.txt" 2>&1 || true
      ldeadline=$(( SECONDS + 90 ))
      while [ "$SECONDS" -lt "$ldeadline" ]; do
        cur_uid="$(kubectl get pod "$pod2" -n "$NAMESPACE" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
        after="$(kubectl get pod "$pod2" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || true)"
        # Same pod object (UID unchanged) with a higher restartCount = a real
        # liveness restart. If the UID changed the pod was recreated instead,
        # which is not proof of liveness recovery.
        if [ "$cur_uid" = "$pod2_uid" ] && [ -n "$after" ] && [ "$after" -gt "${before:-0}" ] 2>/dev/null; then
          liveness_ok=1
          break
        fi
        if [ -n "$cur_uid" ] && [ "$cur_uid" != "$pod2_uid" ]; then
          break
        fi
        sleep 3
      done
      kubectl wait --for=condition=Ready "pod/$pod2" -n "$NAMESPACE" --timeout="${POD_READY_TIMEOUT}" >> "$dir/kill1-exec.txt" 2>&1 || true
      kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" >> "$dir/rollout-after-kill.txt" 2>&1 || true
    fi
  fi
  kubectl get pods -n "$NAMESPACE" -l "$selector" -o wide > "$dir/pods-final.txt" 2>&1 || true

  {
    printf 'deployment=%s\n' "$d"
    printf 'deleted_pod=%s\n' "$pod"
    printf 'deleted_pod_uid=%s\n' "$pod_uid"
    printf 'old_uid_gone=%s\n' "$old_gone"
    printf 'replacement_name=%s\n' "$replacement_name"
    printf 'replacement_uid=%s\n' "$replacement_uid"
    printf 'self_healing_ok=%s\n' "$replacement_ok"
    printf 'liveness_pod=%s\n' "$pod2"
    printf 'liveness_pod_uid=%s\n' "$pod2_uid"
    printf 'liveness_restart_observed=%s\n' "$liveness_ok"
  } > "$dir/crash-summary.txt"

  if [ "$delete_ok" -eq 1 ] && [ "$replacement_ok" -eq 1 ] && [ "$liveness_ok" -eq 1 ]; then
    log "crash: PASS"
    return 0
  fi
  err "crash: FAIL (delete_ok=$delete_ok self_healing_ok=$replacement_ok liveness_ok=$liveness_ok)"
  return 1
}

# 11.6 - node/pod disruption: drain a worker, assert availability, uncordon.
run_disruption() {
  local dir="$ARTIFACTS_DIR/disruption"
  mkdir -p "$dir"
  local d="$TARGET_DEPLOY"
  local node
  node="$(select_node)"
  if [ -z "$node" ]; then
    err "no worker node found to drain (set NODE_NAME to override)"
    return 1
  fi
  log "disruption: cordon + drain node '$node'"

  kubectl get node "$node" -o wide > "$dir/node-before.txt" 2>&1 || true
  kubectl get pdb -n "$NAMESPACE" -o wide > "$dir/pdb-before.txt" 2>&1 || true

  start_bg availability_loop "$dir/availability.log"
  local avail_pid="$LAST_BG_PID"

  if ! kubectl cordon "$node" > "$dir/cordon.txt" 2>&1; then
    err "failed to cordon node '$node'"
    stop_bg "$avail_pid"
    summarize_availability "$dir/availability.log" "$dir/availability-summary.txt" || true
    return 1
  fi
  CORDONED_NODES+=("$node")

  local drain_ok=1
  if ! kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="${DRAIN_TIMEOUT}" > "$dir/drain.txt" 2>&1; then
    drain_ok=0
  fi
  kubectl get pdb -n "$NAMESPACE" -o wide > "$dir/pdb-during.txt" 2>&1 || true

  kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" > "$dir/rollout-status.txt" 2>&1 || true
  kubectl wait --for=condition=available "deploy/$d" -n "$NAMESPACE" --timeout="${POD_READY_TIMEOUT}" > "$dir/deployment-available.txt" 2>&1 || true

  stop_bg "$avail_pid"
  local avail_ok=0
  if summarize_availability "$dir/availability.log" "$dir/availability-summary.txt"; then
    avail_ok=1
  fi

  # Always restore the node, even when the drain failed.
  kubectl uncordon "$node" > "$dir/uncordon.txt" 2>&1 || true
  CORDONED_NODES=("${CORDONED_NODES[@]/$node/}")
  kubectl get node "$node" -o wide > "$dir/node-after.txt" 2>&1 || true

  {
    printf 'node=%s\n' "$node"
    printf 'drain_ok=%s\n' "$drain_ok"
    printf 'availability_ok=%s\n' "$avail_ok"
  } > "$dir/disruption-summary.txt"

  if [ "$drain_ok" -eq 1 ] && [ "$avail_ok" -eq 1 ]; then
    log "disruption: PASS"
    return 0
  fi
  err "disruption: FAIL (drain_ok=$drain_ok availability_ok=$avail_ok)"
  return 1
}

# 11.1 - rollback: create a revision, then undo it and verify readiness.
run_rollback() {
  local dir="$ARTIFACTS_DIR/rollback"
  mkdir -p "$dir"
  local d="$TARGET_DEPLOY"
  if ! kubectl get deploy "$d" -n "$NAMESPACE" >/dev/null 2>&1; then
    err "deployment '$d' not found in namespace '$NAMESPACE'"
    return 1
  fi

  kubectl rollout history "deploy/$d" -n "$NAMESPACE" > "$dir/rollout-history-before.txt" 2>&1 || true

  start_bg availability_loop "$dir/availability.log"
  local avail_pid="$LAST_BG_PID"

  # Create a new revision so there is a previous generation to return to.
  kubectl rollout restart "deploy/$d" -n "$NAMESPACE" > "$dir/rollout-restart.txt" 2>&1 || true
  local restart_ok=1
  kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" > "$dir/rollout-status-new.txt" 2>&1 || restart_ok=0
  kubectl get rs -n "$NAMESPACE" -o wide > "$dir/replicasets-before-undo.txt" 2>&1 || true

  # Undo back to the previous revision.
  local undo_ok=1
  kubectl rollout undo "deploy/$d" -n "$NAMESPACE" > "$dir/rollout-undo.txt" 2>&1 || undo_ok=0
  kubectl rollout status "deploy/$d" -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT}" >> "$dir/rollout-undo.txt" 2>&1 || undo_ok=0
  kubectl wait --for=condition=available "deploy/$d" -n "$NAMESPACE" --timeout="${POD_READY_TIMEOUT}" >> "$dir/rollout-undo.txt" 2>&1 || undo_ok=0

  stop_bg "$avail_pid"
  local avail_ok=0
  if summarize_availability "$dir/availability.log" "$dir/availability-summary.txt"; then
    avail_ok=1
  fi

  kubectl rollout history "deploy/$d" -n "$NAMESPACE" > "$dir/rollout-history-after.txt" 2>&1 || true
  kubectl get deploy "$d" -n "$NAMESPACE" -o wide > "$dir/deployment-after.txt" 2>&1 || true
  kubectl get rs -n "$NAMESPACE" -o wide > "$dir/replicasets-after-undo.txt" 2>&1 || true

  {
    printf 'deployment=%s\n' "$d"
    printf 'new_revision_ready=%s\n' "$restart_ok"
    printf 'undo_ready=%s\n' "$undo_ok"
    printf 'availability_ok=%s\n' "$avail_ok"
  } > "$dir/rollback-summary.txt"

  if [ "$restart_ok" -eq 1 ] && [ "$undo_ok" -eq 1 ] && [ "$avail_ok" -eq 1 ]; then
    log "rollback: PASS"
    return 0
  fi
  err "rollback: FAIL (restart_ok=$restart_ok undo_ok=$undo_ok availability_ok=$avail_ok)"
  return 1
}

# --- all-mode summary --------------------------------------------------------
evidence_for() {
  case "$1" in
    rolling) printf '`rolling/rollout-status.txt`, `rolling/availability-summary.txt`' ;;
    hpa) printf '`hpa/replica-trajectory.txt`, `hpa/hpa-watch.log`, `hpa/load.log`' ;;
    crash) printf '`crash/delete-pod.txt`, `crash/pods-after-delete.txt`, `crash/kill1-exec.txt`' ;;
    disruption) printf '`disruption/drain.txt`, `disruption/pdb-during.txt`, `disruption/availability-summary.txt`, `disruption/uncordon.txt`' ;;
    rollback) printf '`rollback/rollout-undo.txt`, `rollback/rollout-history-after.txt`, `rollback/availability-summary.txt`' ;;
    *) printf '' ;;
  esac
}

write_summary() {
  local out="$ARTIFACTS_DIR/00-summary.md" row step result note evidence revision
  revision="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  {
    printf '# Reliability demo summary\n\n'
    printf -- '- namespace: `%s`\n' "$NAMESPACE"
    printf -- '- release: `%s`\n' "$RELEASE"
    printf -- '- base_url: `%s`\n' "$BASE_URL"
    printf -- '- generated (UTC): %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf -- '- git revision: `%s`\n\n' "$revision"
    printf '| Step | Result | Evidence |\n'
    printf '|---|---|---|\n'
    for row in "${STEP_RESULTS[@]:-}"; do
      [ -n "$row" ] || continue
      IFS='|' read -r step result note <<<"$row"
      evidence="$(evidence_for "$step")"
      printf '| %s | %s | %s |\n' "$step" "$result" "$evidence"
    done
    printf '\n> Runtime execution requires the local kind cluster (`make kind-up`).\n'
    printf '> Full evidence: `%s/`\n' "$ARTIFACTS_DIR"
  } > "$out"
  log "wrote $out"
}

run_all() {
  local step overall=0
  for step in rolling hpa crash disruption rollback; do
    log "=== reliability demo: $step ==="
    if "run_$step"; then
      STEP_RESULTS+=("$step|PASS|")
    else
      STEP_RESULTS+=("$step|FAIL|failed")
      overall=1
    fi
  done
  write_summary
  return "$overall"
}

# --- usage / entrypoint ------------------------------------------------------
usage() {
  cat <<'EOF'
KubeCommerce reliability / self-healing demo evidence collector.

Usage:
  scripts/reliability-demo.sh <mode>
  scripts/reliability-demo.sh --help

Modes:
  rolling     11.1  rolling restart + zero-failure availability loop
  hpa         11.4  k6 load + HPA replica trajectory (asserts scale-up)
  crash       11.5  pod delete (self-healing) + `kill 1` (liveness restart)
  disruption  11.6  cordon + drain a worker node, then uncordon
  rollback    11.1  restart to a new revision, then `rollout undo`
  all               run every mode in order, writing 00-summary.md

Key environment variables (full list in the script header):
  NAMESPACE=kubecommerce-dev  BASE_URL=http://localhost/
  TARGET_DEPLOY=gateway-api   K6_VUS=20  K6_DURATION=2m
  ARTIFACTS_DIR=artifacts/reliability/<UTC timestamp>
  FORCE=1                     required for any kubecommerce-prod* namespace

Requires: kubectl + a reachable kind cluster. k6 is required for `hpa`.
Evidence is written under ARTIFACTS_DIR; namespaces/PVCs are never deleted.
EOF
}

main() {
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
  fi

  local mode="${1:-}"
  case "$mode" in
    rolling|hpa|crash|disruption|rollback|all) ;;
    "") usage; exit 1 ;;
    *) err "unknown mode: '$mode'"; usage; exit 2 ;;
  esac

  case "$NAMESPACE" in
    kubecommerce-prod*)
      if [ "$FORCE" != "1" ]; then
        err "refusing to operate on '$NAMESPACE' without FORCE=1 (matches kubecommerce-prod*)"
        exit 3
      fi
      ;;
  esac

  preflight
  setup_artifacts

  local rc=0
  if [ "$mode" = "all" ]; then
    if ! run_all; then rc=1; fi
  else
    if ! "run_$mode"; then rc=1; fi
  fi

  printf '\n'
  log "artifacts: $ARTIFACTS_DIR"
  exit "$rc"
}

main "$@"
