"""Content-guard tests for the five shipped doc-review persona files.

Guards the invariants in R1/R11/R12 of the persona-files plan:
- Exactly the five expected files exist in personas/ with the specified names.
- Each file is substantive (non-trivial length).
- Each carries the no-tools / pasted-content framing and a grounding phrase.
- Each contains no provider/model binding (no YAML frontmatter, no model tokens).
- Each contains no dangling ce-orchestration strings.
- Each contains no zero-width or BOM characters.

Paths are resolved relative to the repo root (tests/../personas/), following
the _REPO pattern established in test_starter_fleets.py.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PERSONAS_DIR = _REPO / "personas"

# The five persona files that must exist in personas/ (R1/R11 — exact names).
_EXPECTED_PERSONAS = {
    "coherence-reviewer.md",
    "feasibility-reviewer.md",
    "scope-guardian-reviewer.md",
    "product-reviewer.md",
    "adversarial-document-reviewer.md",
}

# Minimum character count to pass the "substantive" check.  Each authored
# persona is several hundred words; 300 chars is a very conservative floor.
_MIN_CHARS = 300

# Phrase that must appear in every persona (R12 — no-tools / pasted-content
# framing).  Use the literal phrase authored in each file so a future content
# change that removes the instruction is caught rather than silently passing.
_NO_TOOLS_PHRASE = "NO tools available"

# Grounding-control phrases — both must appear in each persona (R12).
_CITE_PHRASE = "cite"
_NO_FINDING_PHRASE = "no finding"

# Provider / model tokens that must NOT appear (R1 — no binding in persona).
# Natural-language-risky words are matched on word boundaries so ordinary prose
# ("magnum opus", "grok the spec", "a haiku") cannot spuriously trip the guard;
# unambiguous provider/slug tokens that never occur in prose stay plain substrings.
_BANNED_MODEL_WORDS = ["haiku", "sonnet", "opus", "grok", "gemini"]
_BANNED_MODEL_SLUGS = ["gpt-", "deepseek", "openrouter", "anthropic", "openai", "xai"]

# Structural binding indicators that must NOT appear (R1 — no YAML frontmatter,
# no model/tools keys).
_BANNED_STRUCTURAL = [
    "model:",
    "tools:",
]

# Dangling ce-orchestration strings that must NOT appear (R11).
_BANNED_ORCHESTRATION = [
    "Document type:",
    "Origin:",
    "safe_auto",
    "confidence: 0",
    "confidence: 25",
    "confidence: 50",
    "confidence: 75",
    "confidence: 100",
    "subagent-template",
    "<review-context>",
]

# Zero-width and BOM characters that must NOT appear (R11 content guard).
_BANNED_UNICODE = [
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "﻿",  # BOM
]


def _read(name: str) -> str:
    """Read a persona file and return its text."""
    return (_PERSONAS_DIR / name).read_text(encoding="utf-8")


class TestStarterPersonasExist(unittest.TestCase):
    """Exactly the five expected persona files exist — no extras, no missing."""

    def test_exactly_five_expected_files(self):
        """personas/ contains exactly the five expected .md files (glob, not listdir)."""
        found = {p.name for p in _PERSONAS_DIR.glob("*.md")}
        self.assertEqual(
            found,
            _EXPECTED_PERSONAS,
            f"personas/ must contain exactly {_EXPECTED_PERSONAS}, got {found}",
        )


class TestStarterPersonasSubstantive(unittest.TestCase):
    """Each persona file is non-empty and substantively long."""

    def _check(self, name: str) -> None:
        text = _read(name)
        self.assertGreater(
            len(text),
            _MIN_CHARS,
            f"{name} must be substantive (>{_MIN_CHARS} chars), got {len(text)}",
        )

    def test_coherence_reviewer_substantive(self):
        self._check("coherence-reviewer.md")

    def test_feasibility_reviewer_substantive(self):
        self._check("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_substantive(self):
        self._check("scope-guardian-reviewer.md")

    def test_product_reviewer_substantive(self):
        self._check("product-reviewer.md")

    def test_adversarial_document_reviewer_substantive(self):
        self._check("adversarial-document-reviewer.md")


class TestStarterPersonasNoToolsFraming(unittest.TestCase):
    """Each persona carries the no-tools / pasted-content framing (R12)."""

    def _check(self, name: str) -> None:
        text = _read(name)
        self.assertIn(
            _NO_TOOLS_PHRASE,
            text,
            f"{name} must contain the no-tools declaration (missing {_NO_TOOLS_PHRASE!r})",
        )

    def test_coherence_reviewer_no_tools(self):
        self._check("coherence-reviewer.md")

    def test_feasibility_reviewer_no_tools(self):
        self._check("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_no_tools(self):
        self._check("scope-guardian-reviewer.md")

    def test_product_reviewer_no_tools(self):
        self._check("product-reviewer.md")

    def test_adversarial_document_reviewer_no_tools(self):
        self._check("adversarial-document-reviewer.md")


class TestStarterPersonasGroundingPhrases(unittest.TestCase):
    """Each persona carries the grounding-control phrases (R12): cite + no finding."""

    def _check(self, name: str) -> None:
        text = _read(name).lower()
        self.assertIn(
            _CITE_PHRASE,
            text,
            f"{name} must contain the cite grounding phrase (missing {_CITE_PHRASE!r})",
        )
        self.assertIn(
            _NO_FINDING_PHRASE,
            text,
            f"{name} must contain the 'no finding' blessing (missing {_NO_FINDING_PHRASE!r})",
        )

    def test_coherence_reviewer_grounding(self):
        self._check("coherence-reviewer.md")

    def test_feasibility_reviewer_grounding(self):
        self._check("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_grounding(self):
        self._check("scope-guardian-reviewer.md")

    def test_product_reviewer_grounding(self):
        self._check("product-reviewer.md")

    def test_adversarial_document_reviewer_grounding(self):
        self._check("adversarial-document-reviewer.md")


class TestStarterPersonasNoModelBinding(unittest.TestCase):
    """Each persona contains no provider/model binding (R1)."""

    def _check_no_frontmatter(self, name: str) -> None:
        """No YAML frontmatter block (no leading '---' line)."""
        text = _read(name)
        first_line = text.lstrip("\n").split("\n")[0]
        self.assertNotEqual(
            first_line.strip(),
            "---",
            f"{name} must not start with YAML frontmatter ('---')",
        )

    def _check_no_structural_keys(self, name: str) -> None:
        """No bare 'model:' or 'tools:' YAML keys."""
        text = _read(name)
        for token in _BANNED_STRUCTURAL:
            # Match as a line-starting key (optional whitespace, then the key).
            pattern = r"^\s*" + re.escape(token)
            matches = re.findall(pattern, text, re.MULTILINE)
            self.assertEqual(
                matches,
                [],
                f"{name} must not contain the YAML key {token!r} (found {matches})",
            )

    def _check_no_model_tokens(self, name: str) -> None:
        """No model/provider binding (words on \\b boundaries; unambiguous slugs as substrings)."""
        text = _read(name).lower()
        for word in _BANNED_MODEL_WORDS:
            self.assertIsNone(
                re.search(r"\b" + re.escape(word) + r"\b", text),
                f"{name} must not contain the model token {word!r} as a word",
            )
        for slug in _BANNED_MODEL_SLUGS:
            self.assertNotIn(
                slug, text, f"{name} must not contain the provider/model slug {slug!r}"
            )

    def test_coherence_reviewer_no_binding(self):
        self._check_no_frontmatter("coherence-reviewer.md")
        self._check_no_structural_keys("coherence-reviewer.md")
        self._check_no_model_tokens("coherence-reviewer.md")

    def test_feasibility_reviewer_no_binding(self):
        self._check_no_frontmatter("feasibility-reviewer.md")
        self._check_no_structural_keys("feasibility-reviewer.md")
        self._check_no_model_tokens("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_no_binding(self):
        self._check_no_frontmatter("scope-guardian-reviewer.md")
        self._check_no_structural_keys("scope-guardian-reviewer.md")
        self._check_no_model_tokens("scope-guardian-reviewer.md")

    def test_product_reviewer_no_binding(self):
        self._check_no_frontmatter("product-reviewer.md")
        self._check_no_structural_keys("product-reviewer.md")
        self._check_no_model_tokens("product-reviewer.md")

    def test_adversarial_document_reviewer_no_binding(self):
        self._check_no_frontmatter("adversarial-document-reviewer.md")
        self._check_no_structural_keys("adversarial-document-reviewer.md")
        self._check_no_model_tokens("adversarial-document-reviewer.md")


class TestStarterPersonasNoOrchestration(unittest.TestCase):
    """Each persona contains no dangling ce-orchestration strings (R11)."""

    def _check(self, name: str) -> None:
        text = _read(name)
        for phrase in _BANNED_ORCHESTRATION:
            self.assertNotIn(
                phrase,
                text,
                f"{name} must not contain ce-orchestration string {phrase!r}",
            )

    def test_coherence_reviewer_no_orchestration(self):
        self._check("coherence-reviewer.md")

    def test_feasibility_reviewer_no_orchestration(self):
        self._check("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_no_orchestration(self):
        self._check("scope-guardian-reviewer.md")

    def test_product_reviewer_no_orchestration(self):
        self._check("product-reviewer.md")

    def test_adversarial_document_reviewer_no_orchestration(self):
        self._check("adversarial-document-reviewer.md")


class TestStarterPersonasNoInvisibleUnicode(unittest.TestCase):
    """Each persona contains no zero-width or BOM characters (R11)."""

    def _check(self, name: str) -> None:
        text = _read(name)
        for char in _BANNED_UNICODE:
            self.assertNotIn(
                char,
                text,
                f"{name} must not contain invisible Unicode U+{ord(char):04X}",
            )

    def test_coherence_reviewer_no_invisible_unicode(self):
        self._check("coherence-reviewer.md")

    def test_feasibility_reviewer_no_invisible_unicode(self):
        self._check("feasibility-reviewer.md")

    def test_scope_guardian_reviewer_no_invisible_unicode(self):
        self._check("scope-guardian-reviewer.md")

    def test_product_reviewer_no_invisible_unicode(self):
        self._check("product-reviewer.md")

    def test_adversarial_document_reviewer_no_invisible_unicode(self):
        self._check("adversarial-document-reviewer.md")


if __name__ == "__main__":
    unittest.main()
