#!/usr/bin/env python
"""Hermes skill entry: run the research-swarm fleet on a task.

Thin wrapper — it calls the engine directly (not the CLI) and prints the
rendered result. Invoked by SKILL.md with the task as the argument. Runs on the
Hermes host, where hermes-agent and the user's providers are available.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the repo root importable when run from the skill directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.capture import resolve_run_dir, save_run  # noqa: E402
from fleet_engine.config import ConfigError, FleetConfig  # noqa: E402
from fleet_engine.engine import run_fleet  # noqa: E402
from fleet_engine.model_client import ModelClient  # noqa: E402
from fleet_engine.render import render_result  # noqa: E402

DEFAULT_FLEET = _REPO_ROOT / "fleets" / "research-swarm.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the research-swarm fleet.")
    parser.add_argument("--task", required=True, help="The research task / query")
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET), help="Path to the fleet spec")
    parser.add_argument(
        "--no-capture",
        action="store_true",
        default=False,
        help="Disable run capture (no folder written to disk)",
    )
    args = parser.parse_args(argv)

    capture = not args.no_capture

    try:
        cfg = FleetConfig.load(args.fleet)
    except ConfigError as err:
        print(str(err))
        return 1
    except FileNotFoundError:
        print(f"Fleet spec not found: {args.fleet}\n"
              "Copy fleets/research-swarm.example.yaml to fleets/research-swarm.yaml "
              "and set your confirmed provider+model strings.")
        return 1

    if capture:
        run_dir = resolve_run_dir(args.task)
        try:
            old_umask = os.umask(0o077)
            try:
                run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            finally:
                os.umask(old_umask)
        except OSError as exc:
            print(
                f"Cannot create run directory {run_dir}: {exc}\n"
                "Use --no-capture to bypass run capture."
            )
            return 1

    result = run_fleet(cfg, args.task, ModelClient())
    output = render_result(result)

    if capture:
        try:
            save_run(cfg, result, run_dir)
            output = f"{output}\n\nRun folder: {run_dir}"
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to save run artifacts: {exc}", file=sys.stderr)

    print(output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
