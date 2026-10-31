# SPDX-License-Identifier: GPL-2.0-or-later
"""Use cases shared by every front end, including the only path that writes to a keyboard."""
from __future__ import annotations

import contextlib
import platform
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import __version__, proposals, render
from . import vial_adapter as va
from .device import Backend, DeviceManager, Session
from .errors import VialticError
from .models import (
    ApplyResult, ApprovalAttestation, ChangeSet, DeviceEntry, DeviceIdentity, DeviceList, DoctorCheck, DoctorReport,
    FollowingWrite,
    InspectResult, Journal, KeycodeList, Operation, OperationResult, Proposal, ProposalType, Snapshot, UnlockPosition,
    UnlockStatus, VerificationDifference, VersionInfo, ViewResult,
)
from .storage import Store


@dataclass
class ServiceOptions:
    reconnect_window: float = 30.0
    reconnect_poll: float = 1.0
    unlock_timeout: float = 60.0
    unlock_poll: float = 0.2
    lock_timeout: float = 10.0
    proposal_ttl: timedelta = proposals.DEFAULT_TTL
    max_retries: int | None = None
    progress: Callable[[str], None] = lambda message: None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleep: Callable[[float], None] = time.sleep


class _WriteFailure(Exception):
    """A device write or read-back failed partway; recovery decides the public outcome."""

    def __init__(self, message: str, differences: list[VerificationDifference] | None = None):
        super().__init__(message)
        self.message = message
        self.differences = differences or []


@dataclass
class _ApplyState:
    proposal: Proposal
    baseline: Snapshot
    live: DeviceIdentity
    completed: int = 0
    failed_index: int | None = None
    packets_before: int = 0
    packets_total: int = 0
    unlocked_here: bool = False
    relock_required: bool = False
    relocked: bool | None = None
    relock_error: str | None = None
    # Set once read-back passed. From then on the write stands and is never rolled back.
    committed: bool = False
    # Whether the firmware may have received this write's lighting save.
    save_attempted: bool = False
    # Families a recovery cannot restore and leaves as they were at planning time.
    skip_families: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_relock(self) -> bool:
        return self.unlocked_here or self.relock_required


class VialticService:
    def __init__(self, backend: Backend, store: Store, options: ServiceOptions | None = None):
        self.options = options or ServiceOptions()
        self.store = store
        self.backend = backend
        self.devices = DeviceManager(backend, store, self.options.max_retries, self.options.lock_timeout)

    # Read-only.

    def version(self) -> VersionInfo:
        return VersionInfo(version=__version__, vial_gui_revision=va.VIAL_GUI_REVISION,
                           supported_via_protocols=va.SUPPORTED_VIA_PROTOCOL,
                           supported_vial_protocols=va.SUPPORTED_VIAL_PROTOCOL, python=platform.python_version(),
                           platform=f"{sys.platform}-{platform.machine()}", frozen=bool(getattr(sys, "frozen", False)))

    def list_devices(self) -> DeviceList:
        entries, ignored, _ = self.devices.list_devices()
        return DeviceList(simulated=self.backend.simulated, devices=entries, ignored=ignored)

    def inspect_device(self, selector: str | None = None) -> InspectResult:
        entry, desc, _ = self.devices.select(selector)
        with self.devices.session(desc) as session:
            binding = session.identity.binding_id
            self.store.settle_finished_write(binding)
            return InspectResult(device=session.identity, capabilities=va.capabilities_of(session.kb),
                                 layout=va.layout_of(session.kb), unlock=self._unlock_status(session),
                                 recovery_pending=self.store.has_unresolved_write(binding),
                                 previous_available=self.store.previous_path(binding).exists(),
                                 warnings=self._unlock_warnings(session))

    def read_snapshot(self, selector: str | None = None) -> Snapshot:
        entry, desc, _ = self.devices.select(selector)
        with self.devices.session(desc) as session:
            snapshot = self._with_recovery_warning(session.snapshot(self.options.now()))
            extra = self._unlock_warnings(session)
            return snapshot.model_copy(update={"warnings": snapshot.warnings + extra}) if extra else snapshot

    def _unlock_warnings(self, session: Session) -> list[str]:
        """Say so if Vialtic unlocked this keyboard and did not lock it again."""
        binding = session.identity.binding_id
        if self.store.read_unlock_marker(binding) is None:
            return []
        if session.kb.get_unlock_status() != 1:
            self.store.clear_unlock_marker(binding)
            return []
        return ["Vialtic unlocked this keyboard and has not locked it again. Run `vialtic lock` or replug it."]

    def _with_recovery_warning(self, snapshot: Snapshot) -> Snapshot:
        binding = snapshot.device.binding_id
        self.store.settle_finished_write(binding)
        if self.store.has_unresolved_write(binding):
            return snapshot.model_copy(update={"warnings": snapshot.warnings + [
                "An earlier write did not finish cleanly. Run `vialtic recover plan`."]})
        return snapshot

    def render(self, selector: str | None, layers: list[int] | None, fmt: str, output: str | None,
               proposal_id: str | None = None, show_unlock: bool = False) -> ViewResult:
        proposal = self._load_proposal(proposal_id) if proposal_id else None
        entry, desc, _ = self.devices.select(selector)
        with self.devices.session(desc) as session:
            snapshot = session.snapshot(self.options.now())
            highlights = []
            if show_unlock:
                highlights = [(r, c) for r, c in session.kb.get_unlock_keys()]
        if proposal is not None and proposal.device.binding_id != snapshot.device.binding_id:
            raise VialticError("device_changed", "That proposal belongs to a different keyboard.",
                               details={"proposal_device": proposal.device.name})
        chosen = layers if layers is not None else list(range(snapshot.capabilities.layers))
        for layer in chosen:
            if not 0 <= layer < snapshot.capabilities.layers:
                raise VialticError("invalid_coordinate", f"Layer {layer} does not exist; this keyboard has layers "
                                                         f"0-{snapshot.capabilities.layers - 1}.")
        text = render.render_text(snapshot, chosen, proposal, highlights)
        path = None
        if fmt == "png":
            path = render.write_png(snapshot, chosen, proposal, highlights, output, self.store.root / "renders")
        return ViewResult(device=snapshot.device, layers=chosen, format=fmt, text=text, artifact_path=path,
                          proposal_id=proposal.id if proposal else None)

    def keycodes(self, selector: str | None, query: str | None) -> KeycodeList:
        entry, desc, _ = self.devices.select(selector)
        with self.devices.session(desc) as session:
            return KeycodeList(device=session.identity, keycodes=va.list_keycodes(query))

    def _unlock_status(self, session: Session) -> UnlockStatus:
        kb = session.kb
        try:
            positions = [UnlockPosition(row=r, col=c) for r, c in kb.get_unlock_keys()]
            return UnlockStatus(unlocked=kb.get_unlock_status() == 1, in_progress=kb.get_unlock_in_progress() == 1,
                                positions=positions)
        except RuntimeError as exc:
            raise VialticError("device_communication_failed", f"Reading the lock state failed: {exc}") from exc

    def diagnose(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        entries: list[DeviceEntry] = []
        try:
            entries, ignored, _ = self.devices.list_devices()
            checks.append(DoctorCheck(name="hid_backend", status="ok", message="The HID library loaded and enumerated devices."))
        except VialticError as exc:
            ignored = []
            checks.append(DoctorCheck(name="hid_backend", status="problem", message=exc.message, next_steps=exc.next_steps))
        if not entries:
            checks.append(DoctorCheck(name="keyboards", status="warning", message="No USB Vial keyboard was found.",
                                      next_steps=["Connect the keyboard by USB. Wireless connections are not supported."]))
        for entry in entries:
            name = f"keyboard {entry.identity.device_id}"
            if entry.status == "ready":
                checks.append(DoctorCheck(
                    name=name, status="ok",
                    message=f"{entry.identity.name}: readable, VIA protocol {entry.identity.via_protocol}, "
                            f"Vial protocol {entry.identity.vial_protocol}."))
                if entry.identity.identity_confidence != "usb_serial":
                    checks.append(DoctorCheck(name=f"{name} identity", status="info", message=entry.identity.identity_note))
                self.store.settle_finished_write(entry.identity.binding_id)
                if self.store.read_unlock_marker(entry.identity.binding_id) is not None:
                    checks.append(DoctorCheck(name=f"{name} lock", status="warning",
                                              message="Vialtic unlocked this keyboard and may not have locked it again.",
                                              next_steps=["Run `vialtic lock` or replug the keyboard."]))
                if self.store.has_unresolved_write(entry.identity.binding_id):
                    checks.append(DoctorCheck(name=f"{name} recovery", status="problem",
                                              message="An earlier write did not finish cleanly.",
                                              next_steps=["Run `vialtic recover plan`."]))
            else:
                problem = entry.problem
                checks.append(DoctorCheck(name=name, status="problem", message=problem.message if problem else entry.status,
                                          next_steps=problem.next_steps if problem else []))
        for item in ignored:
            checks.append(DoctorCheck(name=f"ignored {item.vendor_id}:{item.product_id}", status="info",
                                      message=f"{item.name}: {item.reason}."))
        checks.append(DoctorCheck(name="other_software", status="info",
                                  message="Vial or VIA running at the same time can interleave with Vialtic's reads. "
                                          "Close them before applying changes."))
        return DoctorReport(version=__version__, platform=f"{sys.platform}-{platform.machine()}",
                            simulated=self.backend.simulated, checks=checks, devices=entries)

    # Proposals.

    def create_proposal(self, selector: str | None, changes: ChangeSet) -> Proposal:
        entry, desc, entries = self.devices.select(selector)
        with self.devices.session(desc) as session:
            self._refuse_if_unresolved(session.identity)
            snapshot = session.snapshot(self.options.now())
            operations, warnings = proposals.validate_changes(session, snapshot, changes.changes)
            if not operations:
                raise VialticError("no_changes", "The keyboard already holds every requested value.",
                                   details={"notes": warnings})
            return self._finish_proposal("change", session, entries, snapshot, operations, warnings)

    def create_undo_proposal(self, selector: str | None) -> Proposal:
        entry, desc, entries = self.devices.select(selector)
        with self.devices.session(desc) as session:
            binding = session.identity.binding_id
            self._refuse_if_unresolved(session.identity)
            path = self.store.previous_path(binding)
            if not path.exists():
                raise VialticError("nothing_to_undo", "No previous configuration is saved for this keyboard.")
            target = self.store.read_snapshot(path)
            snapshot = session.snapshot(self.options.now())
            operations, notes = self._restore_plan(snapshot, target, "undo", selector, path)
            if not operations:
                raise VialticError("no_changes", "The keyboard already matches the previous configuration.")
            notes.append("After this undo, the configuration being replaced becomes the single previous state.")
            return self._finish_proposal("undo", session, entries, snapshot, operations, notes)

    def create_recovery_proposal(self, selector: str | None) -> Proposal:
        entry, desc, entries = self.devices.select(selector)
        with self.devices.session(desc) as session:
            binding = session.identity.binding_id
            self.store.settle_finished_write(binding)
            try:
                journal = self.store.read_journal(binding)
            except VialticError:
                journal = None
            path = self.store.pending_path(binding)
            if not path.exists():
                self.store.clear_journal(binding)
                raise VialticError("nothing_to_recover", "No interrupted write is recorded for this keyboard.")
            try:
                target = self.store.read_snapshot(path)
            except VialticError as exc:
                raise VialticError(exc.code, f"The saved state cannot be read: {exc.message}", details=exc.details,
                                   next_steps=[f"Vialtic will not delete it. Inspect {path} or move it aside by hand "
                                               "to continue, then check the keyboard with `vialtic view`."]) from exc
            snapshot = session.snapshot(self.options.now())
            operations, notes = self._restore_plan(snapshot, target, "recovery", selector, path, journal)
            if journal is not None:
                notes.insert(0, f"Restores the state saved before proposal {journal.proposal_id}, which "
                                f"finished {journal.completed_operations} of {journal.total_operations} operations.")
            return self._finish_proposal("recovery", session, entries, snapshot, operations, notes)

    def _restore_plan(self, current: Snapshot, target: Snapshot, purpose: str, selector: str | None, path,
                      journal: Journal | None = None) -> tuple[list[Operation], list[str]]:
        way_out = (["Nothing was changed. Vialtic will not delete the saved state. Check the keyboard with `vialtic view`, "
                    f"then move {path} aside by hand to continue."] if purpose == "recovery"
                   else ["Create a new proposal for the changes you want."])
        if target.device.binding_id != current.device.binding_id:
            raise VialticError("device_changed", "The saved configuration belongs to a different keyboard or firmware.",
                               next_steps=way_out)
        # The binding does not cover firmware updates, and saved values mean something else across these boundaries.
        if not va.same_encoding(target.device.vial_protocol, current.device.vial_protocol):
            raise VialticError("device_changed", f"The saved configuration was read from Vial protocol "
                                                 f"{target.device.vial_protocol} and this keyboard now reports "
                                                 f"{current.device.vial_protocol}, which encodes keycodes or macros differently.",
                               next_steps=way_out)
        notes_for_unit = []
        # Without a real serial, the backup may belong to an identical unit. Only a person can tell.
        if target.device.path != current.device.path and current.device.identity_confidence != "usb_serial":
            if not selector:
                raise VialticError(
                    "device_ambiguous",
                    f"The saved configuration came from a keyboard at {target.device.path} and this one is at "
                    f"{current.device.path}. Identical keyboards cannot be told apart, so it may be a different unit.",
                    details={"backup_path": target.device.path, "connected": current.device.device_id},
                    next_steps=["Ask the user whether this is the same physical keyboard. If it is, pass "
                                f"--device {current.device.device_id}."])
            notes_for_unit.append(f"The saved configuration came from a keyboard at {target.device.path}, not "
                                  f"{current.device.path}. It is applied here because this keyboard was selected explicitly.")
        # Only what the reversed write changed: lighting can differ just because someone pressed a key.
        following = target.followed_by
        lighting_save, systems = None, []
        if following and "lighting" in following.families:
            systems = following.lighting_systems
            # An interrupted write may never have reached its save, which comes last.
            lighting_save = journal.lighting_save_sent if purpose == "recovery" and journal else following.lighting_saved
        try:
            operations, blockers, notes = proposals.restore_operations(
                current.configuration, target.configuration, current.device.vial_protocol, lighting_save, systems)
        except VialticError:
            raise
        except Exception as exc:
            raise VialticError("invalid_file", f"The saved configuration at {path} holds values that cannot be restored: "
                                               f"{type(exc).__name__}.",
                               next_steps=[f"Vialtic will not delete it. Move {path} aside by hand to continue."]) from exc
        notes = notes_for_unit + notes
        if blockers and purpose == "recovery":
            # Refusing here would leave the keyboard unable to accept any new plan.
            notes.append(f"The firmware no longer has the same entries for {', '.join(blockers)} as the saved state, so "
                         "they cannot be restored and stay as they are now. Everything else is restored.")
        elif blockers:
            raise VialticError(
                "restore_unsupported",
                f"This {purpose} would need to restore {', '.join(blockers)}, and the firmware no longer has the same "
                "entries as the saved state. Nothing was changed.",
                details={"families": blockers}, next_steps=["Inspect the keyboard and create a new proposal instead."])
        return operations, notes

    def _finish_proposal(self, proposal_type: ProposalType, session: Session, entries: list[DeviceEntry],
                         snapshot: Snapshot, operations: list[Operation], warnings: list[str]) -> Proposal:
        warnings = list(warnings) + [w for w in snapshot.warnings if w not in warnings] + self._unlock_warnings(session)
        if session.identity.identity_confidence != "usb_serial":
            warnings.append(session.identity.identity_note)
        if self._twins(session.identity, entries):
            warnings.append("Another connected keyboard reports this exact identity. Backups are stored per identity, "
                            "so they cannot be attributed to one of these units with certainty.")
        positions: list[tuple[int, int]] = []
        if proposals.requires_unlock(operations):
            positions = [tuple(p) for p in session.kb.get_unlock_keys()]
        proposal = proposals.build_proposal(proposal_type, session.identity, snapshot, operations, warnings, positions,
                                            self.options.now(), self.options.proposal_ttl, self.options.reconnect_window)
        self.store.save_proposal(proposal, self.options.now())
        return proposal

    @staticmethod
    def _twins(identity: DeviceIdentity, entries: list[DeviceEntry]) -> list[DeviceEntry]:
        return [e for e in entries if e.identity.binding_id == identity.binding_id
                and e.identity.device_id != identity.device_id]

    def get_proposal(self, proposal_id: str) -> Proposal:
        return self._load_proposal(proposal_id)

    def _load_proposal(self, proposal_id: str) -> Proposal:
        proposal = self.store.load_proposal(proposal_id)
        # Also catches a valid proposal copied over another id, which approval must not cover.
        if proposal.id != proposal_id:
            raise VialticError("proposal_tampered", f"The file stored for {proposal_id} holds proposal {proposal.id}.",
                               next_steps=["Create a new proposal and get it approved."])
        proposals.check_integrity(proposal)
        return proposal

    def _check_approval(self, proposal: Proposal, attestation: ApprovalAttestation | None) -> None:
        if attestation is None:
            raise VialticError(
                "approval_required", f"Proposal {proposal.id} has not been attested as approved. Nothing was written.",
                next_steps=["Show the user this exact proposal and wait for explicit approval.",
                            f"Then run `vialtic apply {proposal.id} --user-approved`."])
        if attestation.proposal_id != proposal.id or attestation.binding_id != proposal.device.binding_id:
            raise VialticError("approval_required", "This approval was given for a different proposal or keyboard.")
        self._check_not_expired(proposal)

    def _check_not_expired(self, proposal: Proposal) -> None:
        if self.options.now() >= proposal.expires_at:
            raise VialticError("proposal_expired", f"Proposal {proposal.id} expired at {proposal.expires_at.isoformat()}.",
                               next_steps=["Create a new proposal and get it approved."])

    def _refuse_if_unresolved(self, identity: DeviceIdentity) -> None:
        binding = identity.binding_id
        self.store.settle_finished_write(binding)
        if self.store.has_unresolved_write(binding):
            raise VialticError("recovery_pending", "An earlier write to this keyboard did not finish cleanly. Resolve it first.",
                               details={"backup": str(self.store.pending_path(binding))},
                               next_steps=["Run `vialtic recover plan`, review it and apply it."])

    # Apply: the only code that opens the transport write gate.

    def apply_proposal(self, proposal_id: str, attestation: ApprovalAttestation | None,
                       selector: str | None = None) -> ApplyResult:
        proposal = self._load_proposal(proposal_id)
        self._check_approval(proposal, attestation)

        binding = proposal.device.binding_id
        with self.store.binding_lock(binding, self.options.lock_timeout):
            self.store.settle_finished_write(binding)
            if proposal.proposal_type != "recovery" and self.store.has_unresolved_write(binding):
                self._refuse_if_unresolved(proposal.device)
            desc, warnings = self._locate_for_write(proposal.device, selector)
            state: _ApplyState | None = None
            try:
                with self.devices.session(desc) as session:
                    state = self._start_write(session, proposal, warnings)
                    self._write_and_verify(session, state)
                    return self._finish_success(session, state)
            except _WriteFailure as failure:
                return self._recover_after_failure(state, failure)
            except KeyboardInterrupt:
                if state is None:
                    raise VialticError("cancelled", "Cancelled before anything was written.") from None
                if state.committed:
                    return self._committed_after_interrupt(state)
                return self._recover_after_failure(state, _WriteFailure("Cancelled while writing."))

    def _locate_for_write(self, device: DeviceIdentity, selector: str | None) -> tuple[dict[str, Any], list[str]]:
        """Find the keyboard a proposal is bound to. Never settles for 'the first match'."""
        entries, _, by_id = self.devices.list_devices()
        matches = [e for e in entries if e.identity.binding_id == device.binding_id]
        if not matches:
            # An unreadable keyboard reports no binding, so look for what it would have been.
            for entry in entries:
                problem = entry.problem
                if problem is not None and (entry.identity.vendor_id, entry.identity.product_id,
                                            entry.identity.serial_number) == (device.vendor_id, device.product_id,
                                                                              device.serial_number):
                    raise VialticError(problem.code, problem.message, details=problem.details, next_steps=problem.next_steps)
        chosen = [e for e in matches if selector in (e.identity.device_id, e.identity.path)] if selector else matches
        if not chosen:
            raise VialticError("device_changed", f"The keyboard this proposal was made for ({device.name}) is not connected, "
                                                 "not the one selected, or its firmware changed.",
                               details={"connected": [e.identity.name for e in entries]},
                               next_steps=["Reconnect it, or create a new proposal for the connected keyboard."])
        if len(chosen) > 1:
            raise VialticError("device_ambiguous", "Several connected keyboards match this proposal and cannot be told apart.",
                               details={"candidates": [e.identity.device_id for e in chosen]},
                               next_steps=["Ask the user which physical keyboard is meant, then pass --device <device_id>."])
        match = chosen[0]
        warnings = []
        if match.identity.path != device.path:
            # Without a real serial, a new path may be a replug or may be another identical unit.
            if device.identity_confidence != "usb_serial" and (
                    not selector or any(e.identity.path == device.path for e in entries)):
                raise VialticError(
                    "device_ambiguous",
                    f"This proposal was made for the keyboard at {device.path}, and a keyboard with the same identity is "
                    f"now at {match.identity.path}. Identical keyboards cannot be told apart, so it may be a different unit.",
                    details={"proposal_path": device.path, "connected": [e.identity.device_id for e in matches]},
                    next_steps=["Ask the user whether this is the same physical keyboard. If it is and the original path is "
                                "gone, pass --device <device_id>. Otherwise create a new proposal."])
            warnings.append(f"The USB path changed since this proposal was made ({device.path} -> {match.identity.path}).")
        return by_id[match.identity.device_id], warnings

    def _check_live_identity(self, session: Session, proposal: Proposal) -> None:
        identity, device = session.identity, proposal.device
        for name in ("binding_id", "layout_fingerprint", "via_protocol", "vial_protocol", "vial_uid"):
            if getattr(identity, name) != getattr(device, name):
                raise VialticError("device_changed", f"The keyboard's {name} changed since this proposal was made.",
                                   next_steps=["Create a new proposal."])

    def _check_baseline(self, current: Snapshot, proposal: Proposal) -> None:
        changed = [family for family in proposal.baseline.checked_families
                   if current.family_fingerprints.get(family) != proposal.baseline.family_fingerprints.get(family)]
        if changed:
            raise VialticError("proposal_stale", f"The keyboard's {', '.join(changed)} changed since this proposal was made, "
                                                 "possibly by another application. Nothing was written.",
                               details={"changed_families": changed},
                               next_steps=["Create a new proposal from the current state and get it approved."])

    def _start_write(self, session: Session, proposal: Proposal, warnings: list[str]) -> _ApplyState:
        """Recheck against the live keyboard, unlock if needed, recheck, and save the backup."""
        binding = session.identity.binding_id
        unlocked_here = False
        relock_required = False
        try:
            relock_required = self._unlock_marker_applies(session, proposal)
            if not relock_required:
                warnings = warnings + self._unlock_warnings(session)
            self._check_live_identity(session, proposal)
            self._check_baseline(session.snapshot(self.options.now()), proposal)
            unlocked_here = self._unlock_for(session, proposal)
            if unlocked_here:
                # The wait can be long: the proposal must still hold once the keys are released.
                session.refresh()
                self._check_live_identity(session, proposal)
            self._check_not_expired(proposal)
            current = session.snapshot(self.options.now())
            self._check_baseline(current, proposal)
            proposals.check_stated_values(proposal, session.kb)
            if session.kb.get_unlock_status() != 1 and not proposal.unlock.required and proposals.requires_unlock(
                    proposal.operations):
                raise VialticError("unlock_required", "This proposal needs a physical unlock it did not declare.")
            state = self._save_baseline(session, proposal, current, warnings)
            state.unlocked_here, state.relock_required = unlocked_here, relock_required
            return state
        except BaseException as exc:
            # No write gate has opened, so a baseline saved by this attempt protects nothing.
            if proposal.proposal_type != "recovery":
                self.store.discard_pending(binding)
                self.store.clear_journal(binding)
            if isinstance(exc, (KeyboardInterrupt, VialticError, RuntimeError, OSError)):
                self._relock_after_refusal(session, unlocked_here or relock_required, exc)
            if isinstance(exc, RuntimeError):
                raise VialticError("device_communication_failed", f"The keyboard stopped responding: {exc}",
                                   next_steps=["Replug the keyboard and retry."]) from exc
            if isinstance(exc, OSError):
                raise VialticError("backup_failed", f"Could not save the local state required before writing: {exc}",
                                   next_steps=["Free disk space or fix permissions on the state directory, then retry."]) from exc
            raise

    def _relock_after_refusal(self, session: Session, needed: bool, exc: BaseException) -> None:
        if not needed:
            return
        binding = session.identity.binding_id
        try:
            with session.transport.security():
                session.kb.lock()
            if session.kb.get_unlock_status() != 1:
                self.store.clear_unlock_marker(binding)
                return
        except Exception:
            pass
        if isinstance(exc, VialticError):
            exc.next_steps.append("The keyboard is still unlocked. Run `vialtic lock` or replug it.")

    def _unlock_marker_applies(self, session: Session, proposal: Proposal) -> bool:
        """Whether an unlock Vialtic performed earlier belongs to this operation."""
        binding = session.identity.binding_id
        marker = self.store.read_unlock_marker(binding)
        if marker is None:
            return False
        if session.kb.get_unlock_status() != 1:
            self.store.clear_unlock_marker(binding)  # Replugging locks the firmware, so the marker is stale.
            return False
        # A recovery ends whatever operation unlocked it, which may not have left a journal.
        return marker == proposal.id or proposal.proposal_type == "recovery"

    def _check_previous_readable(self, binding: str) -> None:
        """A write ends by replacing previous.json, so one this build cannot read must stop it first."""
        path = self.store.previous_path(binding)
        if not path.exists():
            return
        try:
            self.store.read_snapshot(path)
        except VialticError as exc:
            raise VialticError(exc.code, f"The saved previous configuration cannot be read, and this write would replace "
                                         f"it: {exc.message} Nothing was written.", details=exc.details,
                               next_steps=[f"Vialtic will not delete it. Move {path} aside by hand to continue, or use the "
                                           "Vialtic version that wrote it."]) from exc

    def _save_baseline(self, session: Session, proposal: Proposal, current: Snapshot, warnings: list[str]) -> _ApplyState:
        binding = session.identity.binding_id
        skip: list[str] = []
        if proposal.proposal_type == "recovery":
            pending = self.store.pending_path(binding)
            if not pending.exists():
                raise VialticError("nothing_to_recover", "The recovery baseline no longer exists.")
            baseline = self.store.read_snapshot(pending)
            following = baseline.followed_by
            skip = proposals.restore_operations(current.configuration, baseline.configuration, lighting_systems=(
                following.lighting_systems if following else ()))[1]
        else:
            self._check_previous_readable(binding)
            baseline = current.model_copy(update={"followed_by": FollowingWrite(
                families=proposal.affected, lighting_saved=proposals.saves_lighting(proposal.operations),
                lighting_systems=proposals.touched_lighting_systems(proposal.operations))})
            self.store.write_pending(binding, baseline)
        now = self.options.now()
        self.store.write_journal(Journal(
            proposal_id=proposal.id, proposal_type=proposal.proposal_type, binding_id=binding, device=session.identity,
            status="applying", total_operations=len(proposal.operations), completed_operations=0, started_at=now,
            updated_at=now))
        return _ApplyState(proposal=proposal, baseline=baseline, live=session.identity, warnings=list(warnings),
                           packets_before=session.transport.packets["write"], skip_families=skip)

    def _unlock_for(self, session: Session, proposal: Proposal) -> bool:
        """Run the guided physical unlock when the proposal needs it. True if this call unlocked."""
        kb = session.kb
        if not proposal.unlock.required or kb.get_unlock_status() == 1:
            return False
        binding = session.identity.binding_id
        positions = kb.get_unlock_keys()
        self.options.progress("Hold these keys until the keyboard unlocks: "
                              + ", ".join(f"row {r} column {c}" for r, c in positions))
        # Written first: a crash from here on must not lose the fact that Vialtic unlocked it.
        self.store.set_unlock_marker(binding, proposal.id)
        with session.transport.security():
            try:
                kb.unlock_start()
                deadline = time.monotonic() + self.options.unlock_timeout
                while time.monotonic() < deadline:
                    data = kb.unlock_poll()
                    if data and data[0] == 1:
                        self.options.progress("Keyboard unlocked.")
                        return True
                    self.options.progress(f"Unlock progress: {data[2] if len(data) > 2 else '?'} remaining")
                    self.options.sleep(self.options.unlock_poll)
            except KeyboardInterrupt:
                self._abort_unlock(session)
                raise VialticError("cancelled", "Unlock cancelled. Nothing was written. Replug the keyboard: until then the "
                                                "firmware waits for the unlock and ignores other commands.") from None
            except RuntimeError as exc:
                self._abort_unlock(session)
                raise VialticError("device_communication_failed", f"The keyboard stopped responding during unlock: {exc}. "
                                                                  "Nothing was written.",
                                   next_steps=["Replug the keyboard and retry."]) from exc
            self._abort_unlock(session)
        raise VialticError("unlock_timeout", f"The keyboard was not unlocked within {self.options.unlock_timeout:g} seconds. "
                                             "Nothing was written.",
                           next_steps=["Replug the keyboard: until then the firmware waits for the unlock and ignores other commands.",
                                       "Then run apply again and hold the listed keys."])

    def _abort_unlock(self, session: Session) -> None:
        """Best effort; the marker stays unless the keyboard confirms it is locked."""
        try:
            session.kb.lock()
            if session.kb.get_unlock_status() != 1:
                self.store.clear_unlock_marker(session.identity.binding_id)
        except Exception:
            pass

    def _write_and_verify(self, session: Session, state: _ApplyState) -> None:
        proposal = state.proposal
        binding = session.identity.binding_id
        journal = self.store.read_journal(binding)
        try:
            with session.transport.writes():
                for index, op in enumerate(proposal.operations):
                    state.failed_index = index
                    self._write_operation(session, op)
                    state.completed = index + 1
                    journal = journal.model_copy(update=dict(completed_operations=state.completed,
                                                             updated_at=self.options.now()))
                    try:
                        self.store.write_journal(journal)
                    except OSError as exc:
                        raise _WriteFailure(f"Local recovery metadata could not be updated: {exc}.") from exc
            state.failed_index = None
            session.refresh()
            observed = session.snapshot(self.options.now())
            differences = self._differences(state, proposal.operations, observed)
            if differences:
                raise _WriteFailure(f"Read-back does not match the expected result in {len(differences)} place(s).", differences)
            # Last, so wrong values are never made persistent. The firmware does not confirm saves.
            if proposals.saves_lighting(proposal.operations):
                # Recorded first: from here on, a restore has saved values to put back too.
                state.save_attempted = True
                self.store.write_journal(journal.model_copy(update=dict(lighting_save_sent=True, updated_at=self.options.now())))
                with session.transport.writes():
                    session.kb.save_rgb()
                state.warnings.append("Lighting values were verified and the save command was acknowledged. The firmware "
                                      "cannot report whether they reached permanent storage.")
        except (RuntimeError, OSError, VialticError, va.UnlockRequired, struct.error, ValueError, TypeError,
                AttributeError, IndexError, KeyError, OverflowError, AssertionError) as exc:
            message = exc.message if isinstance(exc, VialticError) else str(exc)
            raise _WriteFailure(f"The keyboard stopped responding during the write: {message}") from exc
        finally:
            state.packets_total = session.transport.packets["write"] - state.packets_before

    @staticmethod
    def _differences(state: _ApplyState, operations: list[Operation], observed: Snapshot) -> list[VerificationDifference]:
        differences = proposals.compare_state(state.baseline.configuration, operations, observed.configuration,
                                              state.skip_families, proposals.touched_lighting_systems(state.proposal.operations))
        # Families a recovery leaves alone must at least not have moved since it was planned.
        expected = state.proposal.baseline.family_fingerprints
        differences += [VerificationDifference(family=family, target=family) for family in state.skip_families
                        if observed.family_fingerprints.get(family) != expected.get(family)]
        return differences

    @staticmethod
    def _write_operation(session: Session, op: Operation) -> None:
        va.write_operation(session.kb, op)

    def _commit(self, state: _ApplyState) -> None:
        """Rotate the backup. Local failures here cannot undo what the keyboard verified."""
        state.committed = True
        binding = state.proposal.device.binding_id
        try:
            try:
                journal = self.store.read_journal(binding)
                if journal is not None:
                    self.store.write_journal(journal.model_copy(update=dict(status="verified", updated_at=self.options.now())))
            except OSError as exc:
                # A full disk can refuse this update yet still allow the renames that follow.
                state.warnings.append(f"Recovery metadata could not be updated: {exc}.")
            self.store.finish_write(binding, state.proposal.proposal_type)
        except (OSError, VialticError) as exc:
            state.warnings.append(f"The change is on the keyboard and verified, but local files could not be updated: {exc}. "
                                  "The saved state was kept.")

    def _finish_success(self, session: Session, state: _ApplyState) -> ApplyResult:
        self._commit(state)
        self._relock(session, state)
        return self._verified_result(state, session.identity)

    def _committed_after_interrupt(self, state: _ApplyState) -> ApplyResult:
        self._commit(state)
        self._relock_fresh(state)
        state.warnings.append("Interrupted after the change was verified; it stays on the keyboard.")
        return self._verified_result(state, state.live)

    def _verified_result(self, state: _ApplyState, device: DeviceIdentity) -> ApplyResult:
        proposal = state.proposal
        self.store.delete_proposal(proposal.id)
        return ApplyResult(
            proposal_id=proposal.id, binding_id=proposal.device.binding_id, device=device, outcome="verified",
            message=f"Applied and verified {len(proposal.operations)} operation(s).",
            operations=self._operation_results(state),
            backup_path=self._existing(self.store.previous_path(proposal.device.binding_id)),
            write_packets_sent=state.packets_total, relocked=state.relocked, relock_error=state.relock_error,
            warnings=state.warnings)

    def _relock(self, session: Session, state: _ApplyState) -> None:
        """Lock again only if Vialtic unlocked, leaving a preexisting unlock untouched."""
        if not state.needs_relock:
            return
        try:
            with session.transport.security():
                session.kb.lock()
            locked = session.kb.get_unlock_status() != 1
        except Exception as exc:
            state.relocked, state.relock_error = False, f"Could not lock the keyboard again: {exc}. Run `vialtic lock` or replug it."
            return
        state.relocked = locked
        if locked:
            state.relock_error = None
            self.store.clear_unlock_marker(state.proposal.device.binding_id)
        else:
            state.relock_error = "The keyboard still reports unlocked. Run `vialtic lock` or replug it."

    def _relock_fresh(self, state: _ApplyState) -> None:
        """Best-effort lock after the writing session ended."""
        if not state.needs_relock:
            return
        try:
            entries, _, by_id = self.devices.list_devices()
            for entry in entries:
                if entry.identity.path == state.live.path and entry.identity.binding_id == state.live.binding_id:
                    with self.devices.session(by_id[entry.identity.device_id]) as session:
                        self._relock(session, state)
                    return
            state.relocked, state.relock_error = False, "The keyboard could not be reached to lock it. Replugging locks it."
        except (VialticError, RuntimeError, OSError) as exc:
            state.relocked, state.relock_error = False, f"Could not lock the keyboard again: {exc}. Run `vialtic lock` or replug it."

    @staticmethod
    def _operation_results(state: _ApplyState) -> list[OperationResult]:
        results = []
        for index, op in enumerate(state.proposal.operations):
            status = "applied" if index < state.completed else ("failed" if index == state.failed_index else "not_attempted")
            results.append(OperationResult(index=index, summary=proposals.describe_operation(op), status=status))
        return results

    # Failure recovery. Disclosed in the proposal, so it needs no second approval.

    def _recover_after_failure(self, state: _ApplyState | None, failure: _WriteFailure) -> ApplyResult:
        assert state is not None
        proposal = state.proposal
        binding = proposal.device.binding_id
        self.options.progress(f"Write failed: {failure.message} Trying to restore the saved state...")
        self._mark_needs_recovery(state, failure.message)

        restored, reason, last_observed = self._try_restore(state)
        # The write ran, so its approval is spent, whatever the outcome.
        self.store.delete_proposal(proposal.id)
        if restored:
            with contextlib.suppress(OSError):
                self.store.discard_pending(binding)
                self.store.clear_journal(binding)
            return ApplyResult(
                proposal_id=proposal.id, binding_id=binding, device=state.live, outcome="failed_restored",
                message=f"{failure.message} The saved state was restored and verified, so the keyboard is as it was before.",
                operations=self._operation_results(state), differences=failure.differences,
                backup_path=self._existing(self.store.previous_path(binding)), write_packets_sent=state.packets_total,
                relocked=state.relocked, relock_error=state.relock_error, warnings=state.warnings)

        # Keep the good backup. Say only what is known about the keyboard.
        self._relock_fresh(state)
        outcome = "partial" if last_observed is not None else "unknown"
        differences = failure.differences
        if last_observed is not None:
            differences = self._differences(state, [], last_observed)
        return ApplyResult(
            proposal_id=proposal.id, binding_id=binding, device=state.live, outcome=outcome,
            message=f"{failure.message} Automatic restore did not complete: {reason} The keyboard may hold a mix of old "
                    "and new values. The saved state was kept.",
            operations=self._operation_results(state), differences=differences,
            backup_path=self._existing(self.store.pending_path(binding)), write_packets_sent=state.packets_total,
            relocked=state.relocked, relock_error=state.relock_error, warnings=state.warnings,
            recovery_steps=["Reconnect the keyboard if needed and close other keyboard software.",
                            "Run `vialtic recover plan --json`, show the user the result and get approval.",
                            "Run `vialtic apply <new-proposal-id> --user-approved`."])

    def _mark_needs_recovery(self, state: _ApplyState, note: str) -> None:
        """A local failure here must not stop the restore that follows."""
        binding = state.proposal.device.binding_id
        try:
            journal = self.store.read_journal(binding)
            if journal is not None:
                self.store.write_journal(journal.model_copy(update=dict(
                    status="needs_recovery", completed_operations=state.completed, updated_at=self.options.now(), note=note)))
        except (OSError, VialticError) as exc:
            state.warnings.append(f"Recovery metadata could not be updated: {exc}. The saved state is what matters and was kept.")

    @staticmethod
    def _existing(path) -> str | None:
        return str(path) if path.exists() else None

    def _try_restore(self, state: _ApplyState) -> tuple[bool, str, Snapshot | None]:
        """Restore the baseline within the reconnect window. Stops early on anything needing a person."""
        live = state.live
        target = state.baseline.configuration
        systems = proposals.touched_lighting_systems(state.proposal.operations)
        # Saving only matters if the failed write may have saved; otherwise the saved values never moved.
        must_save = state.save_attempted
        deadline = time.monotonic() + min(state.proposal.reconnect_window_seconds, self.options.reconnect_window)
        last_observed: Snapshot | None = None
        reason = "the keyboard did not come back in time."
        while True:
            try:
                entries, _, by_id = self.devices.list_devices()
            except VialticError as exc:
                return False, exc.message, last_observed
            matches = [e for e in entries if e.identity.binding_id == live.binding_id]
            if len(matches) > 1:
                return False, "several connected keyboards match its identity.", last_observed
            if any(e.status == "permission_denied" and e.identity.path == live.path for e in entries):
                return False, "permission to open it was lost.", last_observed
            if matches and matches[0].status == "ready":
                entry = matches[0]
                # Without a real serial, only the same path is evidence of the same unit.
                if live.identity_confidence != "usb_serial" and entry.identity.path != live.path:
                    return False, ("a keyboard with the same identity is at a different USB path, and it cannot be "
                                   "confirmed to be the same unit."), last_observed
                try:
                    with self.devices.session(by_id[entry.identity.device_id]) as session:
                        if session.identity.binding_id != live.binding_id:
                            return False, "the keyboard's identity changed.", last_observed
                        last_observed = session.snapshot(self.options.now())
                        if proposals.unexplained_values(target, state.proposal.operations, last_observed.configuration):
                            return False, ("it holds values that neither the saved state nor this write explain, so it "
                                           "may be a different unit or another application changed it."), last_observed
                        operations, blockers, _ = proposals.restore_operations(
                            last_observed.configuration, target, lighting_save=must_save, lighting_systems=systems)
                        blockers = [b for b in blockers if b not in state.skip_families]
                        if blockers:
                            return False, f"{', '.join(blockers)} changed and cannot be restored by this build.", last_observed
                        if proposals.requires_unlock(operations) and session.kb.get_unlock_status() != 1:
                            return False, "restoring it requires a physical unlock.", last_observed
                        with session.transport.writes():
                            for op in operations:
                                self._write_operation(session, op)
                        state.packets_total += session.transport.packets["write"]
                        session.refresh()
                        last_observed = session.snapshot(self.options.now())
                        remaining = self._differences(state, [], last_observed)
                        if not remaining:
                            if must_save:
                                with session.transport.writes():
                                    session.kb.save_rgb()
                                must_save = False
                            self._relock(session, state)
                            return True, "", last_observed
                        reason = f"{len(remaining)} value(s) still differ after restoring."
                except (VialticError, RuntimeError, OSError, va.UnlockRequired) as exc:
                    reason = f"{exc.message if isinstance(exc, VialticError) else exc}."
            if time.monotonic() >= deadline:
                return False, reason, last_observed
            self.options.sleep(self.options.reconnect_poll)

    # Security commands.

    def unlock_status(self, selector: str | None) -> UnlockStatus:
        # Reading the configuration is not possible during an unlock, and not needed here.
        with self.devices.security_session(self.devices.select_interface(selector)) as session:
            return self._unlock_status(session)

    def unlock_start(self, proposal_id: str, attestation: ApprovalAttestation | None, selector: str | None) -> UnlockStatus:
        """Begin the physical unlock for an approved proposal that requires one."""
        proposal = self._load_proposal(proposal_id)
        self._check_approval(proposal, attestation)
        if not proposal.unlock.required:
            raise VialticError("invalid_request", f"Proposal {proposal_id} does not need a physical unlock.")
        desc, _ = self._locate_for_write(proposal.device, selector)
        with self.devices.session(desc) as session:
            self._check_live_identity(session, proposal)
            self._check_baseline(session.snapshot(self.options.now()), proposal)
            status = self._unlock_status(session)
            if status.unlocked:
                return status
            self.store.set_unlock_marker(session.identity.binding_id, proposal.id)
            with session.transport.security():
                session.kb.unlock_start()
            return self._unlock_status(session)

    def unlock_poll(self, selector: str | None) -> UnlockStatus:
        with self.devices.security_session(self.devices.select_interface(selector)) as session:
            status = self._unlock_status(session)
            with session.transport.security():
                data = session.kb.unlock_poll()
            return status.model_copy(update=dict(unlocked=bool(data and data[0] == 1),
                                                 in_progress=bool(len(data) > 1 and data[1] == 1),
                                                 counter=data[2] if len(data) > 2 else None))

    def lock(self, selector: str | None) -> UnlockStatus:
        entry, desc, _ = self.devices.select(selector)
        with self.devices.session(desc) as session:
            with session.transport.security():
                session.kb.lock()
            status = self._unlock_status(session)
            if not status.unlocked:
                self.store.clear_unlock_marker(session.identity.binding_id)
            return status


def attestation_for(proposal: Proposal, now: datetime) -> ApprovalAttestation:
    return ApprovalAttestation(proposal_id=proposal.id, binding_id=proposal.device.binding_id, attested_at=now)
