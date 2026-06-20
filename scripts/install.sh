#!/usr/bin/env bash
# scripts/install.sh — one-time Cadre host setup
#
# This script handles MECHANICAL scaffolding only:
#   - Resolve + record the Hermes venv Python to ~/.cadre/config
#   - Scaffold ~/.cadre/{,fleets/} owner-only
#   - Seed ~/.cadre/palette-candidates.yaml (first run only; edit then re-run)
#   - Verify candidates + write ~/.cadre/palette.yaml (second run, after your edits)
#   - Install the cadre-fleet skill symlink if HERMES_SKILLS_DIR is set
#
# Provider/tool provisioning and the one-profile rule are JUDGMENT steps.
# See docs/RUNBOOK.md — "Install & provisioning (the agent-run handoff)".
#
# Usage:
#   ./scripts/install.sh
#   ./scripts/install.sh --venv-python /usr/local/lib/hermes-agent/venv/bin/python
#   CADRE_HERMES_PYTHON=/path/to/python ./scripts/install.sh

set -euo pipefail

# ── 1. cd to repo root ──────────────────────────────────────────────────────
# Derive from the location of this script so the caller's cwd doesn't matter.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── 2. Resolve + record venv + scaffold ~/.cadre ─────────────────────────────
# python3 scripts/resolve_venv.py prints ONLY the resolved path to stdout.
# Diagnostics (scaffold confirmations, config write) go to stderr.
# Passes through any --venv-python arg the operator supplied.
PYBIN="$(python3 scripts/resolve_venv.py "$@")"

if [ ! -x "$PYBIN" ]; then
    echo "Resolved Hermes Python is not executable: $PYBIN" >&2
    echo "Check --venv-python / CADRE_HERMES_PYTHON, or your Hermes install." >&2
    exit 1
fi

# ── 3. Two-phase palette seed ────────────────────────────────────────────────
CANDIDATES="$HOME/.cadre/palette-candidates.yaml"

if [ ! -f "$CANDIDATES" ]; then
    # First run: seed the candidates file from the example; stop here.
    cp fleets/palette.example.yaml "$CANDIDATES"
    echo ""
    echo "Seeded ~/.cadre/palette-candidates.yaml — EDIT it for your authenticated"
    echo "providers, then re-run ./scripts/install.sh to verify + write the palette."
    echo ""
    echo "See docs/RUNBOOK.md — 'Install & provisioning' for provisioning guidance."
    exit 0
fi

# Second run (operator has had a chance to edit candidates): verify + write palette.
echo "Running palette verification (requires Hermes host + authenticated providers)..."
if "$PYBIN" spikes/verify_aiagent_providers.py; then
    echo "[OK] palette written to ~/.cadre/palette.yaml"
else
    echo "palette verify failed — fix candidates or provider auth, then re-run." >&2
    exit 1
fi

# ── 4. Install the cadre-fleet skill ─────────────────────────────────────────
if [ -n "${HERMES_SKILLS_DIR:-}" ]; then
    # Expand a leading ~ (it does NOT expand inside a quoted variable) and ensure
    # the dir exists, so a ~-prefixed or not-yet-created skills dir still works.
    DEST="${HERMES_SKILLS_DIR/#\~/$HOME}"
    mkdir -p "$DEST"
    ln -sfn "$(pwd)/skills/cadre-fleet" "$DEST/cadre-fleet"
    echo "[OK] installed cadre-fleet skill → $DEST/cadre-fleet"
else
    echo ""
    echo "HERMES_SKILLS_DIR is not set. To install the skill manually, run:"
    echo "  ln -sfn $(pwd)/skills/cadre-fleet /path/to/hermes/skills/cadre-fleet"
    echo "See docs/RUNBOOK.md for your Hermes install's skills directory location."
fi

# ── 5. Next steps footer ─────────────────────────────────────────────────────
echo ""
echo "Setup complete. Next steps:"
echo "  1. Provision your Hermes profile with the terminal toolset + lane search tools"
echo "     (exa/firecrawl for web lanes, SuperGrok/xAI for x_search lanes)."
echo "  2. Confirm install and runtime use the SAME Hermes profile."
echo "  3. Copy a fleet from fleets/*.example.yaml → ~/.cadre/fleets/<name>.yaml"
echo "     and set the provider/model strings from ~/.cadre/palette.yaml."
echo "  See docs/RUNBOOK.md — 'Install & provisioning' for the full checklist."
