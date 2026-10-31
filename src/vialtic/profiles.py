# SPDX-License-Identifier: GPL-2.0-or-later
"""Named profiles and interchange files. Every file here is hostile until proven otherwise.

Nothing in a file is evaluated or executed. Files become typed changes that go through the
same validation as any other request, and only a normal proposal can reach a keyboard.
Upstream's restore_layout is never used: it writes immediately, maps unreadable keycodes to
KC_NO and drops extra entries without saying so.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import stat
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from . import __version__, proposals
from . import vial_adapter as va
from .errors import VialticError
from .models import (
    Change, Configuration, LightingState, Profile, ProfileConfiguration, ProfileEncoder, ProfileKey, ProfileSource,
    SetAltRepeatKeyChange, SetComboChange, SetEncoderChange, SetKeyChange, SetKeyOverrideChange, SetLayoutOptionChange,
    SetLightingChange, SetMacroChange, SetQmkSettingChange, SetTapDanceChange, Snapshot,
)

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_NESTING = 16
MAX_LAYERS = 32
MAX_MATRIX = 256
MAX_ENTRIES = 256
MAX_MACRO_ACTIONS = 4096
MAX_KEYCODE_TEXT = 64
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VIL_KEYS = {"version", "uid", "layout", "encoder_layout", "layout_options", "macro", "vial_protocol", "via_protocol",
            "tap_dance", "combo", "key_override", "alt_repeat_key", "settings"}
MACRO_TAGS = {"text", "tap", "down", "up", "delay"}
LIGHTING_OMISSION = "Lighting: .vil files do not carry it, so this file leaves lighting out."
MACROS_LEFT_OUT = "Macros: left out because macro contents can hold anything a person typed. Use --include-macros to include them."


def invalid(message: str, **details: Any) -> VialticError:
    """Never repeats values from the file, which may hold macro contents."""
    return VialticError("invalid_file", message, details=details)


def incompatible(message: str, **details: Any) -> VialticError:
    return VialticError("profile_incompatible", f"{message} Nothing was changed.", details=details,
                        next_steps=["There is no way to force this. To move settings to another keyboard, create explicit "
                                    "changes with `vialtic plan --changes`."])


def check_name(name: str) -> str:
    if not PROFILE_NAME.match(name) or name.endswith(".json"):
        raise VialticError("invalid_request", "A profile name takes up to 64 letters, digits, dots, dashes and underscores, "
                                              "starts with a letter or digit, and does not end in .json.")
    return name


# Reading untrusted files.

def read_untrusted(path: pathlib.Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Bounded read of a regular file, refusing symlinks, FIFOs and devices without blocking on them."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if os.path.islink(path):
            raise invalid(f"{path} is a symbolic link. Vialtic reads only regular files.") from exc
        raise invalid(f"Could not read {path}: {exc.strerror}.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise invalid(f"{path} is not a regular file.")
        if info.st_size > limit:
            raise invalid(f"{path} is {info.st_size} bytes, over the {limit} byte limit.")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise invalid(f"{path} is over the {limit} byte limit.")
    return data


def _nesting(text: str) -> int:
    """Deepest bracket nesting, found before parsing so a hostile file cannot exhaust the stack."""
    deepest = depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name: str) -> None:
    raise ValueError("not a JSON number")


def parse_json(raw: bytes, path: Any) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise invalid(f"{path} is not UTF-8 text.") from exc
    if _nesting(text) > MAX_NESTING:
        raise invalid(f"{path} is nested more deeply than any Vialtic or Vial file.")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:
        raise invalid(f"{path} is not valid JSON, or repeats a key.") from exc


def _int(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise invalid(f"{what} must be a number from {low} to {high}.")
    return value


def _list(value: Any, limit: int, what: str) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise invalid(f"{what} must be a list of at most {limit} items.")
    return value


def _keycode_text(value: Any, what: str) -> str:
    """Keycodes in Vial files are names, or raw numbers in old ones. Either way only parse_keycode decides."""
    if isinstance(value, str):
        if not 0 < len(value) <= MAX_KEYCODE_TEXT:
            raise invalid(f"{what} is too long to be a keycode.")
        return value
    return f"0x{_int(value, 0, 0xFFFF, what):04x}"


# .vil files.

def parse_vil(data: Any, name: str, now: datetime) -> tuple[Profile, list[str]]:
    """Validate structure only. Whether it fits a keyboard is decided against a live one."""
    if not isinstance(data, dict):
        raise invalid("A .vil file must hold a JSON object.")
    if data.get("version") != 1 or isinstance(data.get("version"), bool):
        raise invalid("This .vil file is not version 1, the only version Vial defines at the pinned revision.")
    uid = _int(data.get("uid"), 0, 2 ** 64 - 1, "uid")
    unknown = {key: value for key, value in data.items() if key not in VIL_KEYS}
    notes = []
    if unknown:
        notes.append(f"Fields Vialtic does not know are kept with the profile and never applied: {', '.join(sorted(unknown))}.")

    if "layout" not in data:
        raise invalid("This .vil file has no layout.")
    layers = _list(data["layout"], MAX_LAYERS, "layout")
    keys: list[ProfileKey] = []
    rows = cols = 0
    numeric = False
    for l, layer in enumerate(layers):
        rows = max(rows, len(_list(layer, MAX_MATRIX, "A layout layer")))
        for r, row in enumerate(layer):
            cols = max(cols, len(_list(row, MAX_MATRIX, "A layout row")))
            for c, code in enumerate(row):
                if code == -1 and not isinstance(code, bool):
                    continue
                numeric = numeric or not isinstance(code, str)
                keys.append(ProfileKey(layer=l, row=r, col=c, keycode=_keycode_text(code, "A key")))
    encoders: list[ProfileEncoder] | None = [] if "encoder_layout" in data else None
    for l, layer in enumerate(_list(data.get("encoder_layout", []), MAX_LAYERS, "encoder_layout")):
        for index, encoder in enumerate(_list(layer, MAX_ENTRIES, "An encoder layer")):
            pair = _list(encoder, 2, "An encoder")
            if len(pair) != 2:
                raise invalid("An encoder must have two directions.")
            for direction, code in enumerate(pair):
                if code == -1 and not isinstance(code, bool):
                    continue
                numeric = numeric or not isinstance(code, str)
                encoders.append(ProfileEncoder(layer=l, index=index, direction=direction, keycode=_keycode_text(code, "An encoder")))

    macros = None
    if "macro" in data:
        if data["macro"] is not None:
            macros = _parse_vil_macros(data["macro"])
    else:
        notes.append("This file has no macros, so a plan from it leaves macros alone.")
    settings = None
    if "settings" in data:
        if not isinstance(data["settings"], dict) or len(data["settings"]) > 1024:
            raise invalid("settings must be an object of at most 1024 values.")
        settings = {}
        for key, value in data["settings"].items():
            if not re.fullmatch(r"\d{1,5}", key):
                raise invalid("settings keys must be setting numbers.")
            settings[key] = _int(value, 0, 2 ** 32 - 1, "A setting")
    protocols = {key: _int(data[key], -1, 2 ** 32 - 1, key) for key in ("vial_protocol", "via_protocol") if key in data}
    if numeric and "vial_protocol" not in protocols:
        raise invalid("This .vil file holds raw keycodes and does not say which protocol they belong to.")
    configuration = ProfileConfiguration(
        matrix=[len(layers), rows, cols], keymap=keys, encoders=encoders,
        layout_options=_int(data["layout_options"], -1, 2 ** 32 - 1, "layout_options") if "layout_options" in data else None,
        macros=macros, tap_dance=_entries(data, "tap_dance", _vil_tap_dance), combos=_entries(data, "combo", _vil_combo),
        key_overrides=_entries(data, "key_override", _vil_key_override),
        alt_repeat_keys=_entries(data, "alt_repeat_key", _vil_alt_repeat), settings=settings)
    source = ProfileSource(vial_uid=f"0x{uid:016x}", vial_protocol=protocols.get("vial_protocol"), via_protocol=protocols.get("via_protocol"),
                           numeric_keycodes=numeric)
    profile = Profile(name=name, created_at=now, origin="vil", vialtic_version=__version__, source=source, configuration=configuration,
                      omissions=[LIGHTING_OMISSION], unknown_fields=unknown)
    return profile, notes


def _entries(data: dict, key: str, parse) -> list | None:
    if key not in data:
        return None
    return [parse(entry) for entry in _list(data[key], MAX_ENTRIES, key)]


def _vil_tap_dance(entry: Any) -> list:
    entry = _list(entry, 5, "A tap dance entry")
    if len(entry) != 5:
        raise invalid("A tap dance entry has four keycodes and a term.")
    return [*(_keycode_text(code, "A tap dance keycode") for code in entry[:4]), _int(entry[4], 0, 0xFFFF, "A tapping term")]


def _vil_combo(entry: Any) -> list:
    entry = _list(entry, 5, "A combo entry")
    if len(entry) != 5:
        raise invalid("A combo entry has four inputs and an output.")
    return [_keycode_text(code, "A combo keycode") for code in entry]


def _exact_fields(entry: Any, fields: set[str], what: str) -> dict:
    if not isinstance(entry, dict) or set(entry) != fields:
        raise invalid(f"{what} must have exactly the fields {', '.join(sorted(fields))}.")
    return entry


def _vil_key_override(entry: Any) -> dict:
    entry = _exact_fields(entry, {"trigger", "replacement", "layers", "trigger_mods", "negative_mod_mask", "suppressed_mods",
                                  "options"}, "A key override")
    return {"trigger": _keycode_text(entry["trigger"], "A key override keycode"),
            "replacement": _keycode_text(entry["replacement"], "A key override keycode"),
            "layers": _int(entry["layers"], 0, 0xFFFF, "A layer mask"),
            **{name: _int(entry[name], 0, 0xFF, name) for name in ("trigger_mods", "negative_mod_mask", "suppressed_mods", "options")}}


def _vil_alt_repeat(entry: Any) -> dict:
    entry = _exact_fields(entry, {"keycode", "alt_keycode", "allowed_mods", "options"}, "An alternate repeat entry")
    return {"keycode": _keycode_text(entry["keycode"], "An alternate repeat keycode"),
            "alt_keycode": _keycode_text(entry["alt_keycode"], "An alternate repeat keycode"),
            "allowed_mods": _int(entry["allowed_mods"], 0, 0xFF, "allowed_mods"), "options": _int(entry["options"], 0, 0xFF, "options")}


def _parse_vil_macros(value: Any) -> list[list[list[Any]]]:
    macros = []
    total = 0
    for macro in _list(value, MAX_ENTRIES, "macro"):
        actions = []
        for action in _list(macro, MAX_MACRO_ACTIONS, "A macro"):
            if not isinstance(action, list) or not action or action[0] not in MACRO_TAGS:
                raise invalid("A macro holds an action this version does not know. Nothing is dropped silently, so it was refused.")
            total += len(action)
            if total > MAX_MACRO_ACTIONS * 4:
                raise invalid("The macros are larger than any keyboard can hold.")
            actions.append(list(action))
        macros.append(actions)
    return macros


def parse_vialtic(data: dict, name: str, now: datetime) -> Profile:
    kind = data.get("kind")
    try:
        if kind == "profile":
            profile = Profile.model_validate(data)
            return profile.model_copy(update={"name": name})
        if kind == "snapshot":
            return from_snapshot(name, Snapshot.model_validate(data), now, origin="vialtic")
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"] and error["type"] == "extra_forbidden"})
        raise invalid("This file does not match the format of this Vialtic version, so it was refused rather than half understood.",
                      errors=exc.error_count(), unknown_fields=fields) from exc
    raise invalid("This file is neither a .vil file nor a Vialtic profile or snapshot.")


def parse_file(path: pathlib.Path, name: str, now: datetime) -> tuple[Profile, list[str]]:
    data = parse_json(read_untrusted(path), path)
    if isinstance(data, dict) and "kind" in data:
        profile = parse_vialtic(data, name, now)
        validate_shape(profile)
        return profile, []
    profile, notes = parse_vil(data, name, now)
    validate_shape(profile)
    return profile, notes


def validate_shape(profile: Profile) -> None:
    """Checks that do not need a keyboard, for files that went through the models."""
    config = profile.configuration
    if len(config.keymap or []) > MAX_LAYERS * MAX_MATRIX * MAX_MATRIX or len(config.encoders or []) > MAX_LAYERS * MAX_ENTRIES * 2:
        raise invalid("The file holds more entries than any keyboard can have.")
    if config.keymap is None:
        raise invalid("This file has no keymap.")
    try:
        for entry in (config.tap_dance or []):
            _vil_tap_dance(entry)
        for entry in (config.combos or []):
            _vil_combo(entry)
        for entry in (config.key_overrides or []):
            _vil_key_override(entry)
        for entry in (config.alt_repeat_keys or []):
            _vil_alt_repeat(entry)
        if config.macro_buffer_b64 is not None:
            base64.b64decode(config.macro_buffer_b64, validate=True)
    except ValueError as exc:
        raise invalid("The macro buffer in this file is not valid.") from exc


# Between snapshots, profiles and files.

def from_snapshot(name: str, snapshot: Snapshot, now: datetime, origin: str = "keyboard", kb: Any = None) -> Profile:
    config = snapshot.configuration
    device = snapshot.device
    omissions, lossy = [], []
    macros = config.macros
    if macros.buffer_b64 is None and macros.count:
        omissions.append(MACROS_LEFT_OUT)
    if kb is not None and macros.buffer_b64:
        for index, segment in enumerate(va.macro_segments(base64.b64decode(macros.buffer_b64), macros.count)):
            if kb.macro_serialize(kb.macro_deserialize(segment)) != segment:
                lossy.append(index)
    return Profile(
        name=name, created_at=now, origin=origin, vialtic_version=__version__, omissions=omissions,
        source=ProfileSource(device_name=device.name, vial_uid=device.vial_uid, vendor_id=device.vendor_id, product_id=device.product_id,
                             via_protocol=device.via_protocol, vial_protocol=device.vial_protocol, layout_fingerprint=device.layout_fingerprint,
                             binding_id=device.binding_id),
        configuration=ProfileConfiguration(
            matrix=[snapshot.capabilities.layers, snapshot.capabilities.rows, snapshot.capabilities.cols],
            keymap=[ProfileKey(layer=e.layer, row=e.row, col=e.col, keycode=e.keycode) for e in config.keymap],
            encoders=[ProfileEncoder(layer=e.layer, index=e.index, direction=e.direction, keycode=e.keycode) for e in config.encoders],
            layout_options=config.layout_options, macro_count=macros.count, macro_buffer_b64=macros.buffer_b64, macros=macros.macros,
            lossy_macros=lossy, tap_dance=[list(e) for e in config.tap_dance], combos=[list(e) for e in config.combos],
            key_overrides=[dict(e) for e in config.key_overrides], alt_repeat_keys=[dict(e) for e in config.alt_repeat_keys],
            settings=dict(config.settings.values), lighting=config.lighting))


def redact(profile: Profile) -> Profile:
    config = profile.configuration.model_copy(update=dict(macro_buffer_b64=None, macros=None, macros_redacted=profile.configuration.has_macros()))
    return profile.model_copy(update={"configuration": config})


def without_macros(profile: Profile) -> Profile:
    """For a file: left out for real, and said so."""
    if not profile.configuration.has_macros():
        return profile
    return redact(profile).model_copy(update={"omissions": [*profile.omissions, MACROS_LEFT_OUT]})


def to_vil(profile: Profile) -> tuple[dict, list[str]]:
    """Upstream's save_layout format. Returns the data and what it cannot carry."""
    config = profile.configuration
    if config.matrix is None or config.keymap is None:
        raise VialticError("invalid_request", "This profile does not have enough to write a .vil file.")
    layers, rows, cols = config.matrix
    omissions = [LIGHTING_OMISSION]
    layout = [[[-1 for _ in range(cols)] for _ in range(rows)] for _ in range(layers)]
    for key in config.keymap:
        layout[key.layer][key.row][key.col] = key.keycode
    count = max((e.index + 1 for e in config.encoders or []), default=0)
    encoder_layout = [[[-1, -1] for _ in range(count)] for _ in range(layers)]
    for encoder in config.encoders or []:
        encoder_layout[encoder.layer][encoder.index][encoder.direction] = encoder.keycode
    data: dict[str, Any] = {
        "version": 1, "uid": int(profile.source.vial_uid, 16), "layout": layout, "encoder_layout": encoder_layout,
        "layout_options": -1 if config.layout_options is None else config.layout_options,
        "vial_protocol": profile.source.vial_protocol, "via_protocol": profile.source.via_protocol,
        "tap_dance": config.tap_dance or [], "combo": config.combos or [], "key_override": config.key_overrides or [],
        "alt_repeat_key": config.alt_repeat_keys or [], "settings": config.settings or {},
    }
    if config.macros is not None:
        data["macro"] = config.macros
        if config.lossy_macros:
            omissions.append(f"Macros {config.lossy_macros}: hold bytes the .vil format cannot represent exactly. The Vialtic format can.")
    elif config.macro_buffer_b64 is None:
        omissions.append(MACROS_LEFT_OUT)
    data = {key: value for key, value in data.items() if value is not None}
    data.update(profile.unknown_fields)
    return data, omissions


# Against a keyboard.

def _check_identity(profile: Profile, snapshot: Snapshot) -> list[str]:
    source, device = profile.source, snapshot.device
    if source.vial_uid != device.vial_uid:
        raise incompatible(f"This profile is for keyboard firmware {source.vial_uid}, and this keyboard reports {device.vial_uid}.")
    if source.layout_fingerprint is not None and source.layout_fingerprint != device.layout_fingerprint:
        raise incompatible("This profile was made for a different physical layout definition.")
    notes = []
    numeric = source.numeric_keycodes or any(key.keycode.startswith("0x") for key in profile.configuration.keymap or [])
    if source.vial_protocol is None:
        notes.append("This file does not say which Vial protocol it came from. Its keycode names were checked against this keyboard.")
    elif not va.same_encoding(source.vial_protocol, device.vial_protocol):
        if numeric or profile.configuration.macro_buffer_b64 is not None:
            raise incompatible(f"This profile holds raw values from Vial protocol {source.vial_protocol}, which mean something else "
                               f"on protocol {device.vial_protocol}.")
        notes.append(f"This file came from Vial protocol {source.vial_protocol} and this keyboard uses {device.vial_protocol}. Its keycode "
                     "names were checked against this keyboard.")
    if source.binding_id is not None and source.binding_id != device.binding_id:
        notes.append("This profile was saved from a keyboard with the same firmware and layout but a different USB identity.")
    return notes


def _check_shape(config: ProfileConfiguration, snapshot: Snapshot) -> None:
    caps, current = snapshot.capabilities, snapshot.configuration
    if config.matrix is not None and config.matrix != [caps.layers, caps.rows, caps.cols]:
        raise incompatible(f"This profile has {config.matrix[0]} layers of {config.matrix[1]}x{config.matrix[2]}, and this keyboard has "
                           f"{caps.layers} of {caps.rows}x{caps.cols}.")
    if {(k.layer, k.row, k.col) for k in config.keymap or []} != {(e.layer, e.row, e.col) for e in current.keymap}:
        raise incompatible("This profile's keys are not the keys this keyboard has.")
    if config.encoders is not None and {(e.layer, e.index, e.direction) for e in config.encoders} != {
            (e.layer, e.index, e.direction) for e in current.encoders}:
        raise incompatible("This profile's encoders are not the encoders this keyboard has.")
    for name, entries, count in (("tap dance entries", config.tap_dance, caps.tap_dance_count), ("combos", config.combos, caps.combo_count),
                                 ("key overrides", config.key_overrides, caps.key_override_count),
                                 ("alternate repeat entries", config.alt_repeat_keys, caps.alt_repeat_key_count),
                                 ("macros", config.macros, caps.macro_count)):
        if entries is not None and len(entries) > count:
            raise incompatible(f"This profile has {len(entries)} {name}, and this keyboard has {count}.")
    if config.macro_buffer_b64 is not None and config.macro_count != caps.macro_count:
        raise incompatible(f"This profile has {config.macro_count} macros, and this keyboard has {caps.macro_count}.")


def _layout_changes(value: int | None, snapshot: Snapshot) -> list[Change]:
    labels = snapshot.capabilities.layout_option_labels
    if value is None or (value == -1 and not labels):
        return []
    if not labels:
        raise incompatible("This profile sets layout options, and this keyboard has none.")
    if value == -1:
        return []
    choices = va.layout_choices(labels, value)
    rebuilt = snapshot.configuration.layout_options
    changes: list[Change] = []
    for index, choice in enumerate(choices):
        rebuilt = va.set_layout_choice(labels, rebuilt, index, choice)
        changes.append(SetLayoutOptionChange(index=index, choice=choice))
    if rebuilt != value:
        raise incompatible("This profile's layout options use bits this keyboard's options do not define.")
    return changes


def _parsed_macro_changes(kb: Any, macros: list, snapshot: Snapshot) -> list[Change]:
    """Only macros that differ are touched, compared as the bytes they encode to."""
    current = va.macro_segments(va.decode_macro_buffer(snapshot.configuration.macros.buffer_b64), kb.macro_count)
    changes: list[Change] = []
    for index, actions in enumerate(macros):
        data, _ = va.encode_macro(kb, index, actions)
        if data != current[index]:
            changes.append(SetMacroChange(index=index, actions=actions))
    return changes


def _lighting_changes(lighting: LightingState | None, snapshot: Snapshot, mode: str) -> list[Change]:
    if lighting is None or mode == "skip":
        return []
    current = snapshot.configuration.lighting
    changes: list[Change] = []
    for system, fields in va.LIGHTING_FIELDS.items():
        wanted, have = getattr(lighting, system), getattr(current, system)
        if wanted is None:
            continue
        if have is None:
            raise incompatible(f"This profile sets {system} lighting, and this keyboard does not have it.")
        values = {name: wanted[name] for name in fields}
        if values != {name: have[name] for name in fields}:
            changes.append(SetLightingChange(system=system, save=mode == "save", **values))
    return changes


def _setting_changes(settings: dict[str, int] | None, snapshot: Snapshot) -> tuple[list[Change], list[str]]:
    if settings is None:
        return [], []
    current = snapshot.configuration.settings
    changes: list[Change] = []
    unknown = []
    for key, value in settings.items():
        qsid = int(key)
        if not va.QmkSettings.is_qsid_supported(qsid):
            unknown.append(qsid)
        elif key not in current.values:
            raise incompatible(f"This profile sets QMK setting {qsid}, which this keyboard does not expose.")
        elif current.values[key] != value:
            changes.append(SetQmkSettingChange(qsid=qsid, value=value))
    notes = [f"QMK settings {unknown} in this profile are not in the pinned Vial tables. They are kept with it and not applied."] if unknown else []
    return changes, notes


def _differing_keycode(kb: Any, wanted: str, current_keycode: str, current_raw: int) -> bool:
    if wanted == current_keycode:
        return False
    value, _ = va.parse_keycode(kb, wanted)
    return value.raw != current_raw


def plan_changes(profile: Profile, snapshot: Snapshot, kb: Any, lighting_mode: str = "save") -> tuple[list[Change], bytes | None, list[str]]:
    """Refuses anything incompatible. Returns typed changes, a raw macro buffer if any, and notes."""
    notes = _check_identity(profile, snapshot)
    config = profile.configuration
    _check_shape(config, snapshot)
    current = snapshot.configuration
    changes: list[Change] = []
    try:
        keys = {(e.layer, e.row, e.col): e for e in current.keymap}
        for key in config.keymap or []:
            have = keys[(key.layer, key.row, key.col)]
            if _differing_keycode(kb, key.keycode, have.keycode, have.raw):
                changes.append(SetKeyChange(layer=key.layer, row=key.row, col=key.col, keycode=key.keycode))
        encoders = {(e.layer, e.index, e.direction): e for e in current.encoders}
        for encoder in config.encoders or []:
            have = encoders[(encoder.layer, encoder.index, encoder.direction)]
            if _differing_keycode(kb, encoder.keycode, have.keycode, have.raw):
                changes.append(SetEncoderChange(layer=encoder.layer, index=encoder.index, direction=encoder.direction, keycode=encoder.keycode))
        changes += _layout_changes(config.layout_options, snapshot)
        for index, entry in enumerate(config.tap_dance or []):
            if list(entry) != list(current.tap_dance[index]):
                changes.append(SetTapDanceChange(index=index, on_tap=entry[0], on_hold=entry[1], on_double_tap=entry[2], on_tap_hold=entry[3],
                                                 tapping_term=entry[4]))
        for index, entry in enumerate(config.combos or []):
            if list(entry) != list(current.combos[index]):
                changes.append(SetComboChange(index=index, keys=list(entry[:4]), output=entry[4]))
        for index, entry in enumerate(config.key_overrides or []):
            if dict(entry) != current.key_overrides[index]:
                changes.append(SetKeyOverrideChange(index=index, **entry))
        for index, entry in enumerate(config.alt_repeat_keys or []):
            if dict(entry) != current.alt_repeat_keys[index]:
                changes.append(SetAltRepeatKeyChange(index=index, **entry))
        settings, setting_notes = _setting_changes(config.settings, snapshot)
        changes += settings
        notes += setting_notes
        changes += _lighting_changes(config.lighting, snapshot, lighting_mode)
        raw = None
        if config.macro_buffer_b64 is not None:
            raw = va.decode_macro_buffer(config.macro_buffer_b64)
            if profile.origin != "keyboard":
                notes.append("This file holds a raw macro buffer. Vialtic writes those bytes as they are.")
        elif config.macros is not None:
            changes += _parsed_macro_changes(kb, config.macros, snapshot)
    except ValidationError as exc:
        raise invalid("A value in this profile has the wrong type.", errors=exc.error_count()) from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise invalid(f"This profile holds values that cannot be used ({type(exc).__name__}).") from exc
    if config.lighting is None:
        notes.append("This profile has no lighting, so lighting is left alone.")
    elif lighting_mode == "skip":
        notes.append("Lighting in this profile was skipped as requested.")
    if not config.has_macros() and current.macros.count:
        notes.append("This profile has no macros, so macros are left alone.")
    return changes, raw, notes
