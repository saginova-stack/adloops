#!/usr/bin/env bash
#
# AdLoops first-run install.
#
# Brings the repo to a state where `cd skill && ../.venv/bin/python -m scripts.run --dry-run`
# can be executed end-to-end (assuming creds are wired). Idempotent — safe to re-run.
#
# What this does:
#   1. Ensures git submodules are checked out (kLOsk/adloop, linkedin-ads-mcp).
#   2. Installs `uv` if missing (needed by the adloop MCP).
#   3. `uv sync` the adloop MCP so its Python deps are ready.
#   4. `npm install && npm run build` the LinkedIn MCP.
#   5. Creates a Python venv at `.venv/` and installs `requirements.txt`.
#   6. Prints next steps (cred wiring + scaffold).
#
# What this does NOT do (intentionally):
#   - Run the OAuth wizards (`uv run adloop init`, `node dist/auth-cli.js`).
#     Those need interactive input and credentials — operator runs them by
#     hand. The install above prepares the trees so those commands work.
#   - Touch `~/Syncthing/adloops-brand/`. Scaffold with `cd skill && ../.venv/bin/python -m scripts.run --scaffold`.
#   - Modify cron. See setup.md §5 for the cron entry.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADLOOP_DIR="$REPO_DIR/skill/mcp-servers/adloop"
LINKEDIN_DIR="$REPO_DIR/skill/mcp-servers/linkedin-ads"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\n\033[1;33m!! \033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31mxx \033[0m %s\n" "$*" >&2; exit 1; }

# --- 1. submodules -----------------------------------------------------------

say "Checking submodules"
cd "$REPO_DIR"
if [ ! -f "$ADLOOP_DIR/pyproject.toml" ] || [ ! -f "$LINKEDIN_DIR/package.json" ]; then
  say "Initializing submodules"
  git submodule update --init --recursive
else
  echo "Both submodules already checked out."
fi

# --- 2. uv -------------------------------------------------------------------

say "Checking uv (required by adloop MCP)"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv via official installer"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin by default
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    die "uv install completed but \`uv\` is still not on PATH. Add ~/.local/bin to PATH and re-run."
  fi
else
  echo "uv $(uv --version | awk '{print $2}') already installed."
fi

# --- 3. adloop MCP (Python / uv) --------------------------------------------

say "Syncing adloop MCP (Google Ads + GA4)"
cd "$ADLOOP_DIR"
uv sync
echo "adloop MCP ready: $(uv run --quiet adloop --help 2>&1 | head -n 1 || echo '(adloop binary linked)')"

# --- 4. LinkedIn MCP (Node / npm) -------------------------------------------

say "Building LinkedIn MCP"
cd "$LINKEDIN_DIR"
if ! command -v node >/dev/null 2>&1; then die "node not found. Install Node 18+ first."; fi
if ! command -v npm >/dev/null 2>&1; then die "npm not found. Install npm first."; fi
npm install --no-audit --no-fund
npm run build
echo "LinkedIn MCP built to: $LINKEDIN_DIR/dist/"

# --- 5. Python venv + skill deps --------------------------------------------

say "Setting up Python venv for the skill itself"
cd "$REPO_DIR"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo "Skill venv ready at .venv/"

# --- 6. Sanity check --------------------------------------------------------

say "Running test suite"
.venv/bin/pytest tests/ -q || warn "Tests failed — investigate before relying on this install."

# --- 7. Next steps ----------------------------------------------------------

cat <<'EOF'

==> Install complete.

Next steps (in order):

  1. Wire credentials. Run one or more of:
       (Google Ads + GA4)  cd skill/mcp-servers/adloop && uv run adloop init
       (LinkedIn)          cd skill/mcp-servers/linkedin-ads && node dist/auth-cli.js
       (Meta)              create system-user token at https://developers.facebook.com/apps/
     Then export the resulting env vars (see setup.md §3).

  2. Scaffold the brand directory and fill in ICP:
       cd skill && ../.venv/bin/python -m scripts.run --scaffold
       # edit ~/Syncthing/adloops-brand/brand.json

  3. Dry-run end to end:
       cd skill && ../.venv/bin/python -m scripts.run --dry-run

  4. Schedule (cron entry in setup.md §5):
       0 9 * * 2,5  cd /home/ubuntu/adloops/skill && ../.venv/bin/python -m scripts.run

EOF
