# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable proposals: validation, content-derived ids, diffs and baseline checks."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import vial_adapter as va
from .device import Session, canonical_json
from .errors import VialticError
from .models import (
    Baseline, Change, ComboValue, Configuration, SetComboChange, SetComboOperation, TapDanceValue, DeviceIdentity, KeycodeValue, KeymapEntry, EncoderEntry, Operation, Proposal,
    ProposalType, ProposalUnlock, SetEncoderChange, SetEncoderOperation, SetKeyChange, SetKeyOperation,
    SetLayoutOptionChange, SetLayoutOptionsOperation, SetTapDanceChange, SetTapDanceOperation, Snapshot, UnlockPosition,
    VerificationDifference,
)

DEFAULT_TTL = timedelta(minutes=30)
ID_PREFIX = "pr-"

VERIFICATION_TEXT = ("After writing, every configuration family is read back from the keyboard and compared with the "
                     "expected result. Lighting is included only when this proposal changes it, because lighting keys "
                     "can change it at any moment.")
RECOVERY_TEXT = ("Everything Vialtic can read from the keyboard is saved before the first write; any exceptions are "
                 "listed in the warnings. If a write or the read-back fails, Vialtic tries to restore that saved state "
                 "within the reconnect window, without further approval. If it cannot, the backup is kept and the exact "
                 "recovery command is reported.")

FAMILY_OF_OPERATION = {"set_key": "keymap", "set_encoder": "encoders", "set_layout_options": "layout_options",
                       "set_tap_dance": "tap_dance", "set_combo": "combos"}
U16 = 0xFFFF


def _value(entry: KeymapEntry | EncoderEntry) -> KeycodeValue:
    return KeycodeValue(raw=entry.raw, keycode=entry.keycode, label=entry.label)


def after_keycodes(op: Operation) -> list[KeycodeValue]:
    """Every keycode an operation writes, including ones an entry merely keeps."""
    if op.type in ("set_key", "set_encoder"):
        return [op.after]
    if op.type == "set_tap_dance":
        return [op.after.on_tap, op.after.on_hold, op.after.on_double_tap, op.after.on_tap_hold]
    if op.type == "set_combo":
        return [*op.after.keys, op.after.output]
    return []


def requires_unlock(operations: Iterable[Operation]) -> bool:
    """Whether the firmware will only accept these writes while physically unlocked."""
    return any(value.keycode == va.RESET_KEYCODE for op in operations for value in after_keycodes(op))


def _capability(present: object, family: str, description: str) -> None:
    if not present:
        raise VialticError("capability_unavailable", f"This keyboard's firmware does not expose {description}.",
                           details={"family": family}, next_steps=["Run `vialtic device inspect` to see what it exposes."])


class _Planner:
    """Turns one request into typed operations, or refuses all of it."""

    def __init__(self, session: Session, snapshot: Snapshot, quiet: bool):
        self.kb = session.kb
        self.config = snapshot.configuration
        self.keys = {(e.layer, e.row, e.col): e for e in self.config.keymap}
        self.encoders = {(e.layer, e.index, e.direction): e for e in self.config.encoders}
        self.operations: list[Operation] = []
        self.warnings: list[str] = []
        self.quiet = quiet
        self.seen: set[tuple] = set()
        self.layout_value: int | None = None

    def _once(self, target: tuple, description: str) -> None:
        if target in self.seen:
            raise VialticError("invalid_request", f"The request changes {description} more than once.")
        self.seen.add(target)

    def _keycode(self, text: str) -> KeycodeValue:
        value, notes = va.parse_keycode(self.kb, text)
        self.warnings.extend(n for n in notes if n not in self.warnings)
        return value

    def _skip(self, description: str) -> None:
        if not self.quiet:
            self.warnings.append(f"Skipped {description}: it already has that value.")

    def _index(self, index: int, count: int, name: str) -> None:
        if not 0 <= index < count:
            raise VialticError("invalid_coordinate", f"{name} {index} does not exist; this keyboard has {count}.")

    def add(self, change: Change) -> None:
        getattr(self, "_" + change.type)(change)

    def _layer(self, layer: int) -> None:
        if not 0 <= layer < self.kb.layers:
            raise VialticError("invalid_coordinate", f"Layer {layer} does not exist; this keyboard has layers "
                                                     f"0-{self.kb.layers - 1}.")

    def _set_key(self, change: SetKeyChange) -> None:
        self._layer(change.layer)
        entry = self.keys.get((change.layer, change.row, change.col))
        if entry is None:
            raise VialticError("invalid_coordinate", f"Layer {change.layer} has no key at row {change.row}, column "
                                                     f"{change.col}.",
                               next_steps=["Run `vialtic view` to see valid positions."])
        self._once(("key", change.layer, change.row, change.col), f"key {change.layer}:{change.row},{change.col}")
        after = self._keycode(change.keycode)
        if entry.raw == after.raw:
            return self._skip(f"layer {change.layer} key {change.row},{change.col}")
        self.operations.append(SetKeyOperation(layer=change.layer, row=change.row, col=change.col, before=_value(entry),
                                               after=after))

    def _set_encoder(self, change: SetEncoderChange) -> None:
        self._layer(change.layer)
        entry = self.encoders.get((change.layer, change.index, change.direction))
        if entry is None:
            _capability(self.kb.encoder_count, "encoders", "encoders")
            raise VialticError("invalid_coordinate", f"Layer {change.layer} has no encoder {change.index} direction "
                                                     f"{change.direction}.")
        self._once(("encoder", change.layer, change.index, change.direction),
                   f"encoder {change.layer}:{change.index}:{change.direction}")
        after = self._keycode(change.keycode)
        if entry.raw == after.raw:
            return self._skip(f"layer {change.layer} encoder {change.index} direction {change.direction}")
        self.operations.append(SetEncoderOperation(layer=change.layer, index=change.index, direction=change.direction,
                                                   before=_value(entry), after=after))

    def _set_layout_option(self, change: SetLayoutOptionChange) -> None:
        labels = self.kb.layout_labels
        _capability(labels, "layout_options", "layout options")
        self._index(change.index, len(labels), "Layout option")
        self._once(("layout_option", change.index), f"layout option {change.index}")
        count = va.layout_option_count(labels[change.index])
        if not 0 <= change.choice < count:
            raise VialticError("invalid_request", f"Layout option {change.index} has choices 0-{count - 1}, not "
                                                  f"{change.choice}.")
        current = self.config.layout_options if self.layout_value is None else self.layout_value
        self.layout_value = va.set_layout_choice(labels, current, change.index, change.choice)

    def _merged_keycode(self, text: str | None, current: KeycodeValue) -> KeycodeValue:
        """A field left out keeps its value, even one a request could not set itself."""
        return current if text is None else self._keycode(text)

    def _bounded(self, value: int | None, current: int, limit: int, what: str) -> int:
        if value is None:
            return current
        if isinstance(value, bool) or not 0 <= value <= limit:
            raise VialticError("invalid_request", f"{what} must be between 0 and {limit}, not {value}.")
        return value

    def _set_tap_dance(self, change: SetTapDanceChange) -> None:
        _capability(self.kb.tap_dance_count, "tap_dance", "tap dance")
        self._index(change.index, self.kb.tap_dance_count, "Tap dance entry")
        self._once(("tap_dance", change.index), f"tap dance {change.index}")
        before = va.tap_dance_value(self.config.tap_dance[change.index])
        after = TapDanceValue(on_tap=self._merged_keycode(change.on_tap, before.on_tap),
                              on_hold=self._merged_keycode(change.on_hold, before.on_hold),
                              on_double_tap=self._merged_keycode(change.on_double_tap, before.on_double_tap),
                              on_tap_hold=self._merged_keycode(change.on_tap_hold, before.on_tap_hold),
                              tapping_term=self._bounded(change.tapping_term, before.tapping_term, U16, "The tapping term"))
        if after == before:
            return self._skip(f"tap dance {change.index}")
        self.operations.append(SetTapDanceOperation(index=change.index, before=before, after=after))

    def _set_combo(self, change: SetComboChange) -> None:
        _capability(self.kb.combo_count, "combos", "combos")
        self._index(change.index, self.kb.combo_count, "Combo")
        self._once(("combo", change.index), f"combo {change.index}")
        before = va.combo_value(self.config.combos[change.index])
        keys = before.keys
        if change.keys is not None:
            keys = [self._keycode(text) for text in change.keys]
            keys += [va.to_value("KC_NO")] * (4 - len(keys))
        after = ComboValue(keys=keys, output=self._merged_keycode(change.output, before.output))
        if after == before:
            return self._skip(f"combo {change.index}")
        raws = [key.raw for key in keys]
        if 0 in raws and any(raws[raws.index(0):]):
            self.warnings.append(f"Combo {change.index} has KC_NO before another input. Firmware stops reading inputs at "
                                 "the first KC_NO, so the ones after it may be ignored.")
        self.operations.append(SetComboOperation(index=change.index, before=before, after=after))

    def finish(self) -> tuple[list[Operation], list[str]]:
        before = self.config.layout_options
        if self.layout_value is not None:
            if self.layout_value == before:
                self._skip("layout options")
            else:
                labels = self.kb.layout_labels
                self.operations.append(SetLayoutOptionsOperation(
                    before=before, after=self.layout_value, before_choices=va.layout_choices(labels, before),
                    after_choices=va.layout_choices(labels, self.layout_value)))
        return self.operations, self.warnings


def validate_changes(session: Session, snapshot: Snapshot, changes: Iterable[Change],
                     quiet: bool = False) -> tuple[list[Operation], list[str]]:
    """Turn requested changes into typed operations, or refuse the whole request."""
    planner = _Planner(session, snapshot, quiet)
    for change in changes:
        planner.add(change)
    return planner.finish()


def describe_operation(op: Operation) -> str:
    """One line for people. Macro contents never appear here."""
    if op.type == "set_key":
        return f"layer {op.layer} key {op.row},{op.col}: {op.before.keycode} -> {op.after.keycode}"
    if op.type == "set_encoder":
        return f"layer {op.layer} encoder {op.index} direction {op.direction}: {op.before.keycode} -> {op.after.keycode}"
    if op.type == "set_layout_options":
        return f"layout options: {op.before} {op.before_choices} -> {op.after} {op.after_choices}"
    if op.type == "set_tap_dance":
        return f"tap dance {op.index}: {_changed_fields(op.before, op.after)}"
    if op.type == "set_combo":
        keys = [key.keycode for key in op.before.keys], [key.keycode for key in op.after.keys]
        return f"combo {op.index}: {keys[0]} => {op.before.output.keycode} -> {keys[1]} => {op.after.output.keycode}"
    raise VialticError("unsupported_operation", f"Unknown operation type {op.type}.")


def _changed_fields(before: Any, after: Any) -> str:
    """Field by field differences of an entry, showing keycodes by name."""
    def shown(value: Any) -> Any:
        return value.keycode if isinstance(value, KeycodeValue) else value

    parts = [f"{name} {shown(getattr(before, name))} -> {shown(getattr(after, name))}"
             for name in type(before).model_fields if getattr(before, name) != getattr(after, name)]
    return ", ".join(parts) or "no field changes"


# Comparable state. Values are what each family reads back as: raw codes for keys and encoders,
# canonical names for entries. Both come from upstream's lossless codec.

def family_state(config: Configuration) -> dict[str, dict[str, Any]]:
    return {
        "keymap": {f"{e.layer}:{e.row},{e.col}": e.raw for e in config.keymap},
        "encoders": {f"{e.layer}:{e.index}:{e.direction}": e.raw for e in config.encoders},
        "layout_options": {"value": config.layout_options},
        "tap_dance": {str(i): list(e) for i, e in enumerate(config.tap_dance)},
        "combos": {str(i): list(e) for i, e in enumerate(config.combos)},
    }


def operation_effect(op: Operation) -> tuple[str, str, Any, Any]:
    """The family, target, prior value and new value of one operation."""
    if op.type == "set_key":
        return "keymap", f"{op.layer}:{op.row},{op.col}", op.before.raw, op.after.raw
    if op.type == "set_encoder":
        return "encoders", f"{op.layer}:{op.index}:{op.direction}", op.before.raw, op.after.raw
    if op.type == "set_layout_options":
        return "layout_options", "value", op.before, op.after
    if op.type == "set_tap_dance":
        return "tap_dance", str(op.index), list(va.tap_dance_wire(op.before)), list(va.tap_dance_wire(op.after))
    if op.type == "set_combo":
        return "combos", str(op.index), list(va.combo_wire(op.before)), list(va.combo_wire(op.after))
    raise VialticError("unsupported_operation", f"Unknown operation type {op.type}.")


def affected_families(operations: Iterable[Operation]) -> list[str]:
    return sorted({FAMILY_OF_OPERATION[op.type] for op in operations})


def restore_blockers(current: Configuration, target: Configuration) -> list[str]:
    """Families whose shape changed, which happens when firmware was rebuilt with the same definition."""
    have, want = family_state(current), family_state(target)
    return [family for family in want if set(want[family]) - set(have[family])]


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
    blockers = restore_blockers(current, target)
    if "layout_options" not in blockers and current.layout_options != target.layout_options:
        ops.append(SetLayoutOptionsOperation(before=current.layout_options, after=target.layout_options))
    if "tap_dance" not in blockers:
        for index, (have_td, want_td) in enumerate(zip(current.tap_dance, target.tap_dance)):
            if list(have_td) != list(want_td):
                ops.append(SetTapDanceOperation(index=index, before=va.tap_dance_value(have_td),
                                                after=va.tap_dance_value(want_td)))
    if "combos" not in blockers:
        for index, (have_combo, want_combo) in enumerate(zip(current.combos, target.combos)):
            if list(have_combo) != list(want_combo):
                ops.append(SetComboOperation(index=index, before=va.combo_value(have_combo), after=va.combo_value(want_combo)))

    now_data, target_data = current.model_dump(mode="json"), target.model_dump(mode="json")
    blockers += [f for f in va.CHECKED_FAMILIES
                 if f not in va.RESTORABLE_FAMILIES and f not in blockers and now_data[f] != target_data[f]]
    notes = []
    if now_data["lighting"] != target_data["lighting"]:
        notes.append("Lighting differs from the saved state and will not be restored: this build cannot write lighting yet.")
    return ops, blockers, notes


def proposal_id(fields: dict) -> str:
    """Derive the id from canonical content, so any edit to the stored file is detectable."""
    digest = hashlib.sha256(canonical_json(fields).encode("ascii")).hexdigest()
    return ID_PREFIX + digest[:20]


def build_proposal(proposal_type: ProposalType, device: DeviceIdentity, snapshot: Snapshot,
                   operations: list[Operation], warnings: list[str], positions: list[tuple[int, int]],
                   now: datetime, ttl: timedelta, reconnect_window: float) -> Proposal:
    required = requires_unlock(operations)
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


def expected_after(baseline: Configuration, operations: Iterable[Operation]) -> dict[str, dict[str, Any]]:
    """The values each family must hold once operations are applied."""
    state = family_state(baseline)
    for op in operations:
        family, target, _, after = operation_effect(op)
        state.setdefault(family, {})[target] = after
    return state


def unexplained_values(baseline: Configuration, operations: Iterable[Operation],
                       observed: Configuration) -> list[VerificationDifference]:
    """Values neither the baseline nor this write can account for.

    A keyboard holding any is not provably the unit that was being written, or another
    application changed it during the write. Either way it must not be overwritten.
    """
    allowed = {family: {target: [value] for target, value in values.items()}
               for family, values in family_state(baseline).items()}
    for op in operations:
        family, target, before, after = operation_effect(op)
        allowed.setdefault(family, {}).setdefault(target, []).extend([before, after])
    return [VerificationDifference(family=family, target=target, observed=value)
            for family, values in family_state(observed).items() for target, value in values.items()
            if value not in allowed.get(family, {}).get(target, [])]


def compare_state(baseline: Configuration, operations: Iterable[Operation], observed: Configuration,
                  skip_families: Iterable[str] = ()) -> list[VerificationDifference]:
    """Differences between the observed configuration and baseline plus operations."""
    expected = expected_after(baseline, operations)
    seen = family_state(observed)
    diffs: list[VerificationDifference] = []
    for family, values in expected.items():
        if family in skip_families:
            continue
        for target, value in values.items():
            if seen.get(family, {}).get(target) != value:
                diffs.append(VerificationDifference(family=family, target=target, expected=value,
                                                    observed=seen.get(family, {}).get(target)))
    base, now = baseline.model_dump(mode="json"), observed.model_dump(mode="json")
    for family in va.CHECKED_FAMILIES:
        # Values stay out of these entries: macro contents must not reach output by default.
        if family not in va.RESTORABLE_FAMILIES and family not in skip_families and base[family] != now[family]:
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
