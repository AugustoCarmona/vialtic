# SPDX-License-Identifier: GPL-2.0-or-later
"""Typed, versioned public contracts shared by the CLI and (later) MCP.

`schema_version` is on every envelope. Unknown fields are rejected so a file
written by a newer version fails closed instead of being half understood.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Envelope(Model):
    schema_version: Literal[1] = SCHEMA_VERSION


class ErrorInfo(Model):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    next_steps: list[str] = Field(default_factory=list)


IdentityConfidence = Literal["usb_serial", "model_and_firmware", "unknown"]


class DeviceIdentity(Model):
    """How a keyboard was recognized. `device_id` only selects it for this plug session."""

    device_id: str
    name: str
    vendor_id: str
    product_id: str
    serial_number: str
    path: str
    simulated: bool = False
    vial_uid: str | None = None
    via_protocol: int | None = None
    vial_protocol: int | None = None
    layout_fingerprint: str | None = None
    binding_id: str | None = None
    identity_confidence: IdentityConfidence = "unknown"
    identity_note: str = ""


DeviceStatus = Literal["ready", "permission_denied", "busy", "unsupported_protocol", "error"]


class DeviceEntry(Model):
    identity: DeviceIdentity
    status: DeviceStatus
    problem: ErrorInfo | None = None


class IgnoredInterface(Model):
    """A HID interface deliberately not offered as a candidate, reported for diagnostics."""

    vendor_id: str
    product_id: str
    name: str
    reason: str


class DeviceList(Envelope):
    kind: Literal["device_list"] = "device_list"
    simulated: bool = False
    devices: list[DeviceEntry]
    ignored: list[IgnoredInterface] = Field(default_factory=list)


class Capabilities(Model):
    layers: int
    rows: int
    cols: int
    key_count: int
    encoder_count: int
    macro_count: int
    macro_memory_bytes: int
    tap_dance_count: int
    combo_count: int
    key_override_count: int
    alt_repeat_key_count: int
    qmk_settings: list[int]
    unknown_qmk_settings: list[int]
    lighting: str
    layout_option_labels: list[Any] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
    custom_keycode_count: int = 0
    midi: str | None = None
    writable_operations: list[str] = Field(default_factory=list)


class KeyGeometry(Model):
    id: str
    row: int | None = None
    col: int | None = None
    encoder_index: int | None = None
    encoder_direction: int | None = None
    x: float
    y: float
    width: float
    height: float
    x2: float = 0
    y2: float = 0
    width2: float = 1
    height2: float = 1
    rotation_x: float = 0
    rotation_y: float = 0
    rotation_angle: float = 0
    layout_index: int = -1
    layout_option: int = -1
    decal: bool = False


class LayoutGeometry(Model):
    keys: list[KeyGeometry]
    encoders: list[KeyGeometry]
    layout_options: int
    layout_choices: list[int]


class KeycodeValue(Model):
    """A firmware keycode. `raw` is authoritative, `keycode`/`label` are for people."""

    raw: int
    keycode: str
    label: str


class KeymapEntry(KeycodeValue):
    layer: int
    row: int
    col: int


class EncoderEntry(KeycodeValue):
    layer: int
    index: int
    direction: int


class MacroState(Model):
    count: int
    memory_bytes: int
    used_bytes: int
    redacted: bool = False
    buffer_b64: str | None = None
    macros: list[list[Any]] | None = None


class SettingsState(Model):
    values: dict[str, int]
    unknown_qsids: list[int] = Field(default_factory=list)
    # Exposed by the firmware and known to the table, but the firmware refused to report them.
    unreadable_qsids: list[int] = Field(default_factory=list)
    # What the firmware answers for unknown_qsids, as hex. Their width is unknown, so these
    # can show that a value moved but can never be written back.
    unknown_raw: dict[str, str] = Field(default_factory=dict)


class LightingState(Model):
    """Values the firmware reports now. Firmware does not say whether they are saved."""

    kind: str
    rgblight: dict[str, Any] | None = None
    backlight: dict[str, Any] | None = None
    vialrgb: dict[str, Any] | None = None


class Configuration(Model):
    keymap: list[KeymapEntry]
    encoders: list[EncoderEntry]
    layout_options: int
    macros: MacroState
    tap_dance: list[list[Any]]
    combos: list[list[Any]]
    key_overrides: list[dict[str, Any]]
    alt_repeat_keys: list[dict[str, Any]]
    settings: SettingsState
    lighting: LightingState


class FollowingWrite(Model):
    """What the write that used this snapshot as its backup changed. Undo needs it for lighting,
    where a difference may just be someone pressing a lighting key."""

    families: list[str] = Field(default_factory=list)
    lighting_saved: bool = False
    lighting_systems: list[str] = Field(default_factory=list)


class Snapshot(Envelope):
    kind: Literal["snapshot"] = "snapshot"
    created_at: datetime
    device: DeviceIdentity
    capabilities: Capabilities
    layout: LayoutGeometry
    configuration: Configuration
    family_fingerprints: dict[str, str]
    coverage: dict[str, str]
    warnings: list[str] = Field(default_factory=list)
    followed_by: FollowingWrite | None = None


class InspectResult(Envelope):
    kind: Literal["device_inspect"] = "device_inspect"
    device: DeviceIdentity
    capabilities: Capabilities
    layout: LayoutGeometry
    unlock: "UnlockStatus"
    recovery_pending: bool = False
    previous_available: bool = False
    warnings: list[str] = Field(default_factory=list)


class UnlockPosition(Model):
    row: int
    col: int


class UnlockStatus(Envelope):
    kind: Literal["unlock_status"] = "unlock_status"
    unlocked: bool
    in_progress: bool
    positions: list[UnlockPosition] = Field(default_factory=list)
    counter: int | None = None


# Requested changes (input).

class ChangeModel(Model):
    """Input from files: a boolean is not a number and text is not a layer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SetKeyChange(ChangeModel):
    type: Literal["set_key"] = "set_key"
    layer: int
    row: int
    col: int
    keycode: str


class SetEncoderChange(ChangeModel):
    type: Literal["set_encoder"] = "set_encoder"
    layer: int
    index: int
    direction: int
    keycode: str


class SetLayoutOptionChange(ChangeModel):
    """Choose one firmware-defined layout alternative. `index` is its position in the option list."""

    type: Literal["set_layout_option"] = "set_layout_option"
    index: int
    choice: int


class SetTapDanceChange(ChangeModel):
    """Change one tap dance entry. Fields left out keep their current value."""

    type: Literal["set_tap_dance"] = "set_tap_dance"
    index: int
    on_tap: str | None = None
    on_hold: str | None = None
    on_double_tap: str | None = None
    on_tap_hold: str | None = None
    tapping_term: int | None = None


class SetComboChange(ChangeModel):
    """Change one combo. `keys` lists up to four inputs and replaces all of them. Fields left out keep their value."""

    type: Literal["set_combo"] = "set_combo"
    index: int
    keys: list[str] | None = Field(default=None, max_length=4)
    output: str | None = None


class SetKeyOverrideChange(ChangeModel):
    """Change one key override. Fields left out keep their current value. Masks are integers:
    `layers` has one bit per layer, modifier masks follow QMK's MOD_BIT layout, and `options`
    holds the firmware flags, with bit 7 enabling the entry."""

    type: Literal["set_key_override"] = "set_key_override"
    index: int
    trigger: str | None = None
    replacement: str | None = None
    layers: int | None = None
    trigger_mods: int | None = None
    negative_mod_mask: int | None = None
    suppressed_mods: int | None = None
    options: int | None = None


class SetAltRepeatKeyChange(ChangeModel):
    """Change one alternate repeat entry. Fields left out keep their current value.
    In `options`, bit 3 enables the entry."""

    type: Literal["set_alt_repeat_key"] = "set_alt_repeat_key"
    index: int
    keycode: str | None = None
    alt_keycode: str | None = None
    allowed_mods: int | None = None
    options: int | None = None


class SetQmkSettingChange(ChangeModel):
    """Change one QMK setting: either the whole `value`, or one flag with `bit` and `enabled`."""

    type: Literal["set_qmk_setting"] = "set_qmk_setting"
    qsid: int
    value: int | None = None
    bit: int | None = None
    enabled: bool | None = None


class SetMacroChange(ChangeModel):
    """Replace one macro. `actions` holds ["text", "abc"], ["tap", "KC_A", ...], ["down", ...],
    ["up", ...] or ["delay", milliseconds]. An empty list clears the macro."""

    type: Literal["set_macro"] = "set_macro"
    index: int
    actions: list[list[Any]] = Field(max_length=4096)


LightingSystem = Literal["rgblight", "backlight", "vialrgb"]


class SetLightingChange(ChangeModel):
    """Change one lighting system. Fields left out keep their current value.

    rgblight takes brightness, effect, effect_speed, hue, saturation. backlight takes brightness,
    effect. vialrgb takes mode, speed, hue, saturation, value. `save` is required: true sends the
    firmware's one save command, which persists all lighting; false lasts until the next power cycle.
    """

    type: Literal["set_lighting"] = "set_lighting"
    system: LightingSystem
    save: bool
    brightness: int | None = None
    effect: int | None = None
    effect_speed: int | None = None
    hue: int | None = None
    saturation: int | None = None
    mode: int | None = None
    speed: int | None = None
    value: int | None = None


Change = Annotated[Union[SetKeyChange, SetEncoderChange, SetLayoutOptionChange, SetTapDanceChange, SetComboChange,
                         SetKeyOverrideChange, SetAltRepeatKeyChange, SetQmkSettingChange, SetMacroChange, SetLightingChange],
                   Field(discriminator="type")]


class ChangeSet(Envelope):
    kind: Literal["change_set"] = "change_set"
    changes: list[Change] = Field(min_length=1, max_length=4096)


# Proposals.

class SetKeyOperation(Model):
    type: Literal["set_key"] = "set_key"
    layer: int
    row: int
    col: int
    before: KeycodeValue
    after: KeycodeValue


class SetEncoderOperation(Model):
    type: Literal["set_encoder"] = "set_encoder"
    layer: int
    index: int
    direction: int
    before: KeycodeValue
    after: KeycodeValue


class SetLayoutOptionsOperation(Model):
    type: Literal["set_layout_options"] = "set_layout_options"
    before: int
    after: int
    before_choices: list[int] = Field(default_factory=list)
    after_choices: list[int] = Field(default_factory=list)


class TapDanceValue(Model):
    on_tap: KeycodeValue
    on_hold: KeycodeValue
    on_double_tap: KeycodeValue
    on_tap_hold: KeycodeValue
    tapping_term: int


class SetTapDanceOperation(Model):
    type: Literal["set_tap_dance"] = "set_tap_dance"
    index: int
    before: TapDanceValue
    after: TapDanceValue


class ComboValue(Model):
    keys: list[KeycodeValue]
    output: KeycodeValue


class SetComboOperation(Model):
    type: Literal["set_combo"] = "set_combo"
    index: int
    before: ComboValue
    after: ComboValue


class KeyOverrideValue(Model):
    trigger: KeycodeValue
    replacement: KeycodeValue
    layers: int
    trigger_mods: int
    negative_mod_mask: int
    suppressed_mods: int
    options: int


class SetKeyOverrideOperation(Model):
    type: Literal["set_key_override"] = "set_key_override"
    index: int
    before: KeyOverrideValue
    after: KeyOverrideValue


class AltRepeatKeyValue(Model):
    keycode: KeycodeValue
    alt_keycode: KeycodeValue
    allowed_mods: int
    options: int


class SetAltRepeatKeyOperation(Model):
    type: Literal["set_alt_repeat_key"] = "set_alt_repeat_key"
    index: int
    before: AltRepeatKeyValue
    after: AltRepeatKeyValue


class SetQmkSettingOperation(Model):
    type: Literal["set_qmk_setting"] = "set_qmk_setting"
    qsid: int
    title: str
    before: int
    after: int


class MacroChangeSummary(Model):
    """One macro the buffer write changes. `before` and `after` are a parsed preview for people;
    they can hide malformed bytes, and the buffers below are what is written."""

    index: int
    before_bytes: int
    after_bytes: int
    before: list[list[Any]] | None = None
    after: list[list[Any]] | None = None


class SetMacrosOperation(Model):
    """Macros are one firmware buffer, written whole. Contents are removed from default output."""

    type: Literal["set_macros"] = "set_macros"
    changed: list[MacroChangeSummary]
    memory_bytes: int
    before_used_bytes: int
    after_used_bytes: int
    before_buffer_b64: str | None = None
    after_buffer_b64: str | None = None
    redacted: bool = False


class SetLightingOperation(Model):
    type: Literal["set_lighting"] = "set_lighting"
    system: LightingSystem
    before: dict[str, int]
    after: dict[str, int]
    save: bool


Operation = Annotated[Union[SetKeyOperation, SetEncoderOperation, SetLayoutOptionsOperation, SetTapDanceOperation,
                            SetComboOperation, SetKeyOverrideOperation, SetAltRepeatKeyOperation, SetQmkSettingOperation,
                            SetMacrosOperation, SetLightingOperation],
                      Field(discriminator="type")]


class Baseline(Model):
    family_fingerprints: dict[str, str]
    checked_families: list[str]


class ProposalUnlock(Model):
    required: bool
    positions: list[UnlockPosition] = Field(default_factory=list)


ProposalType = Literal["change", "undo", "recovery"]


class Proposal(Envelope):
    kind: Literal["proposal"] = "proposal"
    id: str
    proposal_type: ProposalType
    device: DeviceIdentity
    baseline: Baseline
    operations: list[Operation]
    affected: list[str]
    warnings: list[str] = Field(default_factory=list)
    unlock: ProposalUnlock
    verification: str
    recovery: str
    reconnect_window_seconds: float
    created_at: datetime
    expires_at: datetime


class ApprovalAttestation(Model):
    """An agent's statement that the user approved this exact proposal. Not proof of consent."""

    proposal_id: str
    binding_id: str
    attested_at: datetime
    attested_by: str = "cli-flag"


# Apply.

class OperationResult(Model):
    index: int
    summary: str
    status: Literal["applied", "not_attempted", "failed"]


class VerificationDifference(Model):
    family: str
    target: str
    expected: Any = None
    observed: Any = None


ApplyOutcome = Literal["verified", "failed_restored", "partial", "unknown"]


class ApplyResult(Envelope):
    kind: Literal["apply_result"] = "apply_result"
    proposal_id: str
    binding_id: str
    device: DeviceIdentity
    outcome: ApplyOutcome
    message: str
    operations: list[OperationResult]
    differences: list[VerificationDifference] = Field(default_factory=list)
    backup_path: str | None = None
    write_packets_sent: int = 0
    relocked: bool | None = None
    relock_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    recovery_steps: list[str] = Field(default_factory=list)


JournalStatus = Literal["applying", "needs_recovery", "verified"]


class Journal(Envelope):
    """Interrupted-write metadata. Never a configuration history; holds no key data."""

    kind: Literal["recovery_journal"] = "recovery_journal"
    proposal_id: str
    proposal_type: ProposalType
    binding_id: str
    device: DeviceIdentity
    status: JournalStatus
    total_operations: int
    completed_operations: int
    started_at: datetime
    updated_at: datetime
    note: str = ""
    lighting_save_sent: bool = False


# Diagnostics.

class DoctorCheck(Model):
    name: str
    status: Literal["ok", "warning", "problem", "info"]
    message: str
    next_steps: list[str] = Field(default_factory=list)


class DoctorReport(Envelope):
    kind: Literal["doctor_report"] = "doctor_report"
    version: str
    platform: str
    simulated: bool
    checks: list[DoctorCheck]
    devices: list[DeviceEntry]


class KeycodeInfo(Model):
    keycode: str
    label: str
    description: str | None = None
    masked: bool = False
    aliases: list[str] = Field(default_factory=list)


class KeycodeList(Envelope):
    kind: Literal["keycode_list"] = "keycode_list"
    device: DeviceIdentity
    keycodes: list[KeycodeInfo]


class ViewResult(Envelope):
    kind: Literal["view"] = "view"
    device: DeviceIdentity
    layers: list[int]
    format: Literal["text", "png"]
    text: str
    artifact_path: str | None = None
    proposal_id: str | None = None


class VersionInfo(Envelope):
    kind: Literal["version"] = "version"
    version: str
    vial_gui_revision: str
    supported_via_protocols: list[int]
    supported_vial_protocols: list[int]
    python: str
    platform: str
    frozen: bool


InspectResult.model_rebuild()

# `vialtic schema` and the checked-in schema fixtures use this table.
PUBLIC_SCHEMAS: dict[str, type[BaseModel]] = {
    "error": ErrorInfo,
    "device_list": DeviceList,
    "device_inspect": InspectResult,
    "snapshot": Snapshot,
    "change_set": ChangeSet,
    "proposal": Proposal,
    "apply_result": ApplyResult,
    "unlock_status": UnlockStatus,
    "recovery_journal": Journal,
    "doctor_report": DoctorReport,
    "keycode_list": KeycodeList,
    "view": ViewResult,
    "version": VersionInfo,
}
