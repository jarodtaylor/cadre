"""Fleet configuration: typed spec, loader, and validation.

A fleet is defined entirely by a YAML spec — specialists (each a role + provider
+ model + toolset) and a synthesis step. The engine reads these objects; no
fleet-domain strings live in the engine. Loading accumulates every error and
raises ``ConfigError`` once, so a single load surfaces all problems rather than
the first — and malformed inputs become errors, never tracebacks.
"""

from __future__ import annotations

import re
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

# Persona-name allowlist (KTD4 / R10): must start with an alphanumeric char so bare `.`,
# `..`, leading-dot hidden names, separators (`/`, `\`), absolute paths, spaces, and NUL
# are all rejected. Validated at parse time (NO file I/O) in ``from_dict``.
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(Exception):
    """Raised when a fleet spec is invalid. Carries every error found, not just the first.

    ``header`` defaults to the fleet-config framing but is overridable so a caller
    that reuses the same error-accumulation + single-catch idiom for a *different*
    failure category (e.g. ``file_input.compose`` reading ``--doc`` files) does not
    misattribute its errors to the fleet YAML. Existing callers pass no header and
    are unchanged.
    """

    def __init__(self, errors: list[str], *, header: str = "Invalid fleet config:"):
        self.errors = list(errors)
        super().__init__(header + "\n  - " + "\n  - ".join(self.errors))


@dataclass
class SpecialistSpec:
    role: str
    provider: str
    model: str
    focus: str = ""
    toolset: list[str] = field(default_factory=list)
    persona: str = ""
    effective_instruction: str = ""


@dataclass
class SynthesisSpec:
    provider: str
    model: str
    prompt: str = ""


@dataclass
class JudgeSpec:
    provider: str
    model: str
    prompt: str = ""


@dataclass(kw_only=True)
class FleetConfig:
    name: str
    specialists: list[SpecialistSpec]
    synthesis: SynthesisSpec | None = None
    judge: JudgeSpec | None = None
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
        # isinstance guard FIRST: a non-str value (e.g. `convergence: [collect]`) is
        # unhashable and would raise TypeError on the set membership test — accumulate a
        # ConfigError instead, honoring the loader's malformed-input-never-tracebacks contract.
        if not isinstance(conv_raw, str) or conv_raw not in {"synthesize", "collect", "judge"}:
            errors.append("`convergence` must be one of: synthesize, collect, judge")
            convergence = "synthesize"
        else:
            convergence = conv_raw

        # An explicit but empty `description:` parses as YAML null (None); str(None)
        # would render the literal "None" in the preview, so map null -> "".
        desc_raw = data.get("description", "")
        description = "" if desc_raw is None else str(desc_raw)

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
        elif convergence == "collect" and isinstance(syn_raw, dict):
            # collect mode: synthesis block is optional; parse it if present
            synthesis = SynthesisSpec(
                provider=str(syn_raw.get("provider", "")),
                model=str(syn_raw.get("model", "")),
                prompt=str(syn_raw.get("prompt", "")),
            )

        judge_raw = data.get("judge")
        judge: JudgeSpec | None = None
        if convergence == "judge":
            if not isinstance(judge_raw, dict):
                errors.append("`judge` is required and must be a mapping with provider + model")
            else:
                if not judge_raw.get("provider"):
                    errors.append("`judge.provider` is required")
                if not judge_raw.get("model"):
                    errors.append("`judge.model` is required")
                judge = JudgeSpec(
                    provider=str(judge_raw.get("provider", "")),
                    model=str(judge_raw.get("model", "")),
                    prompt=str(judge_raw.get("prompt", "")),
                )
        # convergence != "judge": a stray judge: block is ignored; judge stays None.

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
                    # Role-label hygiene: the role is emitted verbatim as a judge
                    # per-lane label (`=== LANE: <role> ===`) and matched back on the
                    # exact stripped string. A role with leading/trailing whitespace,
                    # control chars, or the `===` delimiter cannot round-trip — the
                    # judge copies it exactly yet the parser strips/splits it, silently
                    # losing every grade for an otherwise-valid fleet. Reject loud.
                    if role != role.strip() or any(ord(c) < 32 for c in role) or "===" in role:
                        errors.append(
                            f"{label}.role {role!r} must not contain leading/trailing "
                            "whitespace, control characters, or '===' — the role is used "
                            "verbatim as a per-lane label and must round-trip exactly."
                        )

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

                # Parse persona and focus; validate the XOR invariant (KTD5) and
                # name allowlist (KTD4 / R10). Both checks accumulate — don't fail-fast.
                persona_raw = raw.get("persona", "")
                if not isinstance(persona_raw, str):
                    errors.append(f"{label}.persona must be a string")
                    persona_raw = ""
                persona = persona_raw

                focus_raw = raw.get("focus", "")
                if focus_raw is not None and not isinstance(focus_raw, str):
                    errors.append(f"{label}.focus must be a string")
                    focus_raw = ""
                focus = str(focus_raw) if focus_raw is not None else ""

                # persona XOR non-empty focus: whitespace-only focus counts as absent
                # (same intent as empty-string), matching the engine's `if spec.focus`
                # convention. A focus of "   " with no persona triggers the "neither
                # set" error, not a silently-empty instruction.
                has_persona = bool(persona)
                has_focus = bool(focus.strip())
                if has_persona and has_focus:
                    errors.append(
                        f"{label} specifies both `persona` and `focus` — exactly one "
                        "instruction source is permitted; remove one."
                    )
                elif not has_persona and not has_focus:
                    errors.append(
                        f"{label} specifies neither `persona` nor `focus` — at least one "
                        "instruction source is required."
                    )

                # Persona-name allowlist (R10 / KTD4): see module-level ``_PERSONA_NAME_RE``.
                # NO file I/O here — validation is parse-time only.
                if has_persona and not _PERSONA_NAME_RE.fullmatch(persona):
                    errors.append(
                        f"{label}.persona name {persona!r} is not a valid pool name — "
                        "must match [A-Za-z0-9][A-Za-z0-9._-]* (no path separators, "
                        "spaces, leading dots, or absolute paths)."
                    )

                specialists.append(
                    SpecialistSpec(
                        role=str(role) if role else "",
                        provider=str(raw.get("provider", "")),
                        model=str(raw.get("model", "")),
                        focus=focus,
                        toolset=[str(t) for t in toolset],
                        persona=persona,
                        # Focus is a complete, no-I/O instruction — populate the carrier at
                        # parse so a focus-only fleet runs straight from FleetConfig.load()
                        # with no resolve() step (preserving the pre-persona engine API). A
                        # persona has focus="" here, so its carrier stays empty until the
                        # caller-layer resolver reads the file.
                        effective_instruction=focus,
                    )
                )

        if errors:
            raise ConfigError(errors)

        # In synthesize mode, a None synthesis means an error was accumulated above.
        # In judge mode, a None judge means an error was accumulated above.
        assert convergence != "synthesize" or synthesis is not None
        assert convergence != "judge" or judge is not None
        return cls(
            name=str(name),
            specialists=specialists,
            synthesis=synthesis,
            judge=judge,
            convergence=convergence,
            description=description,
            allow_privileged_tools=allow_priv,
        )
