#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/workspaces/ftmo_exness_hedge_v3"
cd "$REPO_ROOT"

echo "[post-create] Verifying Claude Code CLI..."
if ! command -v claude &> /dev/null; then
  echo "  Installing Claude Code CLI globally..."
  npm install -g @anthropic-ai/claude-code
else
  claude --version || true
fi

echo "[post-create] Python version:"
python --version

echo "[post-create] Setting up server venv..."
if [ ! -d "$REPO_ROOT/server" ]; then
  echo "[post-create] server/ not found yet — skipping Python setup."
  echo "  Run Step 1.1 first to create folder structure, then rebuild devcontainer."
  exit 0
fi
cd "$REPO_ROOT/server"
if [ ! -d .venv ]; then
  python -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip

echo "[post-create] Step 1: ctrader-open-api with --no-deps..."
pip install --no-deps "ctrader-open-api==0.9.2"

echo "[post-create] Step 2: protobuf 5.x explicit..."
pip install "protobuf>=4.25,<6"

echo "[post-create] Step 3: ctrader transitive deps (manual)..."
pip install \
  "twisted>=23,<26" \
  "pyOpenSSL>=24,<26" \
  "requests>=2.32,<3" \
  "inputimeout==1.0.4" \
  "service_identity>=24,<26"

echo "[post-create] Step 4: hedger-shared editable..."
cd "$REPO_ROOT/shared"
pip install -e .

echo "[post-create] Step 5: hedger-server editable (--no-deps to bypass resolver)..."
cd "$REPO_ROOT/server"
pip install --no-deps -e .

echo "[post-create] Step 6: hedger-server runtime deps (manual)..."
pip install \
  "aiosqlite>=0.20,<0.21" \
  "alembic>=1.13,<1.14" \
  "bcrypt>=4.2,<5" \
  "fastapi>=0.115,<0.116" \
  "httpx>=0.27,<0.28" \
  "pydantic>=2.7,<3" \
  "pydantic-settings>=2.4,<3" \
  "pyjwt>=2.9,<3" \
  "python-multipart>=0.0.12,<0.1" \
  "redis[hiredis]>=5.0,<6" \
  "sqlalchemy[asyncio]>=2.0,<2.1" \
  "uvicorn[standard]>=0.32,<0.33"

echo "[post-create] Step 7: dev tools..."
pip install \
  "fakeredis[lua]>=2.24" \
  "mypy>=1.10" \
  "pytest>=8" \
  "pytest-asyncio>=0.23" \
  "pytest-mock>=3.14" \
  "ruff>=0.5"

deactivate

cd "$REPO_ROOT"

echo "[post-create] Step 8: web npm install (if package.json exists)..."
if [ -f "$REPO_ROOT/web/package.json" ]; then
  cd "$REPO_ROOT/web"
  npm install
  cd "$REPO_ROOT"
else
  echo "  Skipped: web/package.json not present yet (will be created in step 1.4)"
fi

echo "[post-create] Done."
echo ""
echo "Next steps:"
echo "  1. cd server && source .venv/bin/activate"
echo "  2. pytest          # baseline check (no tests yet — expected)"
echo "  3. claude --dangerously-skip-permissions"
