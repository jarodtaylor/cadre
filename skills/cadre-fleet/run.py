#!/usr/bin/env python
"""Hermes skill entry: run any Cadre fleet on a task.

Generalized runner — takes any fleet spec via --fleet (required). Supports a
--preview mode that renders the parsed FleetConfig to stdout and exits without
making any model calls. Runs on the Hermes host, where hermes-agent and the
user's providers are available.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

# Make the repo root importable when run from the skill directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.capture import DEFAULT_HERMES_HOME, prepare_run_dir, save_run  # noqa: E402
from fleet_engine.config import ConfigError, FleetConfig  # noqa: E402
from fleet_engine.model_client import ModelClient  # noqa: E402
from fleet_engine.progress_runner import run_with_progress  # noqa: E402
from fleet_engine.render import _sanitize, render_fleet_preview, render_result  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Cadre fleet on a task.")
    parser.add_argument("--fleet", required=True, help="Path to the fleet spec YAML")
    parser.add_argument(
        "--task",
        default=None,
        help="The task / query to run (required unless --preview)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help=(
            "Render the parsed fleet config and exit without running. "
            "Shows synthesizer, allow_privileged_tools, each lane, and synthesis "
            "prompt — the human approves this output before any run."
        ),
    )
    parser.add_argument(
        "--no-capture",
        action="store_true",
        default=False,
        help="Disable run capture (no folder written to disk)",
    )
    args = parser.parse_args(argv)

    # Load + validate the fleet. ConfigError and any OSError (missing file, a
    # directory path, bad perms) both produce a clean message and exit 1.
    try:
        cfg = FleetConfig.load(args.fleet)
    except ConfigError as err:
        print(str(err))
        return 1
    except OSError as exc:
        print(
            f"Could not read fleet spec '{args.fleet}': {exc}\n"
            "Pass a specific .yaml file (not a directory). Copy one from the repo's "
            "fleets/ into ~/.cadre/fleets/, or compose one from ~/.cadre/palette.yaml."
        )
        return 1

    # --preview: render the parsed config and exit — BEFORE ModelClient, BEFORE
    # prepare_run_dir, BEFORE run_fleet. Zero model calls, zero capture.
    # THIS SHORT-CIRCUIT IS LOAD-BEARING: the human approves the parsed fleet,
    # not the agent's paraphrase of it.
    if args.preview:
        # Show the profile the run will use (env-sourced, not part of the fleet
        # config) so the human okays the fleet AND the profile it runs under — an
        # unset HERMES_HOME silently falls back to the default, which is how a run
        # lands on the wrong (e.g. ungrounded) profile unnoticed.
        print(f"Profile (HERMES_HOME): {os.getenv('HERMES_HOME', DEFAULT_HERMES_HOME)}")
        print(render_fleet_preview(cfg))
        return 0

    # Real run: --task is required.
    if args.task is None:
        print("--task is required (unless --preview)")
        return 2

    capture = not args.no_capture

    if capture:
        try:
            run_dir = prepare_run_dir(args.task)
        except OSError as exc:
            print(
                f"Cannot create run directory: {exc}\n"
                "Use --no-capture to bypass run capture."
            )
            return 1

    result = run_with_progress(
        cfg,
        args.task,
        ModelClient(),
        run_dir=run_dir if capture else None,
    )
    output = render_result(result)

    if capture:
        try:
            save_run(cfg, result, run_dir)
            output = f"{output}\n\nRun folder: {run_dir}"
        except Exception as exc:  # noqa: BLE001
            # Best-effort warning on the [cadre] stream: sanitize the exception text
            # (a run_dir in the message could carry control bytes) and never let a dead
            # stderr turn a save_run failure into a lost report — the warning print must
            # not raise before print(output).
            with contextlib.suppress(OSError, ValueError):
                print(f"[cadre] warn: failed to save run artifacts: {_sanitize(str(exc))}", file=sys.stderr)

    print(output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
