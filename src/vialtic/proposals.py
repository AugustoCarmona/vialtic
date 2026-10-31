# SPDX-License-Identifier: GPL-2.0-or-later
"""Immutable proposals: validation, content-derived ids, diffs and baseline checks."""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import vial_adapter as va
from .device import Session, canonical_json
from .errors import VialticError
from .models import (
    AltRepeatKeyValue, Baseline, Change, SetLightingChange, SetLightingOperation, MacroChangeSummary, SetMacroChange, SetMacrosOperation, ComboValue, Configuration, KeyOverrideValue, SetAltRepeatKeyChange,
    SetAltRepeatKeyOperation, SetComboChange, SetComboOperation, SetKeyOverrideChange, SetKeyOverrideOperation, SetQmkSettingChange, SetQmkSettingOperation,
    TapDanceValue, DeviceIdentity, KeycodeValue, KeymapEntry, EncoderEntry, Operation, Proposal,
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

LIGHTING_SAVED_NOTE = ("The firmware has one save command for all lighting and cannot report saved values. Any other unsaved "
                       "lighting is saved too, the save itself cannot be verified, and undo can only restore the values read "
                       "just before this write, saving them again.")
LIGHTING_TRANSIENT_NOTE = "This lighting change is not saved: it lasts until the keyboard next loses power."

FAMILY_OF_OPERATION = {"set_key": "keymap", "set_encoder": "encoders", "set_layout_options": "layout_options",
                       "set_tap_dance": "tap_dance", "set_combo": "combos", "set_key_override": "key_overrides",
                       "set_alt_repeat_key": "alt_repeat_keys", "set_qmk_setting": "settings", "set_macros": "macros",
                       "set_lighting": "lighting"}
U8 = 0xFF
U16 = 0xFFFF


def _value(entry: KeymapEntry | EncoderEntry) -> KeycodeValue:
    return KeycodeValue(raw=entry.raw, keycode=entry.keycode, label=entry.label)


def touches_lighting(operations: Iterable[Operation]) -> bool:
    return any(op.type == "set_lighting" for op in operations)


def touched_lighting_systems(operations: Iterable[Operation]) -> list[str]:
    return sorted({op.system for op in operations if op.type == "set_lighting"})


def saves_lighting(operations: Iterable[Operation]) -> bool:
    return any(op.type == "set_lighting" and op.save for op in operations)


def after_keycodes(op: Operation) -> list[KeycodeValue]:
    """Every keycode an operation writes, including ones an entry merely keeps."""
    if op.type in ("set_key", "set_encoder"):
        return [op.after]
    if op.type == "set_tap_dance":
        return [op.after.on_tap, op.after.on_hold, op.after.on_double_tap, op.after.on_tap_hold]
    if op.type == "set_combo":
        return [*op.after.keys, op.after.output]
    if op.type == "set_key_override":
        return [op.after.trigger, op.after.replacement]
    if op.type == "set_alt_repeat_key":
        return [op.after.keycode, op.after.alt_keycode]
    return []


def requires_unlock(operations: Iterable[Operation]) -> bool:
    """Whether the firmware will only accept these writes while physically unlocked."""
    return any(op.type == "set_macros" or any(value.keycode == va.RESET_KEYCODE for value in after_keycodes(op))
               for op in operations)


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
        self.settings_after: dict[int, int] = {}
        self.macro_edits: dict[int, bytes] = {}
        self.lighting_saves: set[bool] = set()

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
        if not 0 <= value <= limit:
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

    def _options(self, value: int | None, current: int, known: int, what: str) -> int:
        """Bits the pinned tables do not name may be kept, never changed."""
        value = self._bounded(value, current, U8, f"The options of {what}")
        if (value ^ current) & ~known & U8:
            raise VialticError("invalid_request", f"The options of {what} change bits Vial does not define "
                                                  f"(known mask 0x{known:02x}).")
        return value

    def _set_key_override(self, change: SetKeyOverrideChange) -> None:
        _capability(self.kb.key_override_count, "key_overrides", "key overrides")
        self._index(change.index, self.kb.key_override_count, "Key override")
        self._once(("key_override", change.index), f"key override {change.index}")
        before = va.key_override_value(self.config.key_overrides[change.index])
        layers = self._bounded(change.layers, before.layers, U16, "The layer mask")
        # All-ones is the firmware default and means every layer, however many there are.
        if layers != U16 and layers != before.layers and layers >> self.kb.layers:
            raise VialticError("invalid_layer_reference", f"Layer mask 0x{layers:04x} names a layer this keyboard (layers "
                                                          f"0-{self.kb.layers - 1}) does not have.")
        after = KeyOverrideValue(
            trigger=self._merged_keycode(change.trigger, before.trigger),
            replacement=self._merged_keycode(change.replacement, before.replacement), layers=layers,
            trigger_mods=self._bounded(change.trigger_mods, before.trigger_mods, U8, "The trigger modifier mask"),
            negative_mod_mask=self._bounded(change.negative_mod_mask, before.negative_mod_mask, U8, "The negative modifier mask"),
            suppressed_mods=self._bounded(change.suppressed_mods, before.suppressed_mods, U8, "The suppressed modifier mask"),
            options=self._options(change.options, before.options, va.KEY_OVERRIDE_KNOWN_OPTIONS, f"key override {change.index}"))
        if after == before:
            return self._skip(f"key override {change.index}")
        self.operations.append(SetKeyOverrideOperation(index=change.index, before=before, after=after))

    def _set_alt_repeat_key(self, change: SetAltRepeatKeyChange) -> None:
        _capability(self.kb.alt_repeat_key_count, "alt_repeat_keys", "alternate repeat keys")
        self._index(change.index, self.kb.alt_repeat_key_count, "Alternate repeat entry")
        self._once(("alt_repeat", change.index), f"alternate repeat {change.index}")
        before = va.alt_repeat_value(self.config.alt_repeat_keys[change.index])
        after = AltRepeatKeyValue(
            keycode=self._merged_keycode(change.keycode, before.keycode),
            alt_keycode=self._merged_keycode(change.alt_keycode, before.alt_keycode),
            allowed_mods=self._bounded(change.allowed_mods, before.allowed_mods, U8, "The allowed modifier mask"),
            options=self._options(change.options, before.options, va.ALT_REPEAT_KNOWN_OPTIONS, f"alternate repeat {change.index}"))
        if after == before:
            return self._skip(f"alternate repeat {change.index}")
        self.operations.append(SetAltRepeatKeyOperation(index=change.index, before=before, after=after))

    def _set_qmk_setting(self, change: SetQmkSettingChange) -> None:
        qsid = change.qsid
        _capability(self.kb.supported_settings, "settings", "QMK settings")
        if qsid not in self.kb.supported_settings:
            raise VialticError("capability_unavailable", f"This keyboard's firmware does not expose QMK setting {qsid}.",
                               details={"family": "settings", "qsid": qsid})
        if qsid in self.config.settings.unknown_qsids:
            raise VialticError("unsupported_operation", f"QMK setting {qsid} is not in the pinned Vial tables, so its width "
                                                        "and meaning are unknown. Vialtic will not write it.",
                               details={"qsid": qsid})
        current = self.config.settings.values.get(str(qsid))
        if current is None:
            raise VialticError("unsupported_operation", f"The firmware refused to report QMK setting {qsid}, so a change "
                                                        "could not be backed up. Vialtic will not write it.",
                               details={"qsid": qsid})
        fields = va.setting_fields(qsid)
        by_bit = change.bit is not None or change.enabled is not None
        if by_bit == (change.value is not None) or (by_bit and (change.bit is None or change.enabled is None)):
            raise VialticError("invalid_request", f"QMK setting {qsid}: give either value, or both bit and enabled.")
        self._once(("setting", qsid, change.bit), f"QMK setting {qsid}" + ("" if change.bit is None else f" bit {change.bit}"))
        if ("setting", qsid, None) in self.seen and by_bit or (not by_bit and qsid in self.settings_after):
            raise VialticError("invalid_request", f"The request changes QMK setting {qsid} more than once.")
        value = self.settings_after.get(qsid, current)
        if fields[0]["type"] == "integer":
            if by_bit:
                raise VialticError("invalid_request", f"QMK setting {qsid} ({fields[0]['title']}) is a number, not flags.")
            if not fields[0]["min"] <= change.value <= fields[0]["max"]:
                raise VialticError("invalid_request", f"QMK setting {qsid} ({fields[0]['title']}) must be between "
                                                      f"{fields[0]['min']} and {fields[0]['max']}, not {change.value}.")
            value = change.value
        else:
            known = sum(1 << f.get("bit", 0) for f in fields)
            if by_bit:
                if not 0 <= change.bit < 32 or not known >> change.bit & 1:
                    raise VialticError("invalid_request", f"QMK setting {qsid} has no flag at bit {change.bit}.")
                value = value | (1 << change.bit) if change.enabled else value & ~(1 << change.bit)
            else:
                width = fields[0].get("width", 1)
                if not 0 <= change.value < 1 << (8 * width):
                    raise VialticError("invalid_request", f"QMK setting {qsid} must fit in {width} byte(s), not {change.value}.")
                if (change.value ^ current) & ~known:
                    raise VialticError("invalid_request", f"QMK setting {qsid}: the value changes bits Vial does not define "
                                                          f"(known mask 0x{known:x}).")
                value = change.value
        self.settings_after[qsid] = value

    def _set_lighting(self, change: SetLightingChange) -> None:
        current = getattr(self.config.lighting, change.system)
        _capability(current, "lighting", f"{change.system} lighting")
        self._once(("lighting", change.system), f"{change.system} lighting")
        fields = va.LIGHTING_FIELDS[change.system]
        given = {name for name in type(change).model_fields if name not in ("type", "system", "save")
                 and getattr(change, name) is not None}
        if given - set(fields):
            raise VialticError("invalid_request", f"{change.system} lighting has no {', '.join(sorted(given - set(fields)))}. "
                                                  f"It takes: {', '.join(fields)}.")
        before = {name: current[name] for name in fields}
        limits = {"mode": U16}
        if change.system == "backlight":
            limits["effect"] = 1  # breathing is on or off
        if change.system == "vialrgb":
            limits["value"] = current["maximum_brightness"]
        after = {name: self._bounded(getattr(change, name), before[name], limits.get(name, U8), f"The {name} value")
                 for name in fields}
        if change.system == "vialrgb" and after["mode"] not in current["supported_effects"]:
            raise VialticError("capability_unavailable", f"This keyboard's firmware does not support VialRGB effect {after['mode']}.",
                               details={"family": "lighting", "supported_effects": current["supported_effects"]})
        self.lighting_saves.add(change.save)
        if len(self.lighting_saves) > 1:
            raise VialticError("invalid_request", "The firmware has one save command for all lighting, so a request cannot "
                                                  "save one lighting system and leave another transient.")
        # Same values with a save is meaningful: it makes transient values permanent.
        if after == before and not change.save:
            return self._skip(f"{change.system} lighting")
        self.operations.append(SetLightingOperation(system=change.system, before=before, after=after, save=change.save))
        self.warnings.append(LIGHTING_SAVED_NOTE if change.save else LIGHTING_TRANSIENT_NOTE)

    def _set_macro(self, change: SetMacroChange) -> None:
        _capability(self.kb.macro_count and self.kb.macro_memory, "macros", "macros")
        self._index(change.index, self.kb.macro_count, "Macro")
        self._once(("macro", change.index), f"macro {change.index}")
        data, notes = va.encode_macro(self.kb, change.index, change.actions)
        self.warnings.extend(notes)
        self.macro_edits[change.index] = data

    def _finish_macros(self) -> None:
        if not self.macro_edits:
            return
        macros = self.config.macros
        before = va.decode_macro_buffer(macros.buffer_b64)
        segments = va.macro_segments(before, macros.count)
        for index, data in self.macro_edits.items():
            segments[index] = data
        after = b"\x00".join(segments) + b"\x00"
        if after == before:
            return self._skip("macros")
        if len(after) > macros.memory_bytes:
            raise VialticError("capacity_exceeded", f"The macros would need {len(after)} bytes, counting one terminator "
                                                    f"for each macro, and this keyboard has {macros.memory_bytes}. Nothing "
                                                    "was changed.",
                               details={"needed_bytes": len(after), "memory_bytes": macros.memory_bytes},
                               next_steps=["Shorten or clear a macro. Vialtic never truncates them."])
        self.operations.append(macro_buffer_operation(before, after, macros.count, macros.memory_bytes, self.kb.vial_protocol))

    def finish(self) -> tuple[list[Operation], list[str]]:
        self._finish_macros()
        for qsid, value in self.settings_after.items():
            before_value = self.config.settings.values[str(qsid)]
            if value == before_value:
                self._skip(f"QMK setting {qsid}")
            else:
                self.operations.append(SetQmkSettingOperation(qsid=qsid, title=va.setting_change_title(qsid, before_value, value),
                                                              before=before_value, after=value))
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


def macro_buffer_operation(before: bytes, after: bytes, count: int, memory: int, vial_protocol: int | None) -> SetMacrosOperation:
    """Compare raw buffers macro by macro. The preview needs a keyboard's protocol to mean anything."""
    old, new = va.macro_segments(before, count), va.macro_segments(after, count)
    changed = []
    for index, (was, now) in enumerate(zip(old, new)):
        if was != now:
            previews = {} if vial_protocol is None else dict(before=va.macro_preview(was, vial_protocol),
                                                             after=va.macro_preview(now, vial_protocol))
            changed.append(MacroChangeSummary(index=index, before_bytes=len(was), after_bytes=len(now), **previews))
    return SetMacrosOperation(changed=changed, memory_bytes=memory, before_used_bytes=len(before), after_used_bytes=len(after),
                              before_buffer_b64=base64.b64encode(before).decode("ascii"),
                              after_buffer_b64=base64.b64encode(after).decode("ascii"))


def redact_fingerprints(fingerprints: dict[str, str]) -> dict[str, str]:
    """A plain hash would confirm a guess at the contents."""
    return {family: "redacted" if family == "macros" else value for family, value in fingerprints.items()}


def redact_proposal(proposal: Proposal) -> Proposal:
    """A copy safe for default output. Its id no longer verifies, so it is for display only."""
    operations = []
    for op in proposal.operations:
        if op.type == "set_macros":
            changed = [item.model_copy(update=dict(before=None, after=None)) for item in op.changed]
            op = op.model_copy(update=dict(changed=changed, before_buffer_b64=None, after_buffer_b64=None, redacted=True))
        operations.append(op)
    baseline = proposal.baseline.model_copy(update={"family_fingerprints": redact_fingerprints(proposal.baseline.family_fingerprints)})
    return proposal.model_copy(update={"operations": operations, "baseline": baseline})


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
    if op.type == "set_qmk_setting":
        return f"QMK setting {op.qsid} ({op.title}): {op.before} -> {op.after}"
    if op.type == "set_lighting":
        changes = ", ".join(f"{name} {op.before[name]} -> {op.after[name]}" for name in op.after
                            if op.before[name] != op.after[name]) or "no value changes"
        return f"{op.system} lighting: {changes} ({'saved' if op.save else 'until power is lost'})"
    if op.type == "set_macros":
        which = ", ".join(str(item.index) for item in op.changed) or "none (only the raw buffer differs)"
        return f"macros {which}: buffer {op.before_used_bytes} -> {op.after_used_bytes} of {op.memory_bytes} bytes"
    if op.type == "set_key_override":
        return f"key override {op.index}: {_changed_fields(op.before, op.after)}"
    if op.type == "set_alt_repeat_key":
        return f"alternate repeat {op.index}: {_changed_fields(op.before, op.after)}"
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

def _lighting_state(config: Configuration) -> dict[str, dict[str, int]]:
    """Values a write can set. The rest of a lighting read describes the firmware's limits."""
    light = config.lighting
    return {system: {name: getattr(light, system)[name] for name in fields}
            for system, fields in va.LIGHTING_FIELDS.items() if getattr(light, system)}


def family_state(config: Configuration, lighting: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """Lighting systems are only part of it when named: keys on the board change them at will."""
    systems = _lighting_state(config)
    state = {"lighting": {name: systems[name] for name in lighting if name in systems}} if lighting else {}
    return state | {
        "keymap": {f"{e.layer}:{e.row},{e.col}": e.raw for e in config.keymap},
        "encoders": {f"{e.layer}:{e.index}:{e.direction}": e.raw for e in config.encoders},
        "layout_options": {"value": config.layout_options},
        "tap_dance": {str(i): list(e) for i, e in enumerate(config.tap_dance)},
        "combos": {str(i): list(e) for i, e in enumerate(config.combos)},
        "key_overrides": {str(i): dict(e) for i, e in enumerate(config.key_overrides)},
        "alt_repeat_keys": {str(i): dict(e) for i, e in enumerate(config.alt_repeat_keys)},
        "settings": dict(config.settings.values),
        "macros": {"buffer": config.macros.buffer_b64},
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
    if op.type == "set_key_override":
        return "key_overrides", str(op.index), va.key_override_wire(op.before), va.key_override_wire(op.after)
    if op.type == "set_alt_repeat_key":
        return "alt_repeat_keys", str(op.index), va.alt_repeat_wire(op.before), va.alt_repeat_wire(op.after)
    if op.type == "set_qmk_setting":
        return "settings", str(op.qsid), op.before, op.after
    if op.type == "set_macros":
        return "macros", "buffer", op.before_buffer_b64, op.after_buffer_b64
    if op.type == "set_lighting":
        return "lighting", op.system, op.before, op.after
    raise VialticError("unsupported_operation", f"Unknown operation type {op.type}.")


def affected_families(operations: Iterable[Operation]) -> list[str]:
    return sorted({FAMILY_OF_OPERATION[op.type] for op in operations})


def restore_blockers(current: Configuration, target: Configuration) -> list[str]:
    """Families whose shape changed, which happens when firmware was rebuilt with the same definition."""
    have, want = family_state(current), family_state(target)
    blockers = [family for family in want if set(want[family]) - set(have[family])]
    # A snapshot with its macros left out, or a buffer that no longer fits, cannot be written back.
    macros = target.macros
    if "macros" not in blockers and macros.count and (
            macros.buffer_b64 is None or macros.count != current.macros.count
            or len(va.decode_macro_buffer(macros.buffer_b64)) - macros.count > current.macros.memory_bytes):
        blockers.append("macros")
    return blockers


def restore_operations(current: Configuration, target: Configuration, vial_protocol: int | None = None,
                       lighting_save: bool | None = None,
                       lighting_systems: Iterable[str] = (),
                       layout_labels: list | None = None) -> tuple[list[Operation], list[str], list[str]]:
    """Operations turning current into target. Also returns unrestorable families and notes.

    `lighting_systems` are the ones this restore covers, and `lighting_save` whether to save them.
    Anything else in lighting is left alone, because keys on the board may have changed it.
    """
    ops: list[Operation] = []
    blockers = restore_blockers(current, target)
    if "keymap" not in blockers:
        now_keys = {(e.layer, e.row, e.col): e for e in current.keymap}
        for wanted in target.keymap:
            have = now_keys[(wanted.layer, wanted.row, wanted.col)]
            if have.raw != wanted.raw:
                ops.append(SetKeyOperation(layer=wanted.layer, row=wanted.row, col=wanted.col, before=_value(have),
                                           after=_value(wanted)))
    if "encoders" not in blockers:
        now_encoders = {(e.layer, e.index, e.direction): e for e in current.encoders}
        for wanted in target.encoders:
            have = now_encoders[(wanted.layer, wanted.index, wanted.direction)]
            if have.raw != wanted.raw:
                ops.append(SetEncoderOperation(layer=wanted.layer, index=wanted.index, direction=wanted.direction,
                                               before=_value(have), after=_value(wanted)))
    if "layout_options" not in blockers and current.layout_options != target.layout_options:
        labels = layout_labels or []
        ops.append(SetLayoutOptionsOperation(before=current.layout_options, after=target.layout_options,
                                             before_choices=va.layout_choices(labels, current.layout_options),
                                             after_choices=va.layout_choices(labels, target.layout_options)))
    if "tap_dance" not in blockers:
        for index, (have_td, want_td) in enumerate(zip(current.tap_dance, target.tap_dance)):
            if list(have_td) != list(want_td):
                ops.append(SetTapDanceOperation(index=index, before=va.tap_dance_value(have_td),
                                                after=va.tap_dance_value(want_td)))
    if "combos" not in blockers:
        for index, (have_combo, want_combo) in enumerate(zip(current.combos, target.combos)):
            if list(have_combo) != list(want_combo):
                ops.append(SetComboOperation(index=index, before=va.combo_value(have_combo), after=va.combo_value(want_combo)))
    if "key_overrides" not in blockers:
        for index, (have_ko, want_ko) in enumerate(zip(current.key_overrides, target.key_overrides)):
            if have_ko != want_ko:
                ops.append(SetKeyOverrideOperation(index=index, before=va.key_override_value(have_ko),
                                                   after=va.key_override_value(want_ko)))
    if "alt_repeat_keys" not in blockers:
        for index, (have_alt, want_alt) in enumerate(zip(current.alt_repeat_keys, target.alt_repeat_keys)):
            if have_alt != want_alt:
                ops.append(SetAltRepeatKeyOperation(index=index, before=va.alt_repeat_value(have_alt),
                                                    after=va.alt_repeat_value(want_alt)))

    if "settings" not in blockers:
        for key, wanted in target.settings.values.items():
            if current.settings.values[key] != wanted:
                qsid = int(key)
                title = va.setting_change_title(qsid, current.settings.values[key], wanted)
                ops.append(SetQmkSettingOperation(qsid=qsid, title=title, before=current.settings.values[key], after=wanted))

    if "macros" not in blockers and current.macros.buffer_b64 != target.macros.buffer_b64:
        # Raw bytes, never the parsed form: parsing drops malformed bytes and re-encodes others.
        ops.append(macro_buffer_operation(va.decode_macro_buffer(current.macros.buffer_b64),
                                          va.decode_macro_buffer(target.macros.buffer_b64),
                                          target.macros.count, current.macros.memory_bytes, vial_protocol))

    notes = []
    moved = [key for key, raw in target.settings.unknown_raw.items() if current.settings.unknown_raw.get(key) not in (None, raw)]
    if moved:
        notes.append(f"QMK settings {moved} are not in the pinned Vial tables and changed since the backup. Vialtic cannot "
                     "restore them; use the software that changed them.")
    now_light, want_light = _lighting_state(current), _lighting_state(target)
    systems = set(lighting_systems)
    if {name for name in want_light if name in systems} - set(now_light):
        blockers.append("lighting")
        return ops, blockers, notes
    if any(now_light.get(name) != values for name, values in want_light.items() if name not in systems):
        notes.append("Lighting differs from the saved state and is not restored: the write being reversed did not change "
                     "it, so the difference is likely lighting keys on the keyboard.")
    for system, wanted in want_light.items():
        # A save is written even with equal values: the saved values may be the ones to replace.
        if system in systems and (now_light[system] != wanted or lighting_save):
            ops.append(SetLightingOperation(system=system, before=now_light[system], after=wanted, save=bool(lighting_save)))
    if touches_lighting(ops):
        notes.append(LIGHTING_SAVED_NOTE if lighting_save else LIGHTING_TRANSIENT_NOTE)
    return ops, blockers, notes


def proposal_id(fields: dict) -> str:
    """Derive the id from canonical content, so any edit to the stored file is detectable."""
    digest = hashlib.sha256(canonical_json(fields).encode("ascii")).hexdigest()
    return ID_PREFIX + digest[:20]


def build_proposal(proposal_type: ProposalType, device: DeviceIdentity, snapshot: Snapshot,
                   operations: list[Operation], warnings: list[str], positions: list[tuple[int, int]],
                   now: datetime, ttl: timedelta, reconnect_window: float) -> Proposal:
    required = requires_unlock(operations)
    # A proposal that writes lighting must notice it moved; one that does not must not care.
    checked = list(va.CHECKED_FAMILIES) + (["lighting"] if touches_lighting(operations) else [])
    fields = dict(
        schema_version=1, kind="proposal", proposal_type=proposal_type, device=device,
        baseline=Baseline(family_fingerprints={f: snapshot.family_fingerprints[f] for f in checked}, checked_families=checked),
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
    tampered = VialticError("proposal_tampered", f"Stored proposal {proposal.id} no longer matches its id or its own "
                                                 "operations; it was edited.",
                            next_steps=["Create a new proposal and get it approved."])
    if proposal_id(_content(proposal)) != proposal.id:
        raise tampered
    # An id can be recomputed, so a proposal must also keep the guarantees every real one has.
    if set(va.CHECKED_FAMILIES) - set(proposal.baseline.checked_families) \
            or set(proposal.baseline.checked_families) - set(proposal.baseline.family_fingerprints) \
            or (touches_lighting(proposal.operations) and "lighting" not in proposal.baseline.checked_families) \
            or proposal.affected != affected_families(proposal.operations):
        raise tampered


def check_stated_values(proposal: Proposal, kb: Any) -> None:
    """What a proposal shows must be what it writes. Needs the keyboard's keycode tables active."""
    for op in proposal.operations:
        for value in after_keycodes(op):
            if value != va.to_value(value.keycode) or value.raw != va.Keycode.deserialize(value.keycode, reraise=True):
                raise VialticError("proposal_stale", "A keycode in this proposal resolves differently now.")
        if op.type == "set_macros":
            restated = macro_buffer_operation(va.decode_macro_buffer(op.before_buffer_b64),
                                              va.decode_macro_buffer(op.after_buffer_b64), kb.macro_count, op.memory_bytes,
                                              kb.vial_protocol)
            if restated != op:
                raise VialticError("proposal_tampered", f"The macro preview in proposal {proposal.id} is not what it writes.",
                                   next_steps=["Create a new proposal and get it approved."])


def unexplained_values(baseline: Configuration, operations: Iterable[Operation],
                       observed: Configuration) -> list[VerificationDifference]:
    """Values neither the baseline nor this write can account for.

    A keyboard holding any is not provably the unit that was being written, or another
    application changed it during the write. Either way it must not be overwritten.
    """
    operations = list(operations)
    allowed = {family: {target: [value] for target, value in values.items()}
               for family, values in family_state(baseline).items()}
    for op in operations:
        family, target, before, after = operation_effect(op)
        if family != "lighting":
            allowed.setdefault(family, {}).setdefault(target, []).extend([before, after])
    state = family_state(observed)
    # A macro write goes out in chunks, so an interrupted one can leave any mix of old and new.
    # The other families still identify the keyboard.
    if any(op.type == "set_macros" for op in operations):
        state.pop("macros")
    return [VerificationDifference(family=family, target=target, observed=None if family == "macros" else value)
            for family, values in state.items() for target, value in values.items()
            if value not in allowed.get(family, {}).get(target, [])]


def compare_state(baseline: Configuration, operations: Iterable[Operation], observed: Configuration,
                  skip_families: Iterable[str] = (), lighting_systems: Iterable[str] = ()) -> list[VerificationDifference]:
    """Differences between the observed configuration and baseline plus operations."""
    operations = list(operations)
    systems = sorted(set(lighting_systems) | set(touched_lighting_systems(operations)))
    expected = family_state(baseline, lighting=systems)
    for op in operations:
        family, target, _, after = operation_effect(op)
        expected[family][target] = after
    seen = family_state(observed, lighting=systems)
    diffs: list[VerificationDifference] = []
    for family, values in expected.items():
        if family in skip_families:
            continue
        for target, value in values.items():
            if seen.get(family, {}).get(target) != value:
                # Values stay out of macro entries: their contents must not reach output by default.
                diffs.append(VerificationDifference(family=family, target=target) if family == "macros" else
                             VerificationDifference(family=family, target=target, expected=value,
                                                    observed=seen.get(family, {}).get(target)))
    return diffs


def format_proposal(proposal: Proposal) -> str:
    lines = [f"Proposal {proposal.id} ({proposal.proposal_type})",
             f"Keyboard: {proposal.device.name} [{proposal.device.device_id}]"
             + (" SIMULATED" if proposal.device.simulated else ""),
             f"Expires: {proposal.expires_at.isoformat()}", ""]
    if proposal.operations:
        lines.append(f"Changes ({len(proposal.operations)}):")
        lines.extend(f"  {describe_operation(op)}" for op in proposal.operations)
        for op in proposal.operations:
            if op.type == "set_macros" and not op.redacted:
                for item in op.changed:
                    lines += [f"  macro {item.index} before: {item.before}", f"  macro {item.index} after: {item.after}"]
            elif op.type == "set_macros":
                lines.append(f"  Macro contents are left out. Show them to the user before approval: "
                             f"vialtic proposal show {proposal.id} --include-macros")
    else:
        lines.append("Changes: none; the keyboard already matches the target.")
    if proposal.unlock.required:
        positions = ", ".join(f"{p.row},{p.col}" for p in proposal.unlock.positions)
        lines += ["", f"Physical unlock required. Hold these keys when asked: {positions}"]
    if proposal.warnings:
        lines += ["", "Warnings:"] + [f"  {w}" for w in proposal.warnings]
    lines += ["", f"Verification: {proposal.verification}", f"Recovery: {proposal.recovery}"]
    return "\n".join(lines)
