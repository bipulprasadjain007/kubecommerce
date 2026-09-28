#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - local developer bootstrap (idempotent, safe to re-run).
#
#   * Verifies `uv` is available.
#   * Creates `.secrets/` (mode 700) and an RSA-2048 JWT keypair if missing.
#   * Copies `.env.example` -> `.env` if `.env` does not exist (never overwrites).
#   * Prints the next steps.
#
# It never overwrites existing keys or an existing `.env`, and never prints
# secret material.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SECRETS_DIR="${SECRETS_DIR:-.secrets}"
PRIVATE_KEY="$SECRETS_DIR/jwt-private.pem"
PUBLIC_KEY="$SECRETS_DIR/jwt-public.pem"

log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] WARNING: %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. uv -------------------------------------------------------------------
UV_BIN="${UV:-uv}"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    die "uv not found. Install it with:
       curl -LsSf https://astral.sh/uv/install.sh | sh
       # or:  pipx install uv
     then re-run this script."
  fi
fi
log "using uv: $("$UV_BIN" --version)"

# --- 2. JWT keypair ----------------------------------------------------------
if [ ! -d "$SECRETS_DIR" ]; then
  mkdir -p "$SECRETS_DIR"
  log "created $SECRETS_DIR"
fi
chmod 700 "$SECRETS_DIR" 2>/dev/null || true

if command -v openssl >/dev/null 2>&1; then
  if [ -f "$PRIVATE_KEY" ] && [ -f "$PUBLIC_KEY" ]; then
    log "JWT keypair already present in $SECRETS_DIR (left untouched)"
  else
    if [ ! -f "$PRIVATE_KEY" ]; then
      log "generating RSA-2048 JWT private key -> $PRIVATE_KEY"
      openssl genpkey -algorithm RSA \
        -pkeyopt rsa_keygen_bits:2048 \
        -out "$PRIVATE_KEY" >/dev/null 2>&1
      chmod 600 "$PRIVATE_KEY"
    fi
    if [ ! -f "$PUBLIC_KEY" ]; then
      log "deriving JWT public key -> $PUBLIC_KEY"
      openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" >/dev/null 2>&1
      chmod 644 "$PUBLIC_KEY"
    fi
  fi
else
  warn "openssl not found; skipping JWT key generation."
  warn "Install openssl or provide keys via AUTH_JWT_PRIVATE_KEY(_FILE)."
fi

# --- 3. .env -----------------------------------------------------------------
if [ -f .env ]; then
  log ".env already exists (left untouched)"
else
  if [ -f .env.example ]; then
    cp .env.example .env
    log "created .env from .env.example"
  else
    warn ".env.example missing; cannot create .env"
  fi
fi

# --- 4. Next steps -----------------------------------------------------------
cat <<'EOF'

[bootstrap] Done. Next steps:
  1. make install          # uv sync every lib/service + install pre-commit hooks
  2. make test             # run unit tests (no Docker required)
  3. make compose-up       # start the full local stack (requires Docker)
  4. make smoke-test       # exercise register -> login -> product -> order
  5. make load-test        # optional k6 load test (requires k6)

Local secrets live in .secrets/ (git-ignored). Never commit them.
EOF
