# SPDX-License-Identifier: GPL-2.0-or-later
"""Local files: proposals, the single automatic backup, recovery metadata and locks.

Layout under the state root:

    proposals/<id>.json              temporary, bounded, expired ones are pruned
    devices/<binding>/previous.json  exactly one automatic previous configuration
    devices/<binding>/pending.json   pre-write baseline, only while a write is unresolved
    devices/<binding>/recovery.json  progress metadata for an unresolved write
    locks/                           advisory OS locks
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import VialticError
from .models import Journal, Proposal, Snapshot

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PROPOSALS = 64
M = TypeVar("M", bound=BaseModel)


def state_root() -> pathlib.Path:
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Vialtic" / "state"
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local" / "state")
    return pathlib.Path(base) / "vialtic"


def data_root() -> pathlib.Path:
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Vialtic" / "data"
    base = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(base) / "vialtic"


def _ensure_dir(path: pathlib.Path) -> pathlib.Path:
    """Create a directory chain durably: a backup must survive power loss with its parents."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for created in reversed(missing):
        created.chmod(0o700)
        _fsync_dir(created.parent)
    return path


def atomic_write(path: pathlib.Path, data: bytes, own_directory: bool = True) -> None:
    """Write durably: temp file in the same directory, fsync, rename, fsync directory.

    `own_directory` is false for paths a user chose, where a similarly named file is theirs.
    """
    _ensure_dir(path.parent)
    # A process killed mid-write leaves its temp file, possibly a full snapshot, behind.
    if own_directory:
        for stale in path.parent.glob(f".{path.name}.*"):
            stale.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _fsync_dir(path: pathlib.Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_model(path: pathlib.Path, model: BaseModel) -> None:
    atomic_write(path, model.model_dump_json(indent=2).encode("utf-8"))


def read_model(path: pathlib.Path, cls: type[M]) -> M:
    """Load a schema-versioned file, refusing oversized, malformed or newer content."""
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            raise VialticError("invalid_file", f"{path} is {size} bytes, over the {MAX_JSON_BYTES} byte limit.")
        return cls.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        raise VialticError("invalid_file", f"{path} does not match the expected format.",
                           details={"errors": exc.error_count()}) from exc
    except OSError as exc:
        raise VialticError("storage_failed", f"Could not read {path}: {exc}") from exc


class Store:
    def __init__(self, root: pathlib.Path):
        self.root = root

    # Locks.

    @contextlib.contextmanager
    def _lock(self, name: str, timeout: float, what: str) -> Iterator[None]:
        import fcntl

        path = _ensure_dir(self.root / "locks") / name
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VialticError("lock_timeout", f"Another Vialtic process is using {what}.",
                                           next_steps=["Wait for the other operation to finish, then retry."]) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def path_lock(self, device_path: str, timeout: float) -> Any:
        """Serializes all traffic to one USB path across processes."""
        digest = hashlib.sha256(device_path.encode("utf-8", "surrogateescape")).hexdigest()[:24]
        return self._lock(f"path-{digest}.lock", timeout, f"the device at {device_path}")

    def binding_lock(self, binding_id: str, timeout: float) -> Any:
        """Serializes one bound keyboard's write lifecycle, across replug and path changes."""
        return self._lock(f"binding-{binding_id}.lock", timeout, "this keyboard")

    # Proposals.

    def _proposal_path(self, proposal_id: str) -> pathlib.Path:
        if not proposal_id.replace("-", "").isalnum() or len(proposal_id) > 64:
            raise VialticError("invalid_request", f"Malformed proposal id {proposal_id!r}.")
        return self.root / "proposals" / f"{proposal_id}.json"

    def save_proposal(self, proposal: Proposal, now: datetime) -> None:
        self._prune_proposals(now)
        write_model(self._proposal_path(proposal.id), proposal)

    def load_proposal(self, proposal_id: str) -> Proposal:
        path = self._proposal_path(proposal_id)
        if not path.exists():
            raise VialticError("proposal_not_found", f"No proposal {proposal_id} exists on this machine.",
                               next_steps=["Create it again with `vialtic plan`."])
        return read_model(path, Proposal)

    def delete_proposal(self, proposal_id: str) -> None:
        with contextlib.suppress(OSError):
            self._proposal_path(proposal_id).unlink()

    def _prune_proposals(self, now: datetime) -> None:
        directory = self.root / "proposals"
        if not directory.is_dir():
            return
        remaining = []
        for path in directory.glob("*.json"):
            try:
                proposal = Proposal.model_validate_json(path.read_bytes())
                if proposal.created_at.tzinfo is None or proposal.expires_at.tzinfo is None or proposal.expires_at <= now:
                    path.unlink()
                else:
                    remaining.append((proposal.created_at, path))
            except (OSError, ValidationError):
                with contextlib.suppress(OSError):
                    path.unlink()
        remaining.sort(key=lambda item: item[0])
        for _, path in remaining[:max(0, len(remaining) - MAX_PROPOSALS + 1)]:
            with contextlib.suppress(OSError):
                path.unlink()

    # Per-device backup and recovery state.

    def device_dir(self, binding_id: str) -> pathlib.Path:
        return self.root / "devices" / binding_id

    def previous_path(self, binding_id: str) -> pathlib.Path:
        return self.device_dir(binding_id) / "previous.json"

    def pending_path(self, binding_id: str) -> pathlib.Path:
        return self.device_dir(binding_id) / "pending.json"

    def journal_path(self, binding_id: str) -> pathlib.Path:
        return self.device_dir(binding_id) / "recovery.json"

    def unlock_marker_path(self, binding_id: str) -> pathlib.Path:
        return self.device_dir(binding_id) / "unlocked-by-vialtic"

    def write_pending(self, binding_id: str, snapshot: Snapshot) -> pathlib.Path:
        path = self.pending_path(binding_id)
        try:
            write_model(path, snapshot)
            # Prove the durable copy reads back before any device write happens.
            if read_model(path, Snapshot).family_fingerprints != snapshot.family_fingerprints:
                raise VialticError("backup_failed", f"Backup at {path} does not match what was read.")
        except OSError as exc:
            raise VialticError("backup_failed", f"Could not save the backup required before writing: {exc}",
                               next_steps=["Free disk space or fix permissions on the state directory, then retry."]) from exc
        return path

    def read_snapshot(self, path: pathlib.Path) -> Snapshot:
        return read_model(path, Snapshot)

    def promote_pending(self, binding_id: str) -> pathlib.Path:
        """The write verified: its baseline becomes the single previous configuration."""
        previous = self.previous_path(binding_id)
        os.replace(self.pending_path(binding_id), previous)
        _fsync_dir(previous.parent)
        return previous

    def discard_pending(self, binding_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.pending_path(binding_id).unlink()

    def write_journal(self, journal: Journal) -> None:
        write_model(self.journal_path(journal.binding_id), journal)

    def read_journal(self, binding_id: str) -> Journal | None:
        path = self.journal_path(binding_id)
        return read_model(path, Journal) if path.exists() else None

    def clear_journal(self, binding_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.journal_path(binding_id).unlink()

    def settle_finished_write(self, binding_id: str) -> None:
        """Finish rotation for a write that verified but crashed before cleaning up. No device access."""
        # The baseline only goes away once its write is resolved, so a journal without one is a leftover.
        if self.journal_path(binding_id).exists() and not self.pending_path(binding_id).exists():
            with contextlib.suppress(OSError):
                self.clear_journal(binding_id)
            return
        try:
            journal = self.read_journal(binding_id)
            if journal is None or journal.status != "verified":
                return
            # A damaged baseline must not replace a good previous.json.
            if journal.proposal_type != "recovery":
                self.read_snapshot(self.pending_path(binding_id))
        except VialticError:
            return  # Unreadable metadata stays pending; `recover plan` can still resolve it.
        self.finish_write(binding_id, journal.proposal_type)

    def finish_write(self, binding_id: str, proposal_type: str) -> None:
        """A verified write ends here: rotate its baseline, or drop it if it was a recovery."""
        if self.pending_path(binding_id).exists():
            if proposal_type == "recovery":
                self.discard_pending(binding_id)
            else:
                self.promote_pending(binding_id)
        self.clear_journal(binding_id)

    def has_unresolved_write(self, binding_id: str) -> bool:
        return self.journal_path(binding_id).exists() or self.pending_path(binding_id).exists()

    def set_unlock_marker(self, binding_id: str, proposal_id: str, path: str | None = None) -> None:
        """Remember that Vialtic unlocked this keyboard, for which proposal and at which USB path."""
        atomic_write(self.unlock_marker_path(binding_id), json.dumps({"proposal_id": proposal_id, "path": path}).encode())

    def clear_unlock_marker(self, binding_id: str, path: str | None = None) -> None:
        """An identical keyboard shares the binding, so with a path this only clears that unit's marker."""
        if path is not None and self.read_unlock_marker(binding_id, path) is None:
            return
        with contextlib.suppress(FileNotFoundError):
            self.unlock_marker_path(binding_id).unlink()

    def read_unlock_marker(self, binding_id: str, path: str | None = None) -> str | None:
        try:
            marker = json.loads(self.unlock_marker_path(binding_id).read_text())
            if path is not None and marker.get("path") not in (None, path):
                return None
            return marker.get("proposal_id", "")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError):
            return ""
