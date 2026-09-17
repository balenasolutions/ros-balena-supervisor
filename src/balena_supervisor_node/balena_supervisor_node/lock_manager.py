"""Management of the balena update lock.

balena stacks two independent mechanisms on one file, ``/tmp/balena/updates.lock``:

* **Application updates** are blocked by the file's *existence*. The supervisor
  requires it to have been created with ``O_CREAT | O_EXCL`` and held open by a
  process -- touching the path is not enough.
* **Host OS updates** are blocked by an exclusive ``flock()`` on the same file.
  ``safe_reboot`` in ``hostapp-update`` waits on that lock, indefinitely and
  with no timeout, before rebooting the device.

Holding both means one file descriptor: create with ``O_EXCL``, then ``flock``
it, then keep the descriptor open.

Using both also makes stale locks detectable. Application locks survive a
process crash (they clear only on device reboot), so on startup a leftover file
is ambiguous -- but the flock is not. If we can take the flock on an existing
file, no live process holds it and the file is a corpse we may reclaim. If we
cannot, something alive owns it, possibly the supervisor mid-update, and we
leave it alone.

References:
https://docs.balena.io/learn/deploy/release-strategy/update-locking/
https://github.com/balena-os/meta-balena/blob/master/README.md#os-update-locks
"""

from __future__ import annotations

import errno
import fcntl
import os

DEFAULT_LOCK_PATH = '/tmp/balena/updates.lock'

# Outcomes, mirroring the constants in TakeUpdateLock.srv.
ACQUIRED = 0
ALREADY_HELD_BY_US = 1
HELD_BY_OTHER = 2
RECLAIMED_STALE = 3
FAILED = 4


class UpdateLockManager:
    """Owns at most one update lock for the lifetime of the node.

    Not internally synchronised: callers should serialise access, which the
    node does by running lock services in a mutually exclusive callback group.
    """

    def __init__(self, lock_path: str = DEFAULT_LOCK_PATH) -> None:
        self.lock_path = lock_path
        #: The one descriptor that carries both locks, or None when not held.
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def take(self) -> tuple[int, str]:
        """Take both locks. Returns ``(outcome, message)``."""
        if self.held:
            return ALREADY_HELD_BY_US, 'Update lock already held by this node.'

        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir and not os.path.isdir(lock_dir):
            # /tmp/balena is mounted in by balena. Its absence means we are not
            # running as a balena service, so fail loudly rather than creating
            # a lock nothing will ever honour.
            return FAILED, (
                '{} does not exist. The update lock is only meaningful inside '
                'a balena service container.'.format(lock_dir))

        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            return self._reclaim_if_stale()
        except OSError as exc:
            return FAILED, 'Could not create {}: {}'.format(self.lock_path, exc)

        if not self._flock(fd):
            # Vanishingly unlikely: we just created the file exclusively, so
            # nobody else should have it open. Do not leave a file behind that
            # blocks application updates without us holding the host OS lock.
            os.close(fd)
            self._unlink_quietly()
            return FAILED, (
                'Created {} but could not flock it; lock not taken.'.format(
                    self.lock_path))

        self._fd = fd
        return ACQUIRED, 'Update lock taken; application and host OS updates blocked.'

    def _reclaim_if_stale(self) -> tuple[int, str]:
        """Decide whether an existing lock file is abandoned, and take it if so."""
        try:
            fd = os.open(self.lock_path, os.O_RDWR)
        except OSError as exc:
            return FAILED, 'Lock file exists but could not be opened: {}'.format(exc)

        if not self._flock(fd):
            os.close(fd)
            return HELD_BY_OTHER, (
                'Update lock is held by another live process (possibly the '
                'supervisor applying an update). Not taken.')

        # We hold the flock, so no live process owns this file. Whoever created
        # it is gone.
        self._fd = fd
        return RECLAIMED_STALE, (
            'Reclaimed a stale update lock left by a previous run; application '
            'and host OS updates blocked.')

    def release(self) -> tuple[bool, str]:
        """Release both locks. Returns ``(success, message)``."""
        if not self.held:
            return True, 'No update lock held; nothing to release.'

        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        # Unlink last: until the file is gone, application updates stay blocked.
        removed, detail = self._unlink_quietly()
        if not removed:
            return False, (
                'Released the host OS lock but could not remove {}: {}. '
                'Application updates stay blocked until it is removed.'.format(
                    self.lock_path, detail))
        return True, 'Update lock released.'

    def _flock(self, fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def _unlink_quietly(self) -> tuple[bool, str]:
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            return True, ''
        except OSError as exc:
            return False, str(exc)
        return True, ''

    def __enter__(self) -> tuple[int, str]:
        return self.take()

    def __exit__(self, *_: object) -> bool:
        self.release()
        return False
