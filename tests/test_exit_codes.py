import unittest

from cadre.engine import FleetStatus
from cadre.exit_codes import ExitCode, status_to_exit


class TestExitCodeValues(unittest.TestCase):
    """Every code either runner can return lives in one enum (KTD3, #70)."""

    def test_member_values(self):
        self.assertEqual(ExitCode.SUCCESS, 0)
        self.assertEqual(ExitCode.ERROR, 1)
        self.assertEqual(ExitCode.USAGE, 2)
        self.assertEqual(ExitCode.DEGRADED, 3)
        self.assertEqual(ExitCode.FAILED, 4)
        self.assertEqual(ExitCode.PREFLIGHT_REFUSE, 5)

    def test_is_an_int_enum(self):
        # IntEnum: an ExitCode member IS a real int (argparse / sys.exit / a
        # tuple[int, str] return type all accept it directly).
        self.assertIsInstance(ExitCode.SUCCESS, int)


class TestStatusToExit(unittest.TestCase):
    """status_to_exit is the one mapping both runners call — SUCCESS/DEGRADED/FAILED
    to 0/3/4 — so neither can drift onto its own inline integer."""

    def test_success_maps_to_0(self):
        self.assertEqual(status_to_exit(FleetStatus.SUCCESS), 0)

    def test_degraded_maps_to_3(self):
        self.assertEqual(status_to_exit(FleetStatus.DEGRADED), 3)

    def test_failed_maps_to_4(self):
        self.assertEqual(status_to_exit(FleetStatus.FAILED), 4)

    def test_returns_an_int(self):
        self.assertIsInstance(status_to_exit(FleetStatus.SUCCESS), int)


if __name__ == "__main__":
    unittest.main()
