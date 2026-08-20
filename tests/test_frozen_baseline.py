import hashlib
import unittest
from pathlib import Path


EXPECTED_SHA256 = "c827363a36ef5c52c61fcd61ea257d1aceab3c0025ddb61121c8b9050ed3e4b9"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FrozenBaselineTests(unittest.TestCase):
    def test_connect_vr921_matches_frozen_baseline(self):
        source = PROJECT_ROOT / "connect_vr921.py"
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(
            EXPECTED_SHA256,
            actual,
            "connect_vr921.py differs from the explicitly frozen working baseline",
        )


if __name__ == "__main__":
    unittest.main()
