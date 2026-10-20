"""Nonblocking, process-lifetime lock for cooperating novel writers.

The persistent empty file is a rendezvous point, never an ownership marker.
Do not unlink it: another writer could then lock a different file at the same
path. The OS releases the lock on close or process death, including hard kills.
This guards supported mutating novel CLI entry points, including production,
recovery, controlled trials, the maintained paid translation benchmark, source
extraction, glossary-conflict sidecars, checkpoint EPUB creation, normalization
report/application writers, and finalization/publication/editorial writers.
Read-only modes,
direct internal calls, external editors, and historical standalone scripts do
not participate.
"""

import errno
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import stat
import threading


LOCK_NAME = ".production.lock"
_owned = threading.local()


class ProductionRunLockedError(RuntimeError):
    """Another process already owns this novel's production lock."""


@dataclass(frozen=True, slots=True, eq=False)
class HeldProductionLock:
    """Process/thread-local lease, valid only inside its original OS lock scope.

    Construction/copying is not proof: verification requires object identity in
    the private active registry and the still-open original descriptor. This is
    cooperative ownership evidence, not protection against hostile Python code.
    """
    root: Path
    _handle: object
    _pid: int
    _thread: int
    _device: int
    _inode: int


def _leases():
    if not hasattr(_owned, "leases"):
        _owned.leases = {}
    return _owned.leases


def verify_held_production_lock(lease, novel_root):
    """Verify ownership without acquiring, opening or changing the rendezvous."""
    root = Path(novel_root).absolute()
    if ".." in root.parts:
        raise RuntimeError("Lock lease requires a non-traversing path")
    for path in (root, *root.parents):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise RuntimeError("Lock lease path must not contain symlinks/reparse points")
    root = root.resolve()
    if (type(lease) is not HeldProductionLock or lease.root != root
            or lease._pid != os.getpid() or lease._thread != threading.get_ident()
            or _leases().get(root) is not lease or lease._handle.closed):
        raise RuntimeError("Actual caller-held production lock lease required")
    opened = os.fstat(lease._handle.fileno())
    current = (root / LOCK_NAME).lstat()
    if (not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode)
            or (getattr(current, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            or (opened.st_dev, opened.st_ino) != (lease._device, lease._inode)
            or (current.st_dev, current.st_ino) != (lease._device, lease._inode)):
        raise RuntimeError("Production lock rendezvous identity changed")
    return lease


def held_production_run_lock(novel_root):
    """Obtain the actual caller's lease; a lock file's existence is insufficient."""
    root = Path(novel_root).absolute()
    return verify_held_production_lock(_leases().get(root.resolve()), root)


@contextmanager
def production_run_lock(novel_root):
    """Acquire before reading mutable runtime/approval state; never wait or steal."""
    path = Path(novel_root).resolve() / LOCK_NAME
    if path.is_symlink():
        raise RuntimeError(f"Production lock must not be a symlink: {path}")
    # Python opens non-inheritable descriptors. Child processes cannot keep the
    # lock alive after its owning process exits.
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ProductionRunLockedError(
                    f"Another supported novel writer owns {path.parent}. "
                    "Stop that run before retrying; do not delete the lock file."
                ) from exc
            raise
        opened = os.fstat(handle.fileno())
        lease = HeldProductionLock(path.parent, handle, os.getpid(),
            threading.get_ident(), opened.st_dev, opened.st_ino)
        _leases()[path.parent] = lease
        try:
            # Preserve the legacy yielded Path. Ownership lookup is separate.
            yield path
        finally:
            _leases().pop(path.parent, None)
        # Closing the descriptor releases the kernel lock on every exit path.
