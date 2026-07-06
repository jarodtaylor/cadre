#!/usr/bin/env bash
# scripts/install.sh — one-time Cadre host setup
#
# This is the repo-present convenience wrapper: it installs cadre from THIS
# checkout (`pip install .` below), for when you already have — or want — a
# clone on the host (e.g. to iterate on unreleased changes, or run the test
# suite for extra confidence). A clone is not required to install cadre at
# all: on a clone-less host, run the same verbs directly against the git URL
# instead of step 3's `pip install .` — see docs/RUNBOOK.md. Either way,
# NEVER `pip install cadre` bare: that is a different, unrelated package on
# PyPI. Install from this checkout (`.`) or from the git URL.
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
# --no-deps: protects the Hermes venv's own pinned dependencies from being
# upgraded/downgraded by cadre's requirements.
"$PYBIN" -m pip install --force-reinstall --no-deps .

# --no-deps above means pip never installs pyyaml for us — but cadre imports
# it at startup (cadre.config), so a venv that doesn't already have one
# (e.g. a fresh Hermes install) would fail at `cadre setup` below. Install it
# WITHOUT force-reinstalling (never touches a pyyaml Hermes already pinned),
# and only when it's actually missing.
"$PYBIN" -c "import yaml" 2>/dev/null || "$PYBIN" -m pip install "pyyaml>=6.0"

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
    # First run: candidates were just seeded — from Hermes's own inventory when
    # discovery was available (#61), else the package placeholder. Stop here so
    # the operator can REVIEW them BEFORE the (paid, live-provider) verify step
    # below runs against real API calls.
    echo ""
    echo "Seeded ~/.cadre/palette-candidates.yaml (auto-discovered from Hermes when"
    echo "available — the setup output above says which). Review/edit it, then"
    echo "re-run ./scripts/install.sh to verify + write the palette."
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
# cadre install-skill materializes SKILL.md + run.py from the INSTALLED package
# (cadre/data/skill/, via importlib.resources) into the skills dir as an atomic
# symlink — no repo path involved, so it survives the clone moving or being
# deleted (R14/R15) and a same-Python in-place `cadre` upgrade (R16). It reads
# HERMES_SKILLS_DIR itself when no --skills-dir is given.
#
# SKILL_INSTALL_FAILED tracks an actual install-skill failure (CodeRabbit CR3)
# so the script's final exit code reflects it — the else branch below used to
# only warn to stderr while the script still exited 0, hiding a real failure
# behind an apparently-clean run. Left 0 when HERMES_SKILLS_DIR is unset:
# that's "not attempted", not a failure.
SKILL_INSTALL_FAILED=0
if [ -n "${HERMES_SKILLS_DIR:-}" ]; then
    if "$PYBIN" -m cadre.cli install-skill; then
        echo "[OK] installed cadre-fleet skill"
    else
        echo "install-skill failed — see the message above; the skill was not installed." >&2
        SKILL_INSTALL_FAILED=1
    fi
else
    echo ""
    echo "HERMES_SKILLS_DIR is not set. To install the skill manually, run:"
    echo "  HERMES_SKILLS_DIR=/path/to/hermes/skills $PYBIN -m cadre.cli install-skill"
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

# Non-zero if step 5's install-skill actually ran and failed — the operator
# gets the footer above AND a clear failure signal (CodeRabbit CR3), instead
# of a silent exit 0 that reads as a fully clean run.
exit "$SKILL_INSTALL_FAILED"
