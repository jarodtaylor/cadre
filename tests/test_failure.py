import ast
import inspect
import unittest

import cadre.failure as failure_mod
from cadre.failure import FailureReason


class TestFailureReasonValues(unittest.TestCase):
    """Members + their lowercase string values (manifest-serialization stability)."""

    def test_members_have_lowercase_string_values(self):
        self.assertEqual(FailureReason.OFF_PALETTE.value, "off_palette")
        self.assertEqual(FailureReason.TIMEOUT.value, "timeout")
        self.assertEqual(FailureReason.SKIPPED.value, "skipped")
        self.assertEqual(FailureReason.EMPTY_OUTPUT.value, "empty_output")
        self.assertEqual(FailureReason.MODEL_ERROR.value, "model_error")

    def test_is_a_str_enum(self):
        # Value-equality to the bare string must hold (str, Enum) — the same
        # precedent as FleetStatus — even though identity (`is`) needs coercion.
        self.assertEqual(FailureReason.TIMEOUT, "timeout")
        self.assertIsInstance(FailureReason.TIMEOUT, str)


class TestFailureReasonCoercion(unittest.TestCase):
    """Boundary normalization: a raw string coerces; an enum member is idempotent;
    an unknown value fails fast (mirrors FleetStatus's normalize-at-the-boundary)."""

    def test_raw_string_coerces_to_member(self):
        self.assertIs(FailureReason("timeout"), FailureReason.TIMEOUT)

    def test_enum_member_is_idempotent(self):
        self.assertIs(FailureReason(FailureReason.TIMEOUT), FailureReason.TIMEOUT)

    def test_unknown_value_raises(self):
        with self.assertRaises(ValueError):
            FailureReason("bogus")


class TestFailureModuleIsALeaf(unittest.TestCase):
    """KTD1: cadre/failure.py must import nothing from cadre, so model_client,
    engine, preflight, capture, and render can all import from it with no cycle.
    """

    def test_imports_nothing_from_cadre(self):
        tree = ast.parse(inspect.getsource(failure_mod))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        cadre_imports = [name for name in imported if name == "cadre" or name.startswith("cadre.")]
        self.assertEqual(cadre_imports, [], "cadre/failure.py must not import from cadre (leaf module, KTD1)")


if __name__ == "__main__":
    unittest.main()
