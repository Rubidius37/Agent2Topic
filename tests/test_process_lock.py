import os
import subprocess
import sys
import tempfile
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from process_lock import (  # noqa: E402
    LOCK_BUSY_EXIT_CODE,
    VaultProcessLock,
    delegated_lock_environment,
)


LOCK_CHILD = r"""
import sys
import time

sys.path.insert(0, sys.argv[1])
from process_lock import LOCK_BUSY_EXIT_CODE, ProcessLockUnavailable, VaultProcessLock

try:
    with VaultProcessLock(sys.argv[2], sys.argv[3], command="child-command"):
        print("LOCKED", flush=True)
        if len(sys.argv) > 4 and sys.argv[4] == "hold":
            time.sleep(30)
except ProcessLockUnavailable as exc:
    print(str(exc), flush=True)
    raise SystemExit(LOCK_BUSY_EXIT_CODE)
"""


DELEGATION_CHILD = r"""
import sys

sys.path.insert(0, sys.argv[1])
from process_lock import lock_is_delegated_by_parent

raise SystemExit(0 if lock_is_delegated_by_parent() else 1)
"""


class ProcessLockTests(unittest.TestCase):
    def run_lock_child(self, vault_dir, operation="child-operation", mode=None):
        command = [
            sys.executable,
            "-c",
            LOCK_CHILD,
            SCRIPTS_DIR,
            vault_dir,
            operation,
        ]
        if mode:
            command.append(mode)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )

    def test_second_process_is_blocked_and_can_retry_after_release(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            with VaultProcessLock(vault_dir, "parent-operation", command="parent-command"):
                blocked = self.run_lock_child(vault_dir)

            retried = self.run_lock_child(vault_dir)

        self.assertEqual(blocked.returncode, LOCK_BUSY_EXIT_CODE)
        self.assertIn("parent-operation", blocked.stdout)
        self.assertIn("parent-command", blocked.stdout)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("LOCKED", retried.stdout)

    def test_lock_is_released_when_holder_is_terminated(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    LOCK_CHILD,
                    SCRIPTS_DIR,
                    vault_dir,
                    "terminated-holder",
                    "hold",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
                holder.terminate()
                holder.communicate(timeout=10)
                recovered = self.run_lock_child(vault_dir)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=10)

        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)

    def test_child_can_use_lock_delegated_by_its_parent(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            with VaultProcessLock(vault_dir, "dashboard-save"):
                result = subprocess.run(
                    [sys.executable, "-c", DELEGATION_CHILD, SCRIPTS_DIR],
                    env=delegated_lock_environment(),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
