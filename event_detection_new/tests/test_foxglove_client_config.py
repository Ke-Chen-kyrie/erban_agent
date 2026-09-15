import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FoxgloveClientConfigTest(unittest.TestCase):
    def test_import_uses_environment_without_legacy_config_module(self):
        env = os.environ.copy()
        env["FOXGLOVE_BRIDGE_URL"] = "ws://test-host:9876"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import foxglove_client; print(foxglove_client.FOXGLOVE_BRIDGE_URL)",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ws://test-host:9876")


if __name__ == "__main__":
    unittest.main()
