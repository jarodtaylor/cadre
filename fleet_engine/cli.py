"""Standalone CLI: ``validate`` a fleet spec and ``run`` it on a task.

This is the no-Hermes entry surface — it enables the baseline gut-check and
dogfooding. Rendering lives in ``fleet_engine.render``, shared with the skill.
Usage:

    python -m fleet_engine.cli validate fleets/research-swarm.yaml
    python -m fleet_engine.cli run fleets/research-swarm.yaml --task "..."
"""

from __future__ import annotations

import argparse

from fleet_engine.config import ConfigError, FleetConfig
from fleet_engine.engine import run_fleet
from fleet_engine.model_client import ModelClient
from fleet_engine.render import render_result


def validate_command(path: str) -> tuple[int, str]:
    try:
        cfg = FleetConfig.load(path)
    except ConfigError as err:
        return 1, str(err)
    except FileNotFoundError:
        return 1, f"Fleet spec not found: {path}"
    lines = [f"OK: {cfg.name}", f"  specialists: {len(cfg.specialists)}"]
    for s in cfg.specialists:
        lines.append(f"    - {s.role}: {s.provider}/{s.model} tools={s.toolset}")
    lines.append(f"  synthesis: {cfg.synthesis.provider}/{cfg.synthesis.model}")
    return 0, "\n".join(lines)


def run_command(path: str, task: str, client: ModelClient | None = None) -> tuple[int, str]:
    try:
        cfg = FleetConfig.load(path)
    except ConfigError as err:
        return 1, str(err)
    except FileNotFoundError:
        return 1, f"Fleet spec not found: {path}"
    result = run_fleet(cfg, task, client or ModelClient())
    return (0 if result.ok else 1), render_result(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet", description="Run provider-neutral agent fleets.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate a fleet spec")
    p_validate.add_argument("spec", help="Path to a fleet YAML spec")

    p_run = sub.add_parser("run", help="Run a fleet on a task")
    p_run.add_argument("spec", help="Path to a fleet YAML spec")
    p_run.add_argument("--task", required=True, help="The task / query for the fleet")

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        code, out = validate_command(args.spec)
    else:
        code, out = run_command(args.spec, args.task)
    print(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
