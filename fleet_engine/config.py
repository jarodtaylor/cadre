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

# Toolsets SAFE for a specialist that ingests untrusted web content: they read,
# search, analyze, reason, or generate output content and take no action on
# external systems or the local machine. The gate is an ALLOWLIST (fail-closed):
# anything not listed here requires an explicit ``allow_privileged_tools: true``
# opt-in on the fleet — shell/file/code/browser/computer control, composites that
# bundle them (e.g. ``debugging`` = web+file+terminal), outbound-action or
# external-account tools (``messaging``, ``cronjob``, ``discord``, ...), subagent
# spawning (``delegation``), AND any unrecognized name. A denylist would silently
# wave through the next renamed/added/composite toolset; fail-closed makes a config
# typo or a new Hermes capability error loudly instead of leaking privilege into a
# lane processing untrusted content. Names verified against hermes-agent's
# toolsets.py; err small — the opt-in covers anything under-included here.
SAFE_TOOLSETS = frozenset({
    "web", "search", "x_search",      # web + X search/scrape (read-only)
    "vision", "video",                # image / video analysis (read-only)
    "image_gen", "video_gen", "tts",  # content generation (output only, no system access)
    "moa",                            # mixture-of-agents reasoning (no system access)
    "todo", "clarify",                # internal task planning / user clarification (no system access)
    "safe",                           # Hermes's own safe composite (web + vision + image_gen)
})


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
    specialists: list[SpecialistSpec]
    synthesis: SynthesisSpec | None = None
    convergence: str = "synthesize"
    description: str = ""
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

        conv_raw = data.get("convergence", "synthesize")
        if conv_raw not in {"synthesize", "collect"}:
            errors.append("`convergence` must be one of: synthesize, collect")
            convergence = "synthesize"
        else:
            convergence = conv_raw

        description = str(data.get("description", ""))

        syn_raw = data.get("synthesis")
        synthesis: SynthesisSpec | None = None
        if convergence == "synthesize":
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
        elif isinstance(syn_raw, dict):
            # collect mode: synthesis block is optional; parse it if present
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

                unsafe = sorted(set(toolset) - SAFE_TOOLSETS)
                if unsafe and not allow_priv:
                    errors.append(
                        f"{label} requests non-safe toolset(s) {unsafe}: they can act "
                        "beyond reading/searching (code/file/shell/browser/computer or "
                        "outbound actions), bundle such tools, or are unrecognized. Set "
                        "`allow_privileged_tools: true` on the fleet to permit them."
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

        # In synthesize mode, a None synthesis means an error was accumulated above.
        assert convergence != "synthesize" or synthesis is not None
        return cls(
            name=str(name),
            specialists=specialists,
            synthesis=synthesis,
            convergence=convergence,
            description=description,
            allow_privileged_tools=allow_priv,
        )
