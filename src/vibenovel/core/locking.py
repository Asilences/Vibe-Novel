"""A process and thread lock for Studio writes (no optional dependencies)."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from vibenovel.errors import VibeNovelError


class ConflictError(VibeNovelError):
    """The caller must reload or resolve a pending storage transaction."""


_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@contextmanager
def project_lock(project, *, timeout: float = 0):
    if not isinstance(timeout, (float, int)) or not 0 <= timeout <= 30:
        raise ValueError("lock timeout requires 0–30 seconds")
    deadline = time.monotonic() + timeout
    path = project.path(".runtime/studio.lock")
    with _guard:
        lock = _locks.setdefault(str(path.resolve()).casefold(), threading.Lock())
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise ConflictError("作品正由另一项操作写入，请重试")
    stream = None
    acquired = False
    try:
        stream = path.open("a+b")
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConflictError("作品正由另一进程写入，请重试") from exc
                time.sleep(min(0.01, remaining))
        yield
    finally:
        if stream is not None:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        lock.release()
