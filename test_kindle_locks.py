"""Real Linux OFD regression fixtures; never run complete device scripts."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent
SH = os.environ.get("KINDLE_TEST_SH") or shutil.which("sh")


@unittest.skipUnless(
    sys.platform.startswith("linux") and SH and shutil.which("flock"),
    "Requires Linux /proc and real flock; Windows/Git sh cannot validate OFD locks",
)
class KindleLockTests(unittest.TestCase):
    def test_real_concurrent_lifecycle_and_resource_locks(self):
        generated = subprocess.run(
            [SH, "tests/kindle-lock-bundle.sh"], cwd=ROOT,
            capture_output=True, timeout=20,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        result = subprocess.run(
            [SH], input=generated.stdout, cwd=ROOT,
            capture_output=True, timeout=1020,
        )
        output = (result.stdout + result.stderr).decode(errors="replace")
        if result.returncode == 77:
            self.skipTest(output.strip())
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("PASS all real Linux lock regressions", output)


if __name__ == "__main__":
    unittest.main()
