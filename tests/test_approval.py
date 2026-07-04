"""Tests for fleet_engine/approval.py.

U1 — surface_digest: a pure function binding the full previewed run surface
(the whole resolved FleetConfig, the composed task, and the resolved profile
string) into one stable sha256 hex digest.

U2 — the approval token store: an owner-only, symlink-guarded, one-shot
write/consume of the digest.

Every write/consume test passes an explicit path=<tempfile> so these NEVER
touch the real ~/.cadre.
"""

import copy
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from fleet_engine.approval import (
    DEFAULT_APPROVAL_PATH,
    ApprovalToken,
    consume_approval,
    default_approval_path,
    surface_digest,
    write_approval,
)
from fleet_engine.config import FleetConfig


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    *,
    model: str = "m",
    toolset: list[str] | None = None,
    synthesis_prompt: str | None = None,
    allow_privileged_tools: bool = False,
) -> FleetConfig:
    """A minimal, valid collect-mode FleetConfig for digest tests.

    Collect mode keeps the fixture simple (no synthesis block is required),
    while config.py still permits an OPTIONAL synthesis block in collect mode
    — used here to exercise the synthesis-prompt axis without switching
    convergence modes.
    """
    data = {
        "name": "t",
        "convergence": "collect",
        "allow_privileged_tools": allow_privileged_tools,
        "specialists": [
            {
                "role": "a",
                "provider": "p",
                "model": model,
                "focus": "f",
                "toolset": toolset if toolset is not None else ["web"],
            }
        ],
    }
    if synthesis_prompt is not None:
        data["synthesis"] = {"provider": "sp", "model": "sm", "prompt": synthesis_prompt}
    return FleetConfig.from_dict(data)


# ---------------------------------------------------------------------------
# U1 — surface_digest
# ---------------------------------------------------------------------------


class TestSurfaceDigestDeterminism(unittest.TestCase):
    def test_same_inputs_same_digest(self):
        """Two independent computations of the same (cfg, task, profile) agree."""
        cfg = _make_cfg()
        d1 = surface_digest(cfg, "do the thing", "default")
        d2 = surface_digest(cfg, "do the thing", "default")
        self.assertEqual(d1, d2)

    def test_key_order_independence(self):
        """Two independently-built, value-equal configs digest identically —
        trusts json.dumps(sort_keys=True) rather than depending on any
        particular dict/field insertion order."""
        cfg1 = _make_cfg()
        cfg2 = copy.deepcopy(cfg1)
        self.assertEqual(
            surface_digest(cfg1, "task", "profile"),
            surface_digest(cfg2, "task", "profile"),
        )

    def test_returns_64_char_hex_string(self):
        digest = surface_digest(_make_cfg(), "task", "profile")
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises ValueError if not valid hex


class TestSurfaceDigestAxesFlipIndependently(unittest.TestCase):
    """Each element of the previewed surface changes the digest on its own —
    AE1 (config-swap refused) and AE7 (input-swap refused) both rest on this."""

    def test_specialist_model_change_flips_digest(self):
        base = surface_digest(_make_cfg(model="m1"), "task", "profile")
        changed = surface_digest(_make_cfg(model="m2"), "task", "profile")
        self.assertNotEqual(base, changed)

    def test_specialist_toolset_change_flips_digest(self):
        base = surface_digest(_make_cfg(toolset=["web"]), "task", "profile")
        changed = surface_digest(_make_cfg(toolset=["search"]), "task", "profile")
        self.assertNotEqual(base, changed)

    def test_synthesis_prompt_change_flips_digest(self):
        base = surface_digest(_make_cfg(synthesis_prompt="prompt A"), "task", "profile")
        changed = surface_digest(_make_cfg(synthesis_prompt="prompt B"), "task", "profile")
        self.assertNotEqual(base, changed)

    def test_allow_privileged_tools_flip_changes_digest(self):
        base = surface_digest(_make_cfg(allow_privileged_tools=False), "task", "profile")
        changed = surface_digest(_make_cfg(allow_privileged_tools=True), "task", "profile")
        self.assertNotEqual(base, changed)

    def test_persona_effective_instruction_change_flips_digest(self):
        """surface_digest binds effective_instruction CONTENT regardless of
        whether it arrived via a focus-only spec or a resolved persona file —
        it never calls resolve() itself; it digests whatever cfg it is handed."""
        cfg1 = _make_cfg()
        base = surface_digest(cfg1, "task", "profile")

        cfg2 = copy.deepcopy(cfg1)
        cfg2.specialists[0].effective_instruction = "a different resolved persona body"
        changed = surface_digest(cfg2, "task", "profile")
        self.assertNotEqual(base, changed)

    def test_composed_task_change_flips_digest(self):
        """The critical axis: a naive config-only digest would leave the whole
        task surface (which carries --task + --doc content) unbound."""
        cfg = _make_cfg()
        base = surface_digest(cfg, "original task", "profile")
        changed = surface_digest(cfg, "a completely different task", "profile")
        self.assertNotEqual(base, changed)

    def test_composed_task_with_embedded_nul_byte_is_bound(self):
        """A composed task may legally contain a NUL byte (valid UTF-8, survives
        the --doc decoder) — the JSON-structure approach binds it cleanly,
        with no delimiter-confusion class of bug a raw-concatenation digest
        would risk."""
        cfg = _make_cfg()
        base = surface_digest(cfg, "task-without-nul", "profile")
        with_nul = surface_digest(cfg, "task-with\x00-a-nul-byte", "profile")
        self.assertNotEqual(base, with_nul)

    def test_profile_change_flips_digest(self):
        cfg = _make_cfg()
        base = surface_digest(cfg, "task", "default")
        changed = surface_digest(cfg, "task", "other-profile")
        self.assertNotEqual(base, changed)


# ---------------------------------------------------------------------------
# U2 — default_approval_path
# ---------------------------------------------------------------------------


class TestDefaultApprovalPath(unittest.TestCase):
    """default_approval_path() returns CADRE_APPROVAL_PATH env var first, then
    DEFAULT_APPROVAL_PATH — mirrors personas.default_pool_dir's contract."""

    def test_no_env_returns_default(self):
        env_without = {k: v for k, v in os.environ.items() if k != "CADRE_APPROVAL_PATH"}
        with patch.dict(os.environ, env_without, clear=True):
            result = default_approval_path()
        self.assertEqual(result, DEFAULT_APPROVAL_PATH)

    def test_env_override_returned(self):
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": "/custom/approval"}):
            result = default_approval_path()
        self.assertEqual(result, "/custom/approval")

    def test_empty_env_falls_through_to_default(self):
        with patch.dict(os.environ, {"CADRE_APPROVAL_PATH": ""}):
            result = default_approval_path()
        self.assertEqual(result, DEFAULT_APPROVAL_PATH)


# ---------------------------------------------------------------------------
# U2 — write_approval / consume_approval round-trip
# ---------------------------------------------------------------------------


class TestApprovalTokenRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "approval")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_consume_round_trips_digest_and_privileged(self):
        write_approval("abc123", privileged=False, path=self.path)
        token = consume_approval(path=self.path)
        self.assertIsNotNone(token)
        self.assertEqual(token.digest, "abc123")
        self.assertFalse(token.privileged)

    def test_privileged_flag_round_trips_true(self):
        write_approval("abc123", privileged=True, path=self.path)
        token = consume_approval(path=self.path)
        self.assertIsNotNone(token)
        self.assertTrue(token.privileged)

    def test_second_consume_returns_none(self):
        """Atomic one-shot: the file is gone after the first consume."""
        write_approval("abc123", privileged=False, path=self.path)
        first = consume_approval(path=self.path)
        self.assertIsNotNone(first)
        second = consume_approval(path=self.path)
        self.assertIsNone(second)

    def test_missing_parent_dir_is_created(self):
        nested = os.path.join(self._tmp.name, "does", "not", "exist", "approval")
        write_approval("abc123", privileged=False, path=nested)
        self.assertTrue(os.path.exists(nested))
        token = consume_approval(path=nested)
        self.assertIsNotNone(token)
        self.assertEqual(token.digest, "abc123")

    def test_written_file_mode_is_0600(self):
        write_approval("abc123", privileged=False, path=self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_consume_missing_file_returns_none(self):
        missing = os.path.join(self._tmp.name, "nope")
        self.assertIsNone(consume_approval(path=missing))


class TestApprovalTokenSymlinkGuard(unittest.TestCase):
    """R10-style posture: a symlinked token path is refused on both write and
    read, never followed (mirrors the Pass-1 seed-dir symlink guard)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.token_path = os.path.join(self._tmp.name, "approval")
        self.target_path = os.path.join(self._tmp.name, "target")
        with open(self.target_path, "w", encoding="utf-8") as f:
            f.write("not a token")
        os.symlink(self.target_path, self.token_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_refuses_symlinked_path(self):
        with self.assertRaises(OSError):
            write_approval("abc123", privileged=False, path=self.token_path)
        # The symlink target must be untouched — O_NOFOLLOW refused the open
        # before any write occurred.
        with open(self.target_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "not a token")

    def test_consume_refuses_symlinked_path(self):
        self.assertIsNone(consume_approval(path=self.token_path))
        # Refusal, not deletion — the symlink itself must still be there.
        self.assertTrue(os.path.islink(self.token_path))


class TestApprovalTokenMalformedContent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "approval")

    def tearDown(self):
        self._tmp.cleanup()

    def test_garbage_bytes_return_none(self):
        with open(self.path, "wb") as f:
            f.write(b"\xff\xfe not json garbage \x80\x81\x82")
        self.assertIsNone(consume_approval(path=self.path))

    def test_empty_file_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertIsNone(consume_approval(path=self.path))

    def test_valid_json_missing_fields_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"unrelated": "shape"}, f)
        self.assertIsNone(consume_approval(path=self.path))

    def test_wrong_typed_fields_return_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"digest": 12345, "privileged": "yes"}, f)
        self.assertIsNone(consume_approval(path=self.path))


class TestApprovalTokenExpiry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "approval")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ttl_none_never_expires(self):
        token = ApprovalToken(digest="d", privileged=False, minted_at=1000.0, ttl_seconds=None)
        self.assertFalse(token.is_expired(10_000_000.0))

    def test_expired_when_minted_at_missing_but_ttl_set(self):
        """Defensive branch: a hand-built or corrupted-but-well-typed token can
        carry ttl_seconds with no minted_at (write_approval itself never
        produces this pairing — it always sets minted_at whenever ttl_seconds
        is given). is_expired treats a missing mint time as maximally stale
        (epoch 0) rather than raising, so a malformed token reads as expired —
        fail-closed — instead of crashing the caller (skills/cadre-fleet/run.py)
        with an uncaught TypeError on `None + ttl_seconds`."""
        token = ApprovalToken(digest="d", privileged=False, ttl_seconds=60)
        self.assertTrue(token.is_expired(1000.0))

    def test_expired_after_ttl_boundary(self):
        write_approval("d", privileged=False, ttl_seconds=60, now=1000.0, path=self.path)
        token = consume_approval(path=self.path)
        self.assertIsNotNone(token)
        self.assertFalse(token.is_expired(1000.0))
        self.assertFalse(token.is_expired(1060.0))  # exactly at the boundary: strict `>`
        self.assertTrue(token.is_expired(1100.0))

    def test_minted_at_defaults_to_injected_now(self):
        write_approval("d", privileged=False, now=500.0, path=self.path)
        token = consume_approval(path=self.path)
        self.assertIsNotNone(token)
        self.assertEqual(token.minted_at, 500.0)


class TestApprovalTokenParentDirPermissions(unittest.TestCase):
    """F2 (Codex adversarial review): the token has no MAC, so its integrity rests
    on the parent directory being owner-owned and not group/other-writable. A loose
    parent lets a co-resident replant the token even though the leaf is 0o600 +
    O_NOFOLLOW. write_approval REFUSES to mint into a loose dir (raises); consume
    REFUSES to honor a token from one (returns None — the actual exploit path).
    Mirrors the persona-pool ownership/permission check in personas.resolve."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _dir(self, name, mode):
        # chmod AFTER mkdir — chmod is umask-independent, unlike mkdir's mode arg.
        d = os.path.join(self.root, name)
        os.mkdir(d)
        os.chmod(d, mode)
        return d

    def test_group_writable_parent_write_refuses(self):
        d = self._dir("g", 0o770)
        with self.assertRaises(PermissionError):
            write_approval("d", privileged=False, path=os.path.join(d, "approval"))

    def test_other_writable_parent_write_refuses(self):
        d = self._dir("o", 0o707)
        with self.assertRaises(PermissionError):
            write_approval("d", privileged=False, path=os.path.join(d, "approval"))

    def test_loose_parent_consume_refuses(self):
        # Mint while the dir is safe, then loosen it: consume must refuse to honor
        # the token (fail-closed) even though the 0o600 leaf is untouched.
        d = self._dir("c", 0o700)
        tok = os.path.join(d, "approval")
        write_approval("digest-x", privileged=False, path=tok)
        os.chmod(d, 0o777)
        self.assertIsNone(consume_approval(path=tok))

    def test_safe_parent_round_trips(self):
        # A 0o700 owner-owned parent works normally — no false refusal.
        d = self._dir("s", 0o700)
        tok = os.path.join(d, "approval")
        write_approval("digest-y", privileged=False, path=tok)
        token = consume_approval(path=tok)
        self.assertIsNotNone(token)
        self.assertEqual(token.digest, "digest-y")


if __name__ == "__main__":
    unittest.main()
