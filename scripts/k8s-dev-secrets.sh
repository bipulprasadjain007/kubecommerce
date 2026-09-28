#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - create local Kubernetes secrets for a kind environment.
#
# Creates (idempotently):
#   * platform-system/kubecommerce-infra-secrets
#       postgres-password / redis-password / rabbitmq-password
#   * kubecommerce-<env>/<service>-secrets for all five services
#       environment variables consumed via `envFrom.secretRef` by the Helm chart
#   * observability/postgres-exporter-secret  (key DATA_SOURCE_NAME)
#   * observability/redis-exporter-secret      (key redis-password)
#       credentials for the Phase E Prometheus exporters, which run in the
#       `observability` namespace (not kubecommerce-<env> or platform-system)
#
# Inputs:
#   * `.secrets/jwt-private.pem`          (from scripts/dev-bootstrap.sh)
#   * `.env`                              (optional; tokens/user defaults)
#   * `.secrets/k8s-<env>-passwords.env`  (generated once, git-ignored)
#
# The script is idempotent and never silently rotates credentials: generated
# passwords are persisted on first run. To rotate, delete that file (or use the
# Phase 6 rotation runbook) and re-run - running pods pick the new values up on
# their next restart.
#
# Usage: scripts/k8s-dev-secrets.sh [env]     # env defaults to "dev"
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_NAME="${1:-dev}"
NAMESPACE="kubecommerce-${ENV_NAME}"
PLATFORM_NS="platform-system"
OBSERVABILITY_NS="observability"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl not found. Install kubectl and point it at the kind cluster." >&2
  exit 1
fi
if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: no reachable Kubernetes cluster. Run 'make kind-up' first." >&2
  exit 1
fi

if [ ! -f .secrets/jwt-private.pem ]; then
  echo "ERROR: .secrets/jwt-private.pem missing. Run 'bash scripts/dev-bootstrap.sh'." >&2
  exit 1
fi

env_or_default() { # VAR_NAME default
  local value="${!1:-}"
  if [ -z "$value" ] && [ -f .env ]; then
    value="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${value:-$2}"
}

mkdir -p .secrets
chmod 700 .secrets
PASSWORDS_FILE=".secrets/k8s-${ENV_NAME}-passwords.env"
if [ ! -f "$PASSWORDS_FILE" ]; then
  umask 077
  {
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
    echo "RABBITMQ_PASSWORD=$(openssl rand -hex 24)"
  } > "$PASSWORDS_FILE"
  chmod 600 "$PASSWORDS_FILE"
  echo "==> generated credentials: ${PASSWORDS_FILE} (git-ignored)"
fi
# shellcheck disable=SC1090
source "$PASSWORDS_FILE"

# The Postgres and RabbitMQ StatefulSets in
# kubecommerce-gitops/platform/infrastructure/** hardcode the user 'kubecommerce',
# so the DB/RabbitMQ URLs below always use that literal user. POSTGRES_USER is
# intentionally NOT consulted here; changing the user requires updating
# kubecommerce-gitops/platform/infrastructure/** (and this literal) together.
INFRA_USER="kubecommerce"
# Each service gets its own internal token variable so they can diverge:
CATALOG_INTERNAL_TOKEN="$(env_or_default CATALOG_INTERNAL_API_TOKEN dev-internal-token-change-me)"
ORDERS_INTERNAL_TOKEN="$(env_or_default ORDERS_INTERNAL_API_TOKEN dev-internal-token-change-me)"
GATEWAY_INTERNAL_TOKEN="$(env_or_default GATEWAY_INTERNAL_API_TOKEN dev-internal-token-change-me)"
POSTGRES_HOST="postgres.${PLATFORM_NS}.svc.cluster.local"
REDIS_HOST="redis.${PLATFORM_NS}.svc.cluster.local"
RABBITMQ_HOST="rabbitmq.${PLATFORM_NS}.svc.cluster.local"

for ns in "$PLATFORM_NS" "$NAMESPACE" "$OBSERVABILITY_NS"; do
  if ! kubectl get namespace "$ns" >/dev/null 2>&1; then
    echo "ERROR: namespace ${ns} not found. Run 'make kind-up' first." >&2
    exit 1
  fi
done

apply_secret() { # namespace name --from-... args
  local ns="$1" name="$2"
  shift 2
  kubectl create secret generic "$name" -n "$ns" "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "  applied ${ns}/${name}"
}

echo "==> infra secrets (${PLATFORM_NS})"
apply_secret "$PLATFORM_NS" kubecommerce-infra-secrets \
  --from-literal=postgres-password="$POSTGRES_PASSWORD" \
  --from-literal=redis-password="$REDIS_PASSWORD" \
  --from-literal=rabbitmq-password="$RABBITMQ_PASSWORD"

echo "==> application secrets (${NAMESPACE})"
AUTH_DB="postgresql+asyncpg://${INFRA_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/auth_db"
CATALOG_DB="postgresql+asyncpg://${INFRA_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/catalog_db"
ORDERS_DB="postgresql+asyncpg://${INFRA_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/orders_db"
RABBITMQ_URL="amqp://${INFRA_USER}:${RABBITMQ_PASSWORD}@${RABBITMQ_HOST}:5672/"

apply_secret "$NAMESPACE" auth-service-secrets \
  --from-literal=AUTH_DATABASE_URL="$AUTH_DB" \
  --from-file=AUTH_JWT_PRIVATE_KEY=.secrets/jwt-private.pem

apply_secret "$NAMESPACE" catalog-service-secrets \
  --from-literal=CATALOG_DATABASE_URL="$CATALOG_DB" \
  --from-literal=CATALOG_REDIS_URL="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:6379/0" \
  --from-literal=CATALOG_INTERNAL_API_TOKEN="$CATALOG_INTERNAL_TOKEN"

apply_secret "$NAMESPACE" order-service-secrets \
  --from-literal=ORDERS_DATABASE_URL="$ORDERS_DB" \
  --from-literal=ORDERS_RABBITMQ_URL="$RABBITMQ_URL" \
  --from-literal=ORDERS_INTERNAL_API_TOKEN="$ORDERS_INTERNAL_TOKEN"

apply_secret "$NAMESPACE" notification-worker-secrets \
  --from-literal=WORKER_RABBITMQ_URL="$RABBITMQ_URL" \
  --from-literal=WORKER_REDIS_URL="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:6379/1"

apply_secret "$NAMESPACE" gateway-api-secrets \
  --from-literal=GATEWAY_REDIS_URL="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:6379/2" \
  --from-literal=GATEWAY_INTERNAL_API_TOKEN="$GATEWAY_INTERNAL_TOKEN"

echo "==> exporter secrets (${OBSERVABILITY_NS})"
# Phase E Prometheus exporters (platform/observability-apps/*-exporter.yaml).
# postgres-exporter: the chart's `config.datasourceSecret` mounts the whole
# connection string from the DATA_SOURCE_NAME key.
apply_secret "$OBSERVABILITY_NS" postgres-exporter-secret \
  --from-literal=DATA_SOURCE_NAME="postgresql://${INFRA_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:5432/postgres?sslmode=disable"
# redis-exporter: the chart's native `auth.secret` reads the bare password from
# the redis-password key (the address stays in values, not the Secret).
apply_secret "$OBSERVABILITY_NS" redis-exporter-secret \
  --from-literal=redis-password="$REDIS_PASSWORD"

echo
echo "Done. Restart workloads to pick up changes:"
echo "  kubectl -n ${NAMESPACE} rollout restart deploy"
echo "Verify with: kubectl -n ${NAMESPACE} get secrets; kubectl -n ${PLATFORM_NS} get secrets; kubectl -n ${OBSERVABILITY_NS} get secrets"
