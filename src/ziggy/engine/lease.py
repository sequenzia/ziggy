"""Cross-process workspace lease (spec §6.4, REQ-010; ARCHITECTURE persistence layout).

One mutating Ziggy run per canonical workspace. The lease is a JSON file named
``leases/<sha256(canonical workspace)>.json`` inside the store root — outside
the repository, so project content cannot forge or disable it. Acquisition is
an ``O_EXCL`` create (0600). An existing lease is only replaced when the owner
is *provably* dead: ``ESRCH`` on the recorded pid AND on the recorded process
group (a pgid of 1 or lower skips the group probe). Everything ambiguous —
``EPERM`` liveness probes, a live pid whose start marker no longer matches, an
unreadable or corrupt lease file — stays busy rather than risking concurrent
mutation.

Owner identity is ``(pid, start marker)`` where the start marker is a stable
hash of the process start time (``ps -p PID -o lstart=`` on darwin/linux, with
a psutil-free ``/proc/<pid>/stat`` field-22 fallback when available). A marker
of ``None``/empty means "unavailable" and makes any comparison ambiguous.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal

from ziggy.errors import PersistenceError, WorkspaceBusyError
from ziggy.ids import utc_now_iso
from ziggy.models import WorkspaceLease
from ziggy.store.runstore import FILE_MODE, atomic_write_bytes

if TYPE_CHECKING:
    from ziggy.store import RunStore

__all__ = ["AcquiredLease", "LeaseManager", "canonical_workspace_hash", "lease_path"]

#: Serializes lease acquisition within this process so two threads recovering
#: the same stale lease cannot both win. Cross-process races are handled by the
#: O_EXCL create and the post-replace read-back verification.
_ACQUIRE_LOCK = threading.Lock()

_PS_TIMEOUT_SECONDS = 5.0

_Liveness = Literal["alive", "dead", "ambiguous"]


def canonical_workspace_hash(workspace: Path) -> str:
    """sha256 hex digest of the workspace realpath as a POSIX string."""
    canonical = Path(os.path.realpath(workspace)).as_posix()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lease_path(store: RunStore, workspace: Path) -> Path:
    """Lease file path for ``workspace`` inside ``store`` (dirs are created 0700)."""
    return store.leases_dir / f"{canonical_workspace_hash(workspace)}.json"


def _process_start_marker(pid: int) -> str | None:
    """Stable, opaque token identifying the *incarnation* of ``pid``.

    Primary source: hash of ``ps -p PID -o lstart=`` output (darwin/linux).
    Fallback: ``/proc/<pid>/stat`` field 22 (starttime in clock ticks) when a
    procfs is available. Returns ``None`` when neither source can answer —
    callers must then treat any identity comparison as ambiguous.
    """
    if pid <= 0:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        text = proc.stdout.strip()
        if text:
            return "ps:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    # psutil-free procfs fallback (linux): starttime is field 22 of
    # /proc/<pid>/stat; the comm field may contain spaces, so split after the
    # closing paren. Fields 3..N follow, making starttime index 19 there.
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:
        return None
    return "proc:" + fields[19]


def _pid_liveness(pid: int) -> _Liveness:
    """Probe ``pid`` with signal 0. EPERM means alive-but-other-user: ambiguous."""
    if pid <= 0:
        return "ambiguous"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "ambiguous"
    except OSError:
        return "ambiguous"
    return "alive"


def _pgid_provably_dead(pgid: int) -> bool:
    """True when the recorded process group is provably gone.

    A pgid of 1 or lower is never probed (``killpg`` on it would be unsafe or
    meaningless) and does not block recovery: the pid probe already returned
    ESRCH by the time this runs.
    """
    if pgid <= 1:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


@dataclass
class AcquiredLease:
    """A held workspace lease. Sync context-manager; ``release()`` on teardown."""

    lease: WorkspaceLease
    path: Path
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        """Remove the lease file only if it still holds our run_id.

        Idempotent; tolerates an already-removed file and never removes a
        lease that a different run now holds.
        """
        if self._released:
            return
        self._released = True
        try:
            raw = self.path.read_bytes()
        except OSError:
            return  # already gone, or unreadable: never remove blindly
        try:
            data = json.loads(raw)
        except ValueError:
            return  # not ours to judge; leave it for conservative recovery
        if isinstance(data, dict) and data.get("run_id") == self.lease.run_id:
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> AcquiredLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class LeaseManager:
    """Acquires and recovers workspace leases. Stateless; safe to share."""

    def acquire(self, store: RunStore, workspace: Path, run_id: str) -> AcquiredLease:
        """Acquire the single-mutator lease for ``workspace`` on behalf of ``run_id``.

        Raises ``WorkspaceBusyError`` (details: ``run_id`` of the holder,
        ``workspace``, ``acquired_at``, ``reason`` of ``held`` | ``ambiguous``)
        when another run holds the lease or ownership cannot be proven stale.
        """
        with _ACQUIRE_LOCK:
            canonical = Path(os.path.realpath(workspace)).as_posix()
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            path = store.leases_dir / f"{digest}.json"
            ours = WorkspaceLease(
                canonical_workspace_hash=digest,
                workspace=canonical,
                run_id=run_id,
                owner_pid=os.getpid(),
                owner_process_start=_process_start_marker(os.getpid()) or "",
                owner_process_group=os.getpgrp(),
                acquired_at=utc_now_iso(),
            )
            payload = ours.model_dump_json(indent=2).encode("utf-8")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            except FileExistsError:
                return self._contend(path=path, ours=ours, payload=payload, canonical=canonical)
            except OSError as exc:
                raise PersistenceError(
                    f"cannot create workspace lease: {exc}", details={"path": str(path)}
                ) from exc
            try:
                os.fchmod(fd, FILE_MODE)
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            return AcquiredLease(lease=ours, path=path)

    def _contend(
        self, *, path: Path, ours: WorkspaceLease, payload: bytes, canonical: str
    ) -> AcquiredLease:
        """An existing lease file was found: verify liveness, recover, or fail busy."""
        holder = self._read_holder(path, canonical)
        liveness = _pid_liveness(holder.owner_pid)
        if liveness == "alive":
            recorded = holder.owner_process_start
            current = _process_start_marker(holder.owner_pid)
            if recorded and current and recorded == current:
                raise self._busy(holder=holder, canonical=canonical, reason="held")
            # live pid, but identity unproven (marker mismatch or unavailable)
            raise self._busy(holder=holder, canonical=canonical, reason="ambiguous")
        if liveness == "ambiguous":
            raise self._busy(holder=holder, canonical=canonical, reason="ambiguous")
        # pid is ESRCH-dead; recovery further requires the recorded group gone
        if not _pgid_provably_dead(holder.owner_process_group):
            raise self._busy(holder=holder, canonical=canonical, reason="ambiguous")
        return self._replace_stale(path=path, ours=ours, payload=payload, canonical=canonical)

    def _read_holder(self, path: Path, canonical: str) -> WorkspaceLease:
        """Parse the current lease file; unreadable or corrupt content is ambiguous."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            # Holder vanished between O_EXCL failure and this read (e.g. a
            # concurrent release). Conservative: report busy-ambiguous rather
            # than looping on the create race.
            raise self._busy(holder=None, canonical=canonical, reason="ambiguous") from None
        except OSError:
            raise self._busy(holder=None, canonical=canonical, reason="ambiguous") from None
        try:
            return WorkspaceLease.model_validate_json(raw)
        except ValueError:
            raise self._busy(holder=None, canonical=canonical, reason="ambiguous") from None

    def _replace_stale(
        self, *, path: Path, ours: WorkspaceLease, payload: bytes, canonical: str
    ) -> AcquiredLease:
        """Atomically replace a provably-stale lease, then re-read to confirm.

        Guards the race where two processes recover simultaneously: after the
        ``os.replace`` the file is read back, and if it no longer carries our
        run_id another recoverer won — we must report busy, not proceed.
        """
        atomic_write_bytes(path.parent, path.name, payload)
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise self._busy(holder=None, canonical=canonical, reason="ambiguous") from None
        if not isinstance(data, dict) or data.get("run_id") != ours.run_id:
            winner: WorkspaceLease | None = None
            if isinstance(data, dict):
                with contextlib.suppress(ValueError):
                    winner = WorkspaceLease.model_validate(data)
            raise self._busy(
                holder=winner,
                canonical=canonical,
                reason="held" if winner is not None else "ambiguous",
            )
        return AcquiredLease(lease=ours, path=path)

    @staticmethod
    def _busy(
        *,
        holder: WorkspaceLease | None,
        canonical: str,
        reason: Literal["held", "ambiguous"],
    ) -> WorkspaceBusyError:
        holder_run = holder.run_id if holder is not None else None
        acquired_at = holder.acquired_at if holder is not None else None
        suffix = f" (held by run {holder_run})" if holder_run else ""
        return WorkspaceBusyError(
            f"workspace {canonical} is busy{suffix}: {reason}",
            details={
                "run_id": holder_run,
                "workspace": canonical,
                "acquired_at": acquired_at,
                "reason": reason,
            },
        )
