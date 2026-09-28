#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - end-to-end smoke test.
#
#   gateway readiness -> register -> login -> create product (catalog, internal)
#   -> list products (gateway) -> create order (gateway) -> fetch order
#
# Requirements: curl + python3 (used for JSON parsing, so jq is NOT required).
#
# Environment:
#   BASE_URL            gateway base URL        (default http://localhost:8080)
#   CATALOG_URL         catalog direct URL      (default http://localhost:8002)
#   INTERNAL_API_TOKEN  internal mutation token (default from .env or dev value)
#
# Never prints tokens or passwords. Exits non-zero if any step fails.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is required but was not found." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required (used for JSON parsing) but was not found." >&2
  exit 1
fi

BASE_URL="${BASE_URL:-http://localhost:8080}"
CATALOG_URL="${CATALOG_URL:-http://localhost:8002}"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN:-dev-internal-token-change-me}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
BODY_FILE="$TMP_DIR/body"

HTTP_CODE=""
HTTP_BODY=""
FAILED=0
RESULTS=()

record() { # name result detail
  RESULTS+=("$1|$2|$3")
  [ "$2" = "PASS" ] || FAILED=1
}

is_2xx() { case "$1" in 2??) return 0 ;; *) return 1 ;; esac; }

# http_call METHOD URL [DATA] [BEARER] [EXTRA_HEADER]
http_call() {
  local method="$1" url="$2" data="${3:-}" bearer="${4:-}" extra="${5:-}"
  local -a args=(-sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" "$url")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  [ -n "$bearer" ] && args+=(-H "Authorization: Bearer ${bearer}")
  [ -n "$extra" ] && args+=(-H "$extra")
  # curl prints the -w http_code (000 on connection failure) to stdout even when
  # it exits non-zero, so swallow the exit status rather than appending another 000.
  HTTP_CODE="$(curl "${args[@]}" 2>/dev/null || true)"
  HTTP_BODY="$(cat "$BODY_FILE" 2>/dev/null || true)"
}

# json_field JSON DOT_PATH  (prints scalar, or compact JSON for objects/arrays)
json_field() {
  printf '%s' "$1" | python3 -c '
import sys, json
path = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(3)
for part in path.split("."):
    if part == "":
        continue
    if isinstance(data, dict):
        data = data.get(part, "")
    else:
        data = ""
        break
print(json.dumps(data) if isinstance(data, (dict, list)) else data)
' "$2" 2>/dev/null || true
}

UNIQ="$(date +%s)-$$"
EMAIL="smoke-${UNIQ}@example.com"
PASSWORD="Sm0ke-Test-Password!42"

echo "[smoke-test] BASE_URL=$BASE_URL CATALOG_URL=$CATALOG_URL"

# --- 1. gateway readiness ----------------------------------------------------
http_call GET "$BASE_URL/health/ready"
if is_2xx "$HTTP_CODE"; then
  record "gateway /health/ready" PASS "HTTP $HTTP_CODE"
else
  record "gateway /health/ready" FAIL "HTTP $HTTP_CODE (is the stack up?)"
fi

# --- 2. register -------------------------------------------------------------
REG_BODY="$(printf '{"email":"%s","password":"%s"}' "$EMAIL" "$PASSWORD")"
http_call POST "$BASE_URL/api/auth/users" "$REG_BODY"
if is_2xx "$HTTP_CODE"; then
  record "gateway /api/auth/users (register)" PASS "HTTP $HTTP_CODE"
else
  record "gateway /api/auth/users (register)" FAIL "HTTP $HTTP_CODE"
fi

# --- 3. login + token --------------------------------------------------------
LOGIN_BODY="$(printf '{"email":"%s","password":"%s"}' "$EMAIL" "$PASSWORD")"
http_call POST "$BASE_URL/api/auth/login" "$LOGIN_BODY"
TOKEN="$(json_field "$HTTP_BODY" "access_token")"
if is_2xx "$HTTP_CODE" && [ -n "$TOKEN" ] && [ "$TOKEN" != "null" ]; then
  record "gateway /api/auth/login" PASS "HTTP $HTTP_CODE, token acquired"
else
  record "gateway /api/auth/login" FAIL "HTTP $HTTP_CODE, no access_token"
fi

# --- 4. create product directly in catalog (internal mutation) ---------------
PROD_BODY="$(printf '{"sku":"SMOKE-%s","name":"Smoke Product %s","description":"created by smoke-test.sh","price_cents":1999,"stock":50}' "$UNIQ" "$UNIQ")"
http_call POST "$CATALOG_URL/products" "$PROD_BODY" "" "X-Internal-Token: ${INTERNAL_API_TOKEN}"
PRODUCT_ID="$(json_field "$HTTP_BODY" "id")"
if is_2xx "$HTTP_CODE" && [ -n "$PRODUCT_ID" ] && [ "$PRODUCT_ID" != "null" ]; then
  record "catalog POST /products (internal)" PASS "HTTP $HTTP_CODE"
else
  record "catalog POST /products (internal)" FAIL "HTTP $HTTP_CODE"
fi

# --- 5. list products via gateway -------------------------------------------
http_call GET "$BASE_URL/api/catalog/products"
if is_2xx "$HTTP_CODE"; then
  record "gateway /api/catalog/products (list)" PASS "HTTP $HTTP_CODE"
else
  record "gateway /api/catalog/products (list)" FAIL "HTTP $HTTP_CODE"
fi

# --- 6. create order via gateway (bearer token) -----------------------------
if [ -n "$PRODUCT_ID" ] && [ "$PRODUCT_ID" != "null" ] && [ -n "$TOKEN" ]; then
  ORDER_BODY="$(python3 -c 'import json,sys; print(json.dumps({"items":[{"product_id":sys.argv[1],"quantity":2}]}))' "$PRODUCT_ID")"
  http_call POST "$BASE_URL/api/orders" "$ORDER_BODY" "$TOKEN"
  ORDER_ID="$(json_field "$HTTP_BODY" "id")"
  if is_2xx "$HTTP_CODE" && [ -n "$ORDER_ID" ] && [ "$ORDER_ID" != "null" ]; then
    record "gateway /api/orders (create)" PASS "HTTP $HTTP_CODE"
  else
    record "gateway /api/orders (create)" FAIL "HTTP $HTTP_CODE"
  fi
else
  ORDER_ID=""
  record "gateway /api/orders (create)" FAIL "skipped: missing product or token"
fi

# --- 7. fetch order via gateway ---------------------------------------------
if [ -n "${ORDER_ID:-}" ] && [ "$ORDER_ID" != "null" ] && [ -n "$TOKEN" ]; then
  http_call GET "$BASE_URL/api/orders/${ORDER_ID}" "" "$TOKEN"
  FETCHED_ID="$(json_field "$HTTP_BODY" "id")"
  if is_2xx "$HTTP_CODE" && [ "$FETCHED_ID" = "$ORDER_ID" ]; then
    record "gateway /api/orders/{id} (fetch)" PASS "HTTP $HTTP_CODE"
  else
    record "gateway /api/orders/{id} (fetch)" FAIL "HTTP $HTTP_CODE, id mismatch"
  fi
else
  record "gateway /api/orders/{id} (fetch)" FAIL "skipped: no order id"
fi

# --- report ------------------------------------------------------------------
echo
printf '%-40s %-6s %s\n' "STEP" "RESULT" "DETAIL"
printf '%-40s %-6s %s\n' "----------------------------------------" "------" "------------------------------"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r name result detail <<<"$row"
  printf '%-40s %-6s %s\n' "$name" "$result" "$detail"
done

echo
if [ "$FAILED" -eq 0 ]; then
  echo "[smoke-test] PASS - all steps succeeded."
  exit 0
else
  echo "[smoke-test] FAIL - one or more steps failed." >&2
  exit 1
fi
