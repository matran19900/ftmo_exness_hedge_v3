#!/usr/bin/env bash
# Install tracked git hooks from hooks/ into .git/hooks/.
# Idempotent — safe to run on every fresh clone or devcontainer rebuild.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_DIR="$REPO_ROOT/hooks"
DST_DIR="$REPO_ROOT/.git/hooks"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "[install-git-hooks] no hooks/ directory at $SRC_DIR" >&2
  exit 1
fi

mkdir -p "$DST_DIR"

installed=0
for hook in "$SRC_DIR"/*; do
  [[ -f "$hook" ]] || continue
  name=$(basename "$hook")
  cp "$hook" "$DST_DIR/$name"
  chmod +x "$DST_DIR/$name"
  echo "[install-git-hooks] installed $name → .git/hooks/$name"
  installed=$((installed + 1))
done

echo "[install-git-hooks] done ($installed hook(s) installed)"
