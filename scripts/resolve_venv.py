"""resolve_venv.py — Resolve the Hermes venv Python path, scaffold ~/.cadre, and record it.

This script is the ONLY unit-tested piece of the U4 install. It is pure stdlib,
importable on the dev machine (no hermes-agent, no fleet_engine import needed).

Three exported functions:
  resolve_venv(override, *, probe_paths, env)  — pure resolver, no I/O
  ensure_cadre_dirs(home)                       — owner-only dir scaffolding
  write_config(python_path, config_path)        — owner-only config writer

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


def ensure_cadre_dirs(home: str = "~/.cadre") -> Path:
    """Create <home> and <home>/fleets owner-only (0o700) under umask(0o077).

    Mirrors capture.prepare_run_dir's umask discipline so every created
    directory component is owner-only from creation — no momentary 0o755.
    Idempotent: safe to call multiple times.

    Args:
        home: Root of the ~/.cadre control dir. Defaults to "~/.cadre".

    Returns:
        The resolved home Path (expanduser'd).
    """
    home_path = Path(home).expanduser()
    old_umask = os.umask(0o077)
    try:
        home_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (home_path / "fleets").mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    return home_path


def write_config(
    python_path: str,
    config_path: str = "~/.cadre/config",
) -> None:
    """Write CADRE_HERMES_PYTHON=<python_path> to config_path, owner-only 0o600.

    Writes a single line: ``CADRE_HERMES_PYTHON=<python_path>\\n``

    Uses the capture._write idiom (os.open with O_CREAT/O_TRUNC at mode 0o600,
    then explicit chmod) so the file is owner-only at creation — never the
    momentary 0o644 a write-then-chmod leaves under a default umask.

    Idempotent: overwrites on re-run (single-key file, O_TRUNC).

    The locked config contract (C2): the skill reads this via:
        PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
    so the line MUST match exactly:
        CADRE_HERMES_PYTHON=<path>\\n
    No quotes around the path, no trailing spaces.

    Args:
        python_path: The resolved Hermes venv Python path to record.
        config_path: Destination config file path. Defaults to "~/.cadre/config".
    """
    path = Path(config_path).expanduser()
    content = f"CADRE_HERMES_PYTHON={python_path}\n"

    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        path.chmod(0o600)
    finally:
        os.umask(old_umask)


def main(argv: list[str] | None = None) -> int:
    """Resolve the Hermes venv python, scaffold ~/.cadre dirs, and write config.

    Prints ONLY the resolved python path to STDOUT (bare path, no trailing
    newline from print — but print adds one, which is fine; the shell's
    command substitution strips trailing newlines).

    All diagnostics go to STDERR so that:
        PYBIN="$(python3 scripts/resolve_venv.py)"
    captures only the path, never stray informational text.

    Returns 0 on success; 1 with a stderr message on resolution failure.
    """
    parser = argparse.ArgumentParser(
        description="Resolve the Hermes venv Python, scaffold ~/.cadre, and record the path.",
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

    # Scaffold dirs and write config; diagnostics to stderr.
    try:
        home = ensure_cadre_dirs()
        print(f"scaffolded {home}", file=sys.stderr)
        write_config(python_path)
        print(f"wrote ~/.cadre/config: CADRE_HERMES_PYTHON={python_path}", file=sys.stderr)
    except OSError as exc:
        print(f"error scaffolding ~/.cadre: {exc}", file=sys.stderr)
        return 1

    # ONLY the resolved path goes to stdout (captured by install.sh).
    print(python_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
