# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable proposals: validation, content-derived ids, diffs and baseline checks."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Iterable

from . import vial_adapter as va
from .device import Session, canonical_json
from .errors import VialticError
from .models import (
    Baseline, ChangeSet, Configuration, DeviceIdentity, EncoderEntry, KeycodeValue, KeymapEntry, Operation, Proposal,
    ProposalType, ProposalUnlock, SetEncoderChange, SetEncoderOperation, SetKeyChange, SetKeyOperation, Snapshot,
    UnlockPosition, VerificationDifference,
)

DEFAULT_TTL = timedelta(minutes=30)
ID_PREFIX = "pr-"

VERIFICATION_TEXT = ("After writing, every configuration family except transient lighting is read back from the "
                     "keyboard and compared with the expected result.")
RECOVERY_TEXT = ("Everything Vialtic can read from the keyboard is saved before the first write; any exceptions are "
                 "listed in the warnings. If a write or the read-back fails, Vialtic tries to restore that saved state "
                 "within the reconnect window, without further approval. If it cannot, the backup is kept and the exact "
                 "recovery command is reported.")


def _value(entry: KeymapEntry | EncoderEntry) -> KeycodeValue:
    return KeycodeValue(raw=entry.raw, keycode=entry.keycode, label=entry.label)


def validate_changes(session: Session, snapshot: Snapshot, changes: ChangeSet) -> tuple[list[Operation], list[str]]:
    """Turn requested changes into typed operations, or refuse the whole request."""
    kb = session.kb
    keys = {(e.layer, e.row, e.col): e for e in snapshot.configuration.keymap}
    encoders = {(e.layer, e.index, e.direction): e for e in snapshot.configuration.encoders}
    operations: list[Operation] = []
    warnings: list[str] = []
    seen: set[tuple] = set()

    for change in changes.changes:
        if not 0 <= change.layer < kb.layers:
            raise VialticError("invalid_coordinate", f"Layer {change.layer} does not exist; this keyboard has layers "
                                                     f"0-{kb.layers - 1}.")
        if isinstance(change, SetKeyChange):
            target = ("key", change.layer, change.row, change.col)
            entry = keys.get((change.layer, change.row, change.col))
            if entry is None:
                raise VialticError("invalid_coordinate", f"Layer {change.layer} has no key at row {change.row}, column "
                                                         f"{change.col}.",
                                   next_steps=["Run `vialtic view` to see valid positions."])
        else:
            assert isinstance(change, SetEncoderChange)
            target = ("encoder", change.layer, change.index, change.direction)
            entry = encoders.get((change.layer, change.index, change.direction))
            if entry is None:
                raise VialticError("invalid_coordinate", f"Layer {change.layer} has no encoder {change.index} direction "
                                                         f"{change.direction}.")
        if target in seen:
            raise VialticError("invalid_request", f"The request changes {target[0]} {target[1:]} more than once.")
        seen.add(target)

        after, notes = va.parse_keycode(kb, change.keycode)
        warnings.extend(n for n in notes if n not in warnings)
        before = _value(entry)
        if before.raw == after.raw:
            warnings.append(f"Skipped {_describe_target(change)}: it is already {after.keycode}.")
            continue
        if isinstance(change, SetKeyChange):
            operations.append(SetKeyOperation(layer=change.layer, row=change.row, col=change.col, before=before, after=after))
        else:
            operations.append(SetEncoderOperation(layer=change.layer, index=change.index, direction=change.direction,
                                                  before=before, after=after))
    return operations, warnings


def _describe_target(item) -> str:
    if item.type == "set_key":
        return f"layer {item.layer} key {item.row},{item.col}"
    return f"layer {item.layer} encoder {item.index} direction {item.direction}"


def describe_operation(op: Operation) -> str:
    return f"{_describe_target(op)}: {op.before.keycode} -> {op.after.keycode}"


def restore_operations(current: Configuration, target: Configuration) -> tuple[list[Operation], list[str], list[str]]:
    """Operations turning current into target. Also returns unrestorable families and notes."""
    ops: list[Operation] = []
    now_keys = {(e.layer, e.row, e.col): e for e in current.keymap}
    for wanted in target.keymap:
        have = now_keys.get((wanted.layer, wanted.row, wanted.col))
        if have is not None and have.raw != wanted.raw:
            ops.append(SetKeyOperation(layer=wanted.layer, row=wanted.row, col=wanted.col, before=_value(have),
                                       after=_value(wanted)))
    now_encoders = {(e.layer, e.index, e.direction): e for e in current.encoders}
    for wanted in target.encoders:
        have = now_encoders.get((wanted.layer, wanted.index, wanted.direction))
        if have is not None and have.raw != wanted.raw:
            ops.append(SetEncoderOperation(layer=wanted.layer, index=wanted.index, direction=wanted.direction,
                                           before=_value(have), after=_value(wanted)))

    now_data, target_data = current.model_dump(mode="json"), target.model_dump(mode="json")
    blockers = [f for f in va.CHECKED_FAMILIES
                if f not in va.RESTORABLE_FAMILIES and now_data[f] != target_data[f]]
    notes = []
    if now_data["lighting"] != target_data["lighting"]:
        notes.append("Lighting differs from the saved state and will not be restored: this build cannot write lighting yet.")
    return ops, blockers, notes


def affected_families(operations: Iterable[Operation]) -> list[str]:
    families = {"keymap" if op.type == "set_key" else "encoders" for op in operations}
    return sorted(families)


def proposal_id(fields: dict) -> str:
    """Derive the id from canonical content, so any edit to the stored file is detectable."""
    digest = hashlib.sha256(canonical_json(fields).encode("ascii")).hexdigest()
    return ID_PREFIX + digest[:20]


def build_proposal(proposal_type: ProposalType, device: DeviceIdentity, snapshot: Snapshot,
                   operations: list[Operation], warnings: list[str], positions: list[tuple[int, int]],
                   now: datetime, ttl: timedelta, reconnect_window: float) -> Proposal:
    required = any(op.after.keycode == va.RESET_KEYCODE for op in operations)
    fields = dict(
        schema_version=1, kind="proposal", proposal_type=proposal_type, device=device,
        baseline=Baseline(family_fingerprints={f: snapshot.family_fingerprints[f] for f in va.CHECKED_FAMILIES},
                          checked_families=list(va.CHECKED_FAMILIES)),
        operations=operations, affected=affected_families(operations), warnings=warnings,
        unlock=ProposalUnlock(required=required,
                              positions=[UnlockPosition(row=r, col=c) for r, c in positions] if required else []),
        verification=VERIFICATION_TEXT, recovery=RECOVERY_TEXT, reconnect_window_seconds=reconnect_window,
        created_at=now, expires_at=now + ttl)
    draft = Proposal(id="pending", **fields)
    return draft.model_copy(update={"id": proposal_id(_content(draft))})


def _content(proposal: Proposal) -> dict:
    data = proposal.model_dump(mode="json")
    data.pop("id")
    return data


def check_integrity(proposal: Proposal) -> None:
    if proposal_id(_content(proposal)) != proposal.id:
        raise VialticError("proposal_tampered", f"Stored proposal {proposal.id} no longer matches its id; it was edited.",
                           next_steps=["Create a new proposal and get it approved."])


def expected_after(baseline: Configuration, operations: Iterable[Operation]) -> dict[str, dict]:
    """The keymap and encoder values the keyboard must hold once operations are applied."""
    keys = {f"{e.layer}:{e.row},{e.col}": e.raw for e in baseline.keymap}
    encoders = {f"{e.layer}:{e.index}:{e.direction}": e.raw for e in baseline.encoders}
    for op in operations:
        if op.type == "set_key":
            keys[f"{op.layer}:{op.row},{op.col}"] = op.after.raw
        else:
            encoders[f"{op.layer}:{op.index}:{op.direction}"] = op.after.raw
    return {"keymap": keys, "encoders": encoders}


def unexplained_values(baseline: Configuration, operations: Iterable[Operation],
                       observed: Configuration) -> list[VerificationDifference]:
    """Keymap and encoder values neither the baseline nor this write can account for.

    A keyboard holding any is not provably the unit that was being written, or another
    application changed it during the write. Either way it must not be overwritten.
    """
    allowed: dict[str, set[int]] = {}
    for e in baseline.keymap:
        allowed[f"key {e.layer}:{e.row},{e.col}"] = {e.raw}
    for e in baseline.encoders:
        allowed[f"encoder {e.layer}:{e.index}:{e.direction}"] = {e.raw}
    for op in operations:
        target = (f"key {op.layer}:{op.row},{op.col}" if op.type == "set_key"
                  else f"encoder {op.layer}:{op.index}:{op.direction}")
        allowed.setdefault(target, set()).update({op.before.raw, op.after.raw})
    found = [(f"key {e.layer}:{e.row},{e.col}", "keymap", e.raw) for e in observed.keymap] + \
            [(f"encoder {e.layer}:{e.index}:{e.direction}", "encoders", e.raw) for e in observed.encoders]
    return [VerificationDifference(family=family, target=target, observed=raw)
            for target, family, raw in found if raw not in allowed.get(target, set())]


def compare_state(baseline: Configuration, operations: Iterable[Operation], observed: Configuration,
                  skip_families: Iterable[str] = ()) -> list[VerificationDifference]:
    """Differences between the observed configuration and baseline plus operations."""
    expected = expected_after(baseline, operations)
    diffs: list[VerificationDifference] = []
    seen_keys = {f"{e.layer}:{e.row},{e.col}": e.raw for e in observed.keymap}
    for target, raw in expected["keymap"].items():
        if seen_keys.get(target) != raw:
            diffs.append(VerificationDifference(family="keymap", target=target, expected=raw, observed=seen_keys.get(target)))
    seen_encoders = {f"{e.layer}:{e.index}:{e.direction}": e.raw for e in observed.encoders}
    for target, raw in expected["encoders"].items():
        if seen_encoders.get(target) != raw:
            diffs.append(VerificationDifference(family="encoders", target=target, expected=raw,
                                                observed=seen_encoders.get(target)))
    base, seen = baseline.model_dump(mode="json"), observed.model_dump(mode="json")
    for family in va.CHECKED_FAMILIES:
        # Values stay out of these entries: macro contents must not reach output by default.
        if family not in va.RESTORABLE_FAMILIES and family not in skip_families and base[family] != seen[family]:
            diffs.append(VerificationDifference(family=family, target=family))
    return diffs


def format_proposal(proposal: Proposal) -> str:
    lines = [f"Proposal {proposal.id} ({proposal.proposal_type})",
             f"Keyboard: {proposal.device.name} [{proposal.device.device_id}]"
             + (" SIMULATED" if proposal.device.simulated else ""),
             f"Expires: {proposal.expires_at.isoformat()}", ""]
    if proposal.operations:
        lines.append(f"Changes ({len(proposal.operations)}):")
        lines.extend(f"  {describe_operation(op)}" for op in proposal.operations)
    else:
        lines.append("Changes: none; the keyboard already matches the target.")
    if proposal.unlock.required:
        positions = ", ".join(f"{p.row},{p.col}" for p in proposal.unlock.positions)
        lines += ["", f"Physical unlock required. Hold these keys when asked: {positions}"]
    if proposal.warnings:
        lines += ["", "Warnings:"] + [f"  {w}" for w in proposal.warnings]
    lines += ["", f"Verification: {proposal.verification}", f"Recovery: {proposal.recovery}"]
    return "\n".join(lines)
