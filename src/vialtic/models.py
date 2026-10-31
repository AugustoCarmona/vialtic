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

class SetKeyChange(Model):
    type: Literal["set_key"] = "set_key"
    layer: int
    row: int
    col: int
    keycode: str


class SetEncoderChange(Model):
    type: Literal["set_encoder"] = "set_encoder"
    layer: int
    index: int
    direction: int
    keycode: str


Change = Annotated[Union[SetKeyChange, SetEncoderChange], Field(discriminator="type")]


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


Operation = Annotated[Union[SetKeyOperation, SetEncoderOperation], Field(discriminator="type")]


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
