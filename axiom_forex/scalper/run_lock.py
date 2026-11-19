"""A process-lifetime exclusive owner lock for one live scalper run.

The file is retained; the OS releases its advisory lock when a process exits,
including after a crash. It is never treated as evidence about broker state.
"""
from __future__ import annotations

from os import SEEK_END, name as os_name
from pathlib import Path
from typing import BinaryIO


class RunLockError(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: BinaryIO | None = None
        try:
            stream = self.path.open("a+b")
            self._file = stream
            if os_name == "nt":
                import msvcrt
                stream.seek(0, SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise RunLockError("live_run_owned_by_another_process") from exc

    def close(self) -> None:
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            if os_name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
