"""resolve_venv.py — Resolve the Hermes venv Python path.

This script does ONE thing: figure out which Python interpreter is the
Hermes venv's, and print its path to stdout. It is pure stdlib — no ``cadre``
import, no repo-relative sys.path bootstrap needed, so it works standalone
even before ``cadre`` is installed anywhere.

Scaffolding ``~/.cadre``, seeding starter fleets/personas/palette-candidates,
and writing ``~/.cadre/config`` all moved to ``cadre.provision`` / the
``cadre setup`` console command (U4,
docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md) — this script is
purely the bootstrap step install.sh uses to find $PYBIN *before* installing
and provisioning cadre into it:

    PYBIN="$(python3 scripts/resolve_venv.py)"
    "$PYBIN" -m pip install --force-reinstall --no-deps .
    "$PYBIN" -m cadre.cli setup

Two exported names:
  resolve_venv(override, *, probe_paths, env)  — pure resolver, no I/O
  KNOWN_PROBE_PATHS                            — default probe list

main(argv) — argparse entry; prints ONLY the resolved python path to stdout;
             all diagnostics go to stderr. Designed for:

    PYBIN="$(python3 scripts/resolve_venv.py)"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Known Hermes venv python paths, probed in order when no override is given.
# Root-Linux install first (VPS); user-local install second.
KNOWN_PROBE_PATHS = [
    "/usr/local/lib/hermes-agent/venv/bin/python",
    "~/.hermes/hermes-agent/venv/bin/python",
]


def resolve_venv(
    override: str | None = None,
    *,
    probe_paths: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Resolve the Hermes venv Python. Returns the resolved path string.

    Precedence:
      1. explicit ``override`` arg (e.g. --venv-python), expanduser'd — returned
         even if the path does not exist (operator knows their host).
      2. env['CADRE_HERMES_PYTHON'] (env defaults to os.environ), expanduser'd —
         same: returned verbatim without an existence check.
      3. first existing path in probe_paths (defaults to KNOWN_PROBE_PATHS),
         each expanduser'd and checked with Path.exists().
      4. else raise a clear RuntimeError naming the override knobs + probed paths.

    PURE: only reads env + checks path existence (step 3 only); writes nothing.

    Args:
        override: Explicit python path (e.g. from --venv-python). Takes highest
                  precedence. expanduser'd; existence NOT checked.
        probe_paths: Ordered list of candidate paths to probe. Defaults to
                     KNOWN_PROBE_PATHS.
        env: Mapping to use instead of os.environ. Defaults to os.environ.

    Returns:
        The resolved python path as a string (expanduser'd, no trailing space).

    Raises:
        RuntimeError: If no path can be resolved, with a message naming the
                      override knobs and all probed paths.
    """
    if env is None:
        env = os.environ

    # 1. Explicit override — highest precedence; no existence check.
    if override is not None:
        return str(Path(override).expanduser())

    # 2. Environment variable — second precedence; no existence check.
    env_val = env.get("CADRE_HERMES_PYTHON")
    if env_val:
        return str(Path(env_val).expanduser())

    # 3. Probe known paths — first existing one wins.
    if probe_paths is None:
        probe_paths = KNOWN_PROBE_PATHS

    for candidate in probe_paths:
        resolved = Path(candidate).expanduser()
        if resolved.exists():
            return str(resolved)

    # 4. Nothing found — clear error naming all knobs and probed paths.
    probed_str = "\n  ".join(str(Path(p).expanduser()) for p in probe_paths)
    raise RuntimeError(
        "Could not find the Hermes venv Python. Tried:\n"
        f"  {probed_str}\n"
        "Override with:\n"
        "  --venv-python /path/to/python\n"
        "  CADRE_HERMES_PYTHON=/path/to/python ./scripts/install.sh"
    )


def main(argv: list[str] | None = None) -> int:
    """Resolve the Hermes venv python and print it to stdout.

    Prints ONLY the resolved python path to STDOUT (bare path, no trailing
    newline from print — but print adds one, which is fine; the shell's
    command substitution strips trailing newlines).

    All diagnostics go to STDERR so that:
        PYBIN="$(python3 scripts/resolve_venv.py)"
    captures only the path, never stray informational text.

    Returns 0 on success; 1 with a stderr message on resolution failure.
    """
    parser = argparse.ArgumentParser(
        description="Resolve the Hermes venv Python and print its path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/resolve_venv.py\n"
            "  python3 scripts/resolve_venv.py --venv-python /usr/local/lib/hermes-agent/venv/bin/python\n"
            "  CADRE_HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python python3 scripts/resolve_venv.py"
        ),
    )
    parser.add_argument(
        "--venv-python",
        metavar="PATH",
        help="Explicit Hermes venv Python path (highest precedence; overrides env and probing).",
    )
    args = parser.parse_args(argv)

    try:
        python_path = resolve_venv(override=args.venv_python)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # ONLY the resolved path goes to stdout (captured by install.sh).
    print(python_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
