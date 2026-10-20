"""Process-lifetime local instance locks without optional dependencies.

Lock files are retained: unlinking a locked file could let another process lock a
new inode. The operating system releases ownership if a process crashes. These
locks protect a shared data directory; they do not identify brokerage accounts
used from different data directories or detect older versions without the lock.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path


class InstanceInUseError(RuntimeError):
    """Another process already owns this application's data directory."""


class FileLock:
    """Nonblocking exclusive OS lock, held until release or process exit."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        if self.file is not None:
            return
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.fstat(descriptor).st_size == 0:
                handle.write(b"0")
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {32, 33}:
                raise InstanceInUseError("같은 데이터 폴더를 사용하는 프로그램이 이미 실행 중입니다. 기존 실행 창을 사용하거나 종료한 뒤 다시 실행하세요.") from None
            raise
        except BaseException:
            handle.close()
            raise
        self.file = handle

    def release(self):
        handle = self.file
        if handle is None:
            return
        self.file = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def project_identity(root) -> str:
    """Opaque checkout identity for local health/discovery; contains no paths."""
    canonical = os.path.normcase(str(Path(root).expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
