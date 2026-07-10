"""Tests for cadre/policy.py — the #78 local allow/deny policy loader + matcher.

All tests use tempfile paths or in-memory Policy construction; the real
~/.cadre is NEVER touched (load_policy always receives an explicit path).

Fixtures use neutral, non-real strings throughout (prov-a, prov-b, model-x,
example-family-*, ...) — never a real provider/model name, matching this
repo's hard rule against real provider strings in fixtures.

Test-first: these tests define the contract; the implementation must satisfy them.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cadre.policy as policy_mod
from cadre.policy import (
    DEFAULT_POLICY_PATH,
    Policy,
    PolicyError,
    Violation,
    default_policy_path,
    filter_pairs,
    load_policy,
)


class _PolicyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(_rmtree_writable, self.tmp)

    def _write(self, content: str, name: str = "policy.yaml") -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path


def _rmtree_writable(tmp: Path) -> None:
    """Restore permissions before cleanup so shutil.rmtree can always delete
    (a test below deliberately locks a file/dir down)."""
    for root, dirs, files in os.walk(tmp):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                pass
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tri-state load: absent -> permissive
# ---------------------------------------------------------------------------


class TestLoadPolicyAbsent(_PolicyTestBase):
    def test_missing_file_returns_permissive(self):
        missing = self.tmp / "does-not-exist.yaml"
        pol = load_policy(missing)
        self.assertEqual(pol.source, "absent")
        self.assertEqual(pol.deny_providers, frozenset())
        self.assertEqual(pol.restrict_models, ())

    def test_permissive_blocks_nothing(self):
        pol = Policy.permissive()
        self.assertIsNone(pol.check("prov-a", "model-x"))
        self.assertIsNone(pol.check("anything", "anything"))

    def test_empty_file_returns_permissive(self):
        """An empty file (yaml.safe_load -> None) loads as permissive."""
        path = self._write("")
        pol = load_policy(path)
        self.assertEqual(pol.source, "absent")

    def test_fully_commented_file_returns_permissive(self):
        """A fully-commented file (yaml.safe_load -> None) — the seeded shape."""
        path = self._write("# just a comment\n# another comment\n")
        pol = load_policy(path)
        self.assertEqual(pol.source, "absent")


# ---------------------------------------------------------------------------
# Tri-state load: malformed -> PolicyError, fail closed
# ---------------------------------------------------------------------------


class TestLoadPolicyMalformed(_PolicyTestBase):
    def test_invalid_yaml_raises_policy_error(self):
        path = self._write("deny_providers: [unterminated\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_non_mapping_top_level_raises(self):
        for content in ("- a\n- b\n", "justascalar\n", "42\n"):
            path = self._write(content)
            with self.assertRaises(PolicyError):
                load_policy(path)

    def test_deny_providers_as_string_raises(self):
        """A scalar `deny_providers: prov-a` (not a list) must not be
        char-iterated into ['p','r','o',...] — it's a wrong shape, fail closed."""
        path = self._write("deny_providers: prov-a\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_deny_providers_with_non_string_entry_raises(self):
        path = self._write("deny_providers:\n  - 123\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_deny_providers_false_value_raises_not_silently_empty(self):
        """A wrong-shaped-but-falsy value must be caught by validation, not
        silently coerced to 'no rules' by an `or []` idiom."""
        path = self._write("deny_providers: false\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_not_a_list_raises(self):
        path = self._write("restrict_models: not-a-list\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_entry_not_a_mapping_raises(self):
        path = self._write("restrict_models:\n  - just-a-string\n")
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_missing_match_raises(self):
        path = self._write(
            "restrict_models:\n  - allowed_providers: [prov-a]\n"
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_missing_allowed_providers_raises(self):
        path = self._write(
            'restrict_models:\n  - match: "model-*"\n'
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_empty_allowed_providers_raises(self):
        path = self._write(
            'restrict_models:\n  - match: "model-*"\n    allowed_providers: []\n'
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_non_string_match_raises(self):
        path = self._write(
            "restrict_models:\n  - match: 42\n    allowed_providers: [prov-a]\n"
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_restrict_models_unrecognized_key_raises(self):
        path = self._write(
            'restrict_models:\n  - match: "model-*"\n    allowed_providers: [prov-a]\n'
            "    extra_key: oops\n"
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_unknown_top_level_key_raises(self):
        """Unknown keys are a misspelled rule until proven otherwise — fail closed."""
        path = self._write("deny_providrs:\n  - prov-a\n")  # typo'd key
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_unknown_top_level_key_alongside_valid_ones_raises(self):
        path = self._write(
            "deny_providers:\n  - prov-a\nallow_providers:\n  - prov-b\n"
        )
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_malformed_error_names_path_and_remedy(self):
        path = self._write("deny_providers: prov-a\n")
        with self.assertRaises(PolicyError) as ctx:
            load_policy(path)
        msg = str(ctx.exception)
        self.assertIn(str(path), msg)
        self.assertIn("Fix or remove", msg)

    def test_non_utf8_file_raises(self):
        path = self.tmp / "policy.yaml"
        path.write_bytes(b"\xff\xfe not utf-8 \x80")
        path.chmod(0o600)
        with self.assertRaises(PolicyError):
            load_policy(path)


# ---------------------------------------------------------------------------
# Rule matching: deny_providers
# ---------------------------------------------------------------------------


class TestPolicyCheckDenyProviders(unittest.TestCase):
    def test_denied_provider_blocked_regardless_of_model(self):
        pol = Policy(deny_providers=frozenset({"prov-a"}))
        v = pol.check("prov-a", "model-x")
        self.assertIsNotNone(v)
        self.assertEqual(v.provider, "prov-a")
        self.assertEqual(v.model, "model-x")

    def test_non_denied_provider_unaffected(self):
        pol = Policy(deny_providers=frozenset({"prov-a"}))
        self.assertIsNone(pol.check("prov-b", "model-x"))

    def test_violation_rule_text_names_deny_rule(self):
        pol = Policy(deny_providers=frozenset({"prov-a"}))
        v = pol.check("prov-a", "model-x")
        self.assertEqual(v.rule, "deny_providers: prov-a")

    def test_provider_slug_comparison_is_case_sensitive(self):
        pol = Policy(deny_providers=frozenset({"prov-a"}))
        self.assertIsNone(pol.check("Prov-A", "model-x"))


# ---------------------------------------------------------------------------
# Rule matching: restrict_models
# ---------------------------------------------------------------------------


class TestPolicyCheckRestrictModels(unittest.TestCase):
    def _policy(self, match="example-family-*", allowed=("prov-b",)):
        return Policy(
            restrict_models=(
                policy_mod._RestrictRule(match=match, allowed_providers=tuple(allowed)),
            )
        )

    def test_allowed_provider_for_matching_model_passes(self):
        pol = self._policy()
        self.assertIsNone(pol.check("prov-b", "example-family-large"))

    def test_disallowed_provider_for_matching_model_blocked(self):
        pol = self._policy()
        v = pol.check("prov-a", "example-family-large")
        self.assertIsNotNone(v)
        self.assertEqual(v.provider, "prov-a")
        self.assertEqual(v.model, "example-family-large")

    def test_violation_rule_text_names_restrict_rule_and_allowed_list(self):
        pol = self._policy(match="example-family-*", allowed=("prov-b",))
        v = pol.check("prov-a", "example-family-large")
        self.assertEqual(v.rule, "restrict_models: 'example-family-*' allows [prov-b]")

    def test_non_matching_model_unaffected(self):
        """A model that matches no restrict_models rule is untouched by it."""
        pol = self._policy(match="example-family-*", allowed=("prov-b",))
        self.assertIsNone(pol.check("prov-a", "totally-different-model"))

    def test_exact_name_pattern_matches_only_exact_name(self):
        pol = self._policy(match="exact-model-name", allowed=("prov-b",))
        self.assertIsNone(pol.check("prov-b", "exact-model-name"))
        self.assertIsNone(pol.check("prov-a", "exact-model-name-but-longer"))

    def test_glob_star_suffix_matches_family(self):
        pol = self._policy(match="fam-*", allowed=("prov-b",))
        self.assertIsNone(pol.check("prov-b", "fam-large"))
        self.assertIsNone(pol.check("prov-b", "fam-small"))
        v = pol.check("prov-a", "fam-large")
        self.assertIsNotNone(v)

    def test_model_id_comparison_is_case_sensitive(self):
        pol = self._policy(match="Fam-*", allowed=("prov-b",))
        self.assertIsNone(pol.check("prov-a", "fam-large"))  # does not match "Fam-*"

    def test_multiple_allowed_providers(self):
        pol = self._policy(match="fam-*", allowed=("prov-a", "prov-b"))
        self.assertIsNone(pol.check("prov-a", "fam-x"))
        self.assertIsNone(pol.check("prov-b", "fam-x"))
        self.assertIsNotNone(pol.check("prov-c", "fam-x"))

    def test_first_matching_rule_wins(self):
        """File-order precedence: the first rule whose glob matches decides."""
        pol = Policy(
            restrict_models=(
                policy_mod._RestrictRule(match="fam-*", allowed_providers=("prov-a",)),
                policy_mod._RestrictRule(match="fam-special", allowed_providers=("prov-b",)),
            )
        )
        # "fam-special" matches BOTH rules; the first (broader) rule wins.
        self.assertIsNone(pol.check("prov-a", "fam-special"))
        v = pol.check("prov-b", "fam-special")
        self.assertIsNotNone(v)


class TestPolicyCheckDenyBeatsRestrict(unittest.TestCase):
    def test_deny_wins_even_if_restrict_would_allow(self):
        """deny_providers is an absolute veto, checked before restrict_models."""
        pol = Policy(
            deny_providers=frozenset({"prov-a"}),
            restrict_models=(
                policy_mod._RestrictRule(match="fam-*", allowed_providers=("prov-a",)),
            ),
        )
        v = pol.check("prov-a", "fam-x")
        self.assertIsNotNone(v)
        self.assertEqual(v.rule, "deny_providers: prov-a")


# ---------------------------------------------------------------------------
# filter_pairs — pure, order-preserving
# ---------------------------------------------------------------------------


class TestFilterPairs(unittest.TestCase):
    def test_splits_allowed_and_violations(self):
        pol = Policy(deny_providers=frozenset({"prov-a"}))
        allowed, violations = filter_pairs(
            [("prov-a", "model-x"), ("prov-b", "model-y")], pol
        )
        self.assertEqual(allowed, [("prov-b", "model-y")])
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].provider, "prov-a")

    def test_order_preserved(self):
        pol = Policy.permissive()
        pairs = [("prov-c", "m1"), ("prov-a", "m2"), ("prov-b", "m3")]
        allowed, violations = filter_pairs(pairs, pol)
        self.assertEqual(allowed, pairs)
        self.assertEqual(violations, [])

    def test_empty_input_returns_empty(self):
        allowed, violations = filter_pairs([], Policy.permissive())
        self.assertEqual(allowed, [])
        self.assertEqual(violations, [])

    def test_all_violations_returns_empty_allowed(self):
        pol = Policy(deny_providers=frozenset({"prov-a", "prov-b"}))
        allowed, violations = filter_pairs(
            [("prov-a", "m1"), ("prov-b", "m2")], pol
        )
        self.assertEqual(allowed, [])
        self.assertEqual(len(violations), 2)


# ---------------------------------------------------------------------------
# Violation dataclass shape
# ---------------------------------------------------------------------------


class TestViolationShape(unittest.TestCase):
    def test_fields(self):
        v = Violation(provider="prov-a", model="model-x", rule="deny_providers: prov-a")
        self.assertEqual(v.provider, "prov-a")
        self.assertEqual(v.model, "model-x")
        self.assertEqual(v.rule, "deny_providers: prov-a")


# ---------------------------------------------------------------------------
# File posture: symlink refusal, ownership, permissions
# ---------------------------------------------------------------------------


class TestLoadPolicyFilePosture(_PolicyTestBase):
    def test_symlinked_policy_file_refused(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("deny_providers: [prov-a]\n", encoding="utf-8")
        link = self.tmp / "policy.yaml"
        link.symlink_to(sentinel)
        with self.assertRaises(PolicyError):
            load_policy(link)

    def test_symlinked_policy_file_error_names_remedy(self):
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("deny_providers: [prov-a]\n", encoding="utf-8")
        link = self.tmp / "policy.yaml"
        link.symlink_to(sentinel)
        with self.assertRaises(PolicyError) as ctx:
            load_policy(link)
        self.assertIn("fix or remove", str(ctx.exception).lower())

    def test_0600_owner_posture_round_trips(self):
        """A file written under this repo's canonical owner-only posture
        (0600, owned by the current process) loads successfully — the
        expected posture is honored on read, not just accepted incidentally."""
        path = self._write("deny_providers:\n  - prov-a\n")
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        pol = load_policy(path)
        self.assertEqual(pol.source, "file")
        self.assertIn("prov-a", pol.deny_providers)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only ownership check")
    def test_group_writable_file_refused(self):
        path = self._write("deny_providers:\n  - prov-a\n")
        path.chmod(0o660)  # group-readable AND group-writable
        with self.assertRaises(PolicyError):
            load_policy(path)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only ownership check")
    def test_other_writable_file_refused(self):
        path = self._write("deny_providers:\n  - prov-a\n")
        path.chmod(0o606)
        with self.assertRaises(PolicyError):
            load_policy(path)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only ownership check")
    def test_group_readable_only_is_fine(self):
        """Group/other READ (no write bit) is not the guarded condition —
        only group/other WRITE is refused (mirrors approval._parent_is_safe's
        0o022 mask)."""
        path = self._write("deny_providers:\n  - prov-a\n")
        path.chmod(0o644)
        pol = load_policy(path)
        self.assertEqual(pol.source, "file")

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only ownership check")
    def test_foreign_owned_file_refused(self):
        """A file owned by a different uid is refused even with safe mode
        bits. Mocks os.getuid (rather than chown, which needs root) to
        simulate a foreign owner on a file this test process actually owns —
        mirrors tests/test_install_skill.py's identical technique."""
        path = self._write("deny_providers:\n  - prov-a\n")
        with patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PolicyError) as ctx:
                load_policy(path)
        self.assertIn("not owned by you", str(ctx.exception))

    def test_unreadable_file_raises_policy_error_not_traceback(self):
        if hasattr(os, "getuid") and os.getuid() == 0:
            self.skipTest("root can read 0o000 files")
        path = self._write("deny_providers:\n  - prov-a\n")
        path.chmod(0o000)
        with self.assertRaises(PolicyError):
            load_policy(path)

    def test_directory_at_policy_path_raises_policy_error(self):
        d = self.tmp / "policy.yaml"
        d.mkdir()
        with self.assertRaises(PolicyError):
            load_policy(d)


# ---------------------------------------------------------------------------
# default_policy_path — CADRE_POLICY env override
# ---------------------------------------------------------------------------


class TestDefaultPolicyPath(unittest.TestCase):
    def test_no_env_returns_default(self):
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_POLICY"}
        with patch.dict(os.environ, env_without, clear=True):
            self.assertEqual(default_policy_path(), DEFAULT_POLICY_PATH)

    def test_env_override_returned(self):
        with patch.dict(os.environ, {"CADRE_POLICY": "/custom/policy.yaml"}):
            self.assertEqual(default_policy_path(), "/custom/policy.yaml")

    def test_empty_env_falls_through_to_default(self):
        with patch.dict(os.environ, {"CADRE_POLICY": ""}):
            self.assertEqual(default_policy_path(), DEFAULT_POLICY_PATH)


# ---------------------------------------------------------------------------
# SEED_TEMPLATE — the generic commented default cadre/provision.py seeds
# ---------------------------------------------------------------------------


class TestSeedTemplate(unittest.TestCase):
    def test_seed_template_parses_as_permissive(self):
        pol = policy_mod.Policy.permissive()
        # Round-trip through the real loader via a temp file, so this proves
        # the ACTUAL seeded bytes parse permissively, not just by inspection.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "policy.yaml"
        path.write_text(policy_mod.SEED_TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
        loaded = load_policy(path)
        self.assertEqual(loaded.source, "absent")
        self.assertEqual(loaded.deny_providers, pol.deny_providers)
        self.assertEqual(loaded.restrict_models, pol.restrict_models)

    def test_seed_template_contains_no_real_provider_or_model_strings(self):
        """No anthropic/*, no real provider or model slug — generic examples only."""
        lowered = policy_mod.SEED_TEMPLATE.lower()
        for banned in ("anthropic", "claude", "openai", "xai", "grok", "openrouter", "copilot"):
            self.assertNotIn(banned, lowered)

    def test_seed_template_is_fully_commented(self):
        """Every non-blank line starts with '#' — a stray uncommented line
        would silently activate a rule for the operator."""
        for line in policy_mod.SEED_TEMPLATE.splitlines():
            if line.strip():
                self.assertTrue(line.startswith("#"), f"non-comment line: {line!r}")


if __name__ == "__main__":
    unittest.main()
