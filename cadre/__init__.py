"""Agent Fleet Engine — provider-neutral, ephemeral, multi-model agent fleets.

The engine runs one orchestration primitive (parallel fan-out -> synthesize)
over a fleet defined entirely by config. All live model calls are isolated in
``cadre.model_client``; the rest of the package imports and tests without
``hermes-agent`` installed.
"""

__version__ = "0.1.0"
