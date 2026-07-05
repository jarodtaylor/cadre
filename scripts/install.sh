#!/usr/bin/env bash
# scripts/install.sh — one-time Cadre host setup
#
# This script handles MECHANICAL scaffolding only:
#   - Resolve the Hermes venv Python
#   - pip install cadre into it, then run `cadre setup`: scaffold ~/.cadre,
#     seed fleets/personas/palette-candidates, and record CADRE_HERMES_PYTHON
#     to ~/.cadre/config — all from the installed package, no repo-tree reads
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

# ── 2. Resolve the venv Python ───────────────────────────────────────────────
# python3 scripts/resolve_venv.py prints ONLY the resolved path to stdout;
# diagnostics go to stderr. Passes through any --venv-python arg the operator
# supplied. This is a bootstrap step only — it runs BEFORE cadre is installed
# anywhere, so it stays pure-stdlib (no cadre import).
PYBIN="$(python3 scripts/resolve_venv.py "$@")"

if [ ! -x "$PYBIN" ]; then
    echo "Resolved Hermes Python is not executable: $PYBIN" >&2
    echo "Check --venv-python / CADRE_HERMES_PYTHON, or your Hermes install." >&2
    exit 1
fi

# ── 3. Install cadre into the resolved venv ──────────────────────────────────
# --force-reinstall: pip no-ops a same-version reinstall otherwise (KTD12) —
# this repo-present install should always reflect the current working tree.
# --no-deps: the resolved venv is the Hermes host's, which already carries
# pyyaml from running cadre pre-packaging; a from-scratch venv needs it added.
"$PYBIN" -m pip install --force-reinstall --no-deps .

# ── 4. Provision ~/.cadre + two-phase palette seed ───────────────────────────
# cadre setup scaffolds ~/.cadre, seeds fleets/personas/palette-candidates from
# the installed package (no repo-tree reads), and records $PYBIN as
# CADRE_HERMES_PYTHON — replacing the old resolve_venv.py scaffold/seed/config
# logic (U4). Idempotent: an existing palette-candidates.yaml (operator-edited,
# or seeded by a prior run) is preserved, never overwritten.
CANDIDATES="$HOME/.cadre/palette-candidates.yaml"
CANDIDATES_PRE_EXISTED=0
[ -f "$CANDIDATES" ] && CANDIDATES_PRE_EXISTED=1

"$PYBIN" -m cadre.cli setup

if [ "$CANDIDATES_PRE_EXISTED" -eq 0 ]; then
    # First run: candidates were just seeded from the package example. Stop
    # here so the operator can edit them BEFORE the (paid, live-provider)
    # verify step below runs against real API calls.
    echo ""
    echo "Seeded ~/.cadre/palette-candidates.yaml — EDIT it for your authenticated"
    echo "providers, then re-run ./scripts/install.sh to verify + write the palette."
    echo ""
    echo "See docs/RUNBOOK.md — 'Install & provisioning' for provisioning guidance."
    exit 0
fi

# Second run (operator has had a chance to edit candidates): verify + write palette.
# Verifies candidates against this host and writes ~/.cadre/palette.yaml from the
# installed package (no repo-tree reads) — replacing the now-retired repo-resident
# verification spike (U5).
echo "Running palette verification (requires Hermes host + authenticated providers)..."
if "$PYBIN" -m cadre.cli verify-palette; then
    echo "[OK] palette written to ~/.cadre/palette.yaml"
else
    echo "palette verify failed — fix candidates or provider auth, then re-run." >&2
    exit 1
fi

# ── 5. Install the cadre-fleet skill ─────────────────────────────────────────
# U6 will convert this to `"$PYBIN" -m cadre.cli install-skill`.
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

# ── 6. Next steps footer ─────────────────────────────────────────────────────
echo ""
echo "Setup complete. Next steps:"
echo "  1. Provision your Hermes profile with the terminal toolset + lane search tools"
echo "     (exa/firecrawl for web lanes, SuperGrok/xAI for x_search lanes)."
echo "  2. Confirm install and runtime use the SAME Hermes profile."
echo "  3. Starter fleets are already seeded under ~/.cadre/fleets/ — edit one"
echo "     and set the provider/model strings from ~/.cadre/palette.yaml."
echo "  See docs/RUNBOOK.md — 'Install & provisioning' for the full checklist."
