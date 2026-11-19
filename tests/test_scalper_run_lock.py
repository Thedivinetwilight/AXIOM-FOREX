from __future__ import annotations

import subprocess
import sys

import pytest

from axiom_forex.scalper.run_lock import RunLock, RunLockError


def test_exclusive_lock_blocks_second_process_and_is_released(tmp_path):
    path = tmp_path / "owner.lock"
    script = ("from axiom_forex.scalper.run_lock import RunLock,RunLockError\n"
              "import sys\n"
              "try:\n RunLock(sys.argv[1]).close()\n"
              "except RunLockError:\n sys.exit(3)\n")
    lock = RunLock(path)
    try:
        with pytest.raises(RunLockError):
            RunLock(path)
        blocked = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
        assert blocked.returncode == 3
    finally:
        lock.close()
    free = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
    assert free.returncode == 0 and path.exists()
