"""Small cross-process guards shared by ingestion and retention (Unix hosts)."""
from contextlib import contextmanager
import fcntl
import os


@contextmanager
def file_lock(path, blocking=True):
    with open(path, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def disk_usage(path):
    v = os.statvfs(path)
    used = (v.f_blocks-v.f_bfree)*v.f_frsize
    available = v.f_bavail*v.f_frsize
    # Same denominator as df: reserved blocks are not available capacity.
    return dict(used_bytes=used, available_bytes=available,
                used_pct=100*used/(used+available) if used+available else 100)
