"""Fleet configuration: typed spec, loader, and validation.

A fleet is defined entirely by a YAML spec — specialists (each a role + provider
+ model + toolset) and a synthesis step. The engine reads these objects; no
fleet-domain strings live in the engine. Loading accumulates every error and
raises ``ConfigError`` once, so a single load surfaces all problems rather than
the first — and malformed inputs become errors, never tracebacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Toolsets that can act beyond reading/searching. A specialist that ingests
# untrusted web content must not silently gain these from a config typo, so they
# require an explicit ``allow_privileged_tools`` opt-in on the fleet.
PRIVILEGED_TOOLSETS = frozenset({"terminal", "code_execution", "file", "computer_use"})


class ConfigError(Exception):
    """Raised when a fleet spec is invalid. Carries every error found, not just the first."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("Invalid fleet config:\n  - " + "\n  - ".join(self.errors))


@dataclass
class SpecialistSpec:
    role: str
    provider: str
    model: str
    focus: str = ""
    toolset: list[str] = field(default_factory=list)


@dataclass
class SynthesisSpec:
    provider: str
    model: str
    prompt: str = ""


@dataclass
class FleetConfig:
    name: str
    synthesis: SynthesisSpec
    specialists: list[SpecialistSpec]
    allow_privileged_tools: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "FleetConfig":
        # FileNotFoundError is intentionally NOT caught here — the CLI/skill handle
        # a missing spec separately. Syntactic and decode errors become ConfigError
        # so a broken file produces the same clean UX as a semantically bad one.
        try:
            data = yaml.safe_load(Path(path).read_text())
        except yaml.YAMLError as err:
            raise ConfigError([f"could not parse YAML: {err}"]) from err
        except UnicodeDecodeError as err:
            raise ConfigError([f"could not read fleet spec as UTF-8 text: {err}"]) from err
        if not isinstance(data, dict):
            raise ConfigError([f"top-level YAML must be a mapping, got {type(data).__name__}"])
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FleetConfig":
        errors: list[str] = []

        name = data.get("name")
        if not name or not isinstance(name, str):
            errors.append("`name` is required and must be a non-empty string")

        allow_priv_raw = data.get("allow_privileged_tools", False)
        if not isinstance(allow_priv_raw, bool):
            errors.append("`allow_privileged_tools` must be a boolean (true/false)")
            allow_priv = False
        else:
            allow_priv = allow_priv_raw

        syn_raw = data.get("synthesis")
        synthesis: SynthesisSpec | None = None
        if not isinstance(syn_raw, dict):
            errors.append("`synthesis` is required and must be a mapping with provider + model")
        else:
            if not syn_raw.get("provider"):
                errors.append("`synthesis.provider` is required")
            if not syn_raw.get("model"):
                errors.append("`synthesis.model` is required")
            synthesis = SynthesisSpec(
                provider=str(syn_raw.get("provider", "")),
                model=str(syn_raw.get("model", "")),
                prompt=str(syn_raw.get("prompt", "")),
            )

        specs_raw = data.get("specialists")
        specialists: list[SpecialistSpec] = []
        if not isinstance(specs_raw, list) or not specs_raw:
            errors.append("`specialists` is required and must be a non-empty list")
        else:
            seen_roles: set[str] = set()
            for i, raw in enumerate(specs_raw):
                label = f"specialists[{i}]"
                if not isinstance(raw, dict):
                    errors.append(f"{label} must be a mapping")
                    continue

                role = raw.get("role")
                if not role or not isinstance(role, str):
                    errors.append(f"{label}.role is required and must be a string")
                    role = None
                else:
                    label = f"specialist '{role}'"
                    if role in seen_roles:
                        errors.append(f"duplicate specialist role '{role}'")
                    seen_roles.add(role)

                if not raw.get("provider"):
                    errors.append(f"{label}.provider is required")
                if not raw.get("model"):
                    errors.append(f"{label}.model is required")

                toolset = raw.get("toolset", []) or []
                if not isinstance(toolset, list):
                    errors.append(f"{label}.toolset must be a list")
                    toolset = []
                elif any(not isinstance(t, str) for t in toolset):
                    errors.append(f"{label}.toolset entries must all be strings")
                    toolset = [t for t in toolset if isinstance(t, str)]

                privileged = sorted(set(toolset) & PRIVILEGED_TOOLSETS)
                if privileged and not allow_priv:
                    errors.append(
                        f"{label} requests privileged toolset(s) {privileged} but "
                        "`allow_privileged_tools: true` is not set on the fleet"
                    )

                specialists.append(
                    SpecialistSpec(
                        role=str(role) if role else "",
                        provider=str(raw.get("provider", "")),
                        model=str(raw.get("model", "")),
                        focus=str(raw.get("focus", "")),
                        toolset=[str(t) for t in toolset],
                    )
                )

        if errors:
            raise ConfigError(errors)

        assert synthesis is not None  # guaranteed: a None synthesis appended an error above
        return cls(
            name=str(name),
            synthesis=synthesis,
            specialists=specialists,
            allow_privileged_tools=allow_priv,
        )
