#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - per-project MyPy type checking.
#
# Each library/service is an independent uv project. For every project that has
# a pyproject.toml we run `uv run mypy <package>` inside that project so MyPy
# resolves the project's own dependencies:
#   * libraries:  the top-level `kubecommerce_*` package (e.g. kubecommerce_contracts)
#   * services:   the `app` package when an `app/` directory exists
#
# Fails on the first project that reports an error. Missing/misconfigured
# projects are skipped gracefully so the script is safe to run during the
# incremental build-out of the monorepo.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

UV_BIN="${UV:-uv}"
if ! command -v "$UV_BIN" >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
  UV_BIN="$HOME/.local/bin/uv"
fi

shopt -s nullglob
projects=(libs/*/ services/*/)
shopt -u nullglob

checked=0
for dir in "${projects[@]}"; do
  [ -f "${dir}pyproject.toml" ] || continue

  pkg=""
  if [ -d "${dir}app" ]; then
    pkg="app"
  else
    shopt -s nullglob
    candidates=("${dir}"kubecommerce_*/)
    shopt -u nullglob
    if [ "${#candidates[@]}" -gt 0 ]; then
      pkg="$(basename "${candidates[0]}")"
    fi
  fi

  if [ -z "$pkg" ]; then
    echo "[typecheck] SKIP ${dir} (no app/ or kubecommerce_* package found)"
    continue
  fi

  echo "[typecheck] ${dir} -> mypy ${pkg}"
  ( cd "$dir" && "$UV_BIN" run mypy "$pkg" )
  checked=$((checked + 1))
done

if [ "$checked" -eq 0 ]; then
  echo "[typecheck] no uv projects found yet; nothing to check."
fi
