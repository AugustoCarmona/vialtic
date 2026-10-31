# SPDX-License-Identifier: GPL-2.0-or-later
"""The boundary around the vendored Vial GUI logic.

Everything that touches the upstream modules lives here. Upstream keycode tables
are process-global, so sessions hold `CONTEXT_LOCK` and rebuild the tables for
their own keyboard (see `activate`).
"""
from __future__ import annotations

import ast
import base64
import inspect
import json
import lzma
import pathlib
import re
import struct
import sys
import threading

from .errors import VialticError
from .models import (
    AltRepeatKeyValue, Capabilities, ComboValue, Configuration, KeyOverrideValue, EncoderEntry, KeyGeometry, KeycodeInfo, KeycodeValue, KeymapEntry,
    LayoutGeometry, LightingState, MacroState, SettingsState, TapDanceValue,
)

VIAL_GUI_REVISION = "aef8222a2d0429a183b2ed692d5f9efcfd383f08"


def vendor_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    # Installed/frozen builds carry a copy; a source checkout uses vendor/ directly.
    for candidate in (here / "_vendor" / "vial", here.parents[1] / "vendor" / "vial"):
        if (candidate / "upstream").is_dir():
            return candidate
    raise RuntimeError(f"vendored Vial sources not found near {here}")


def _install_vendor_path() -> None:
    root = vendor_root()
    for part in ("upstream", "headless"):
        path = str(root / part)
        if path not in sys.path:
            sys.path.insert(0, path)


_install_vendor_path()

# Upstream order matters: keycodes finishes importing any_keycode lazily, not the reverse.
from keycodes import keycodes as keycode_tables  # noqa: E402
from keycodes.keycodes import RESET_KEYCODE, Keycode, recreate_keyboard_keycodes  # noqa: E402
from any_keycode import AnyKeycode, functions as ANY_KEYCODE_FUNCTIONS  # noqa: E402
from editor.qmk_settings import QmkSettings  # noqa: E402
from protocol import constants as proto  # noqa: E402
from protocol.alt_repeat_key import AltRepeatKeyEntry  # noqa: E402
from protocol.key_override import KeyOverrideEntry  # noqa: E402
from protocol.keyboard_comm import (  # noqa: E402
    SUPPORTED_VIA_PROTOCOL, SUPPORTED_VIAL_PROTOCOL, Keyboard, ProtocolError,
)
from unlocker import UnlockRequired  # noqa: E402
from util import MSG_LEN, VIAL_SERIAL_NUMBER_MAGIC, VIBL_SERIAL_NUMBER_MAGIC, hid_send  # noqa: E402

__all__ = [
    "CONTEXT_LOCK", "HeadlessKeyboard", "MSG_LEN", "ProtocolError", "RESET_KEYCODE", "SUPPORTED_VIAL_PROTOCOL",
    "SUPPORTED_VIA_PROTOCOL", "UnlockRequired", "VIAL_SERIAL_NUMBER_MAGIC", "VIBL_SERIAL_NUMBER_MAGIC", "hid_send",
    "proto", "Keycode", "QmkSettings",
]

CONTEXT_LOCK = threading.RLock()

MAX_DEFINITION_COMPRESSED = 256 * 1024
MAX_DEFINITION_JSON = 4 * 1024 * 1024
MAX_KEYMAP_BYTES = 0xFFFF
MAX_KEYCODE_TEXT = 64

# Families compared against a proposal's baseline before writing. Lighting joins
# them only in a proposal that changes it: lighting keys on the keyboard change it
# without any host involved, and that must not invalidate an unrelated proposal.
CHECKED_FAMILIES = [
    "keymap", "encoders", "layout_options", "macros", "tap_dance", "combos",
    "key_overrides", "alt_repeat_keys", "settings",
]
ALL_FAMILIES = CHECKED_FAMILIES + ["lighting"]
RESTORABLE_FAMILIES = {"keymap", "encoders", "layout_options", "tap_dance", "combos", "key_overrides", "alt_repeat_keys",
                       "settings"}


class HeadlessKeyboard(Keyboard):
    """Upstream Keyboard with explicit limits on the firmware-provided definition."""

    def reload_layout(self, sideload_json=None):
        if sideload_json is None:
            self.reload_via_protocol()
            data = self.usb_send(self.dev, struct.pack("BB", proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_GET_KEYBOARD_ID),
                                 retries=20)
            self.vial_protocol, self.keyboard_id = struct.unpack("<IQ", data[0:12])
            # Refuse before parsing anything an unknown protocol might encode differently.
            self.check_protocol_version()
            sideload_json = self._fetch_definition()
        super().reload_layout(sideload_json)
        self.sideload = False

    def _fetch_definition(self):
        data = self.usb_send(self.dev, struct.pack("BB", proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_GET_SIZE), retries=20)
        size = struct.unpack("<I", data[0:4])[0]
        if not 0 < size <= MAX_DEFINITION_COMPRESSED:
            raise VialticError("malformed_device_data", f"Firmware reports an implausible definition size ({size} bytes).")
        payload = b""
        block = 0
        remaining = size
        while remaining > 0:
            data = self.usb_send(self.dev, struct.pack("<BBI", proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_GET_DEFINITION,
                                                       block), retries=20)
            payload += data[:remaining] if remaining < MSG_LEN else data
            block += 1
            remaining -= MSG_LEN
        decoder = lzma.LZMADecompressor()
        try:
            text = decoder.decompress(payload, max_length=MAX_DEFINITION_JSON + 1)
        except lzma.LZMAError as exc:
            raise VialticError("malformed_device_data", f"Firmware definition is not valid LZMA data: {exc}") from exc
        if len(text) > MAX_DEFINITION_JSON or not decoder.eof:
            raise VialticError("malformed_device_data", "Firmware definition exceeds the supported size or is truncated.")
        try:
            definition = json.loads(text)
        except ValueError as exc:
            raise VialticError("malformed_device_data", f"Firmware definition is not valid JSON: {exc}") from exc
        validate_definition(definition)
        return definition

    def reload_layers(self):
        super().reload_layers()
        if not 0 < self.layers <= 32 or self.layers * self.rows * self.cols * 2 > MAX_KEYMAP_BYTES:
            raise VialticError("malformed_device_data", f"Implausible keymap dimensions: {self.layers} layers, "
                                                         f"{self.rows} rows, {self.cols} cols.")


def validate_definition(definition) -> None:
    try:
        rows = definition["matrix"]["rows"]
        cols = definition["matrix"]["cols"]
        keymap = definition["layouts"]["keymap"]
    except (KeyError, TypeError) as exc:
        raise VialticError("malformed_device_data", f"Firmware definition lacks a required field: {exc}") from exc
    if not (isinstance(rows, int) and isinstance(cols, int) and 0 < rows <= 256 and 0 < cols <= 256):
        raise VialticError("malformed_device_data", f"Firmware definition has an invalid matrix: {rows}x{cols}.")
    if not isinstance(keymap, list) or not keymap:
        raise VialticError("malformed_device_data", "Firmware definition has an empty layout.")


def activate(kb: Keyboard) -> None:
    """Point the process-global keycode tables at this keyboard. Hold CONTEXT_LOCK."""
    recreate_keyboard_keycodes(kb)


# Display.

def display_label(keycode: str) -> str:
    """Short, ASCII-safe label. Transparent and disabled keys stay distinguishable."""
    if keycode == "KC_TRNS":
        return "___"
    if keycode == "KC_NO":
        return "XXX"
    if keycode.startswith("USER"):
        label = Keycode.label(keycode).replace("\n", " ")
        if label.isascii():
            return label
    return keycode.replace("KC__", "KC_").replace("KC_", "")


def to_value(keycode: str) -> KeycodeValue:
    return KeycodeValue(raw=Keycode.deserialize(keycode, reraise=True), keycode=keycode, label=display_label(keycode))


def value_from_raw(raw: int) -> KeycodeValue:
    keycode = Keycode.serialize(raw)
    return KeycodeValue(raw=raw, keycode=keycode, label=display_label(keycode))


# Keycode input validation. Upstream evaluates names through simpleeval and maps
# failures to KC_NO; requests get a strict grammar and fail loudly instead.

_KEYCODE_TEXT = re.compile(r"^[A-Za-z0-9_(),| ]+$")
_LAYER_FUNCTIONS = {"MO", "TO", "DF", "TG", "TT", "OSL", "LT", "LM"}
_LITERAL_FIRST_ARGUMENT = _LAYER_FUNCTIONS | {"TD"}
_MOD_ARGUMENT = {"MT": 0, "OSM": 0, "LM": 1}  # the only places where modifiers may be combined with |
_CANONICAL_LAYER = re.compile(r"^(?:MO|DF|PDF|TG|TT|OSL|TO)\((\d+)\)$|^LT(\d+)\(")
_CANONICAL_TD = re.compile(r"^TD\((\d+)\)$")
_CANONICAL_MACRO = re.compile(r"^M(\d+)$")


def _invalid(text: str, why: str, code: str = "invalid_keycode") -> VialticError:
    return VialticError(code, f"Invalid keycode {text!r}: {why}",
                        next_steps=["List valid names with `vialtic keycodes`."])


def _literal_layers(tree: ast.AST, text: str) -> list[int]:
    """Validate the expression shape and collect literal layer arguments."""
    layers: list[int] = []
    known_names = AnyKeycode().names

    def walk(node: ast.AST, allow_or: bool = False) -> None:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, int) or isinstance(node.value, bool):
                raise _invalid(text, "only integer literals are allowed")
        elif isinstance(node, ast.Name):
            if node.id not in known_names and node.id not in keycode_tables.KEYCODES_MAP:
                raise _invalid(text, f"unknown name {node.id}")
            if node.id.startswith("MOD_") and not allow_or:
                raise _invalid(text, f"{node.id} is a modifier mask, not a keycode")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if not allow_or:
                raise _invalid(text, "| is only valid between modifiers")
            walk(node.left, True)
            walk(node.right, True)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise _invalid(text, "unsupported call form")
            name = node.func.id
            function = ANY_KEYCODE_FUNCTIONS.get(name)
            if function is None:
                raise _invalid(text, f"unknown function {name}")
            required = [p for p in inspect.signature(function).parameters.values() if p.default is p.empty]
            if len(node.args) != len(required):
                raise _invalid(text, f"{name} takes {len(required)} argument(s)")
            if name in _LITERAL_FIRST_ARGUMENT:
                first = node.args[0]
                if not isinstance(first, ast.Constant) or isinstance(first.value, bool) or not isinstance(first.value, int):
                    raise _invalid(text, f"the first argument of {name} must be a plain number")
                if name in _LAYER_FUNCTIONS:
                    layers.append(first.value)
            match = re.fullmatch(r"LT(\d+)", name)
            if match:
                layers.append(int(match.group(1)))
            for index, arg in enumerate(node.args):
                walk(arg, _MOD_ARGUMENT.get(name) == index)
        else:
            raise _invalid(text, f"unsupported syntax ({type(node).__name__})")

    walk(tree)
    return layers


def _numeric_reference_problem(kb: Keyboard, raw: int) -> tuple[str, str] | None:
    """Check a code with no name against this keyboard's layers and macros.

    Names exist for every layer and macro the keyboard has, so a nameless value inside
    those ranges points past the end. Only meaningful for values without a name.
    """
    v6 = Keycode.protocol == 6
    table = keycode_tables.keycodes_v6.kc if v6 else keycode_tables.keycodes_v5.kc
    span = 0x20 if v6 else 0x100
    for name in ("QK_TO", "QK_MOMENTARY", "QK_DEF_LAYER", "QK_PERSISTENT_DEF_LAYER", "QK_TOGGLE_LAYER",
                 "QK_ONE_SHOT_LAYER", "QK_LAYER_TAP_TOGGLE"):
        base = table.get(name)
        if base is not None and base <= raw < base + span:
            return f"targets a layer this keyboard (layers 0-{kb.layers - 1}) does not have", "invalid_layer_reference"
    base = table["QK_LAYER_MOD"]
    if base <= raw < base + (0x200 if v6 else 0x100) and (raw >> table["QMK_LM_SHIFT"]) & 0xF >= kb.layers:
        return f"targets a layer this keyboard (layers 0-{kb.layers - 1}) does not have", "invalid_layer_reference"
    base = table["QK_LAYER_TAP"]
    if base <= raw < base + 0x1000 and (raw >> 8) & 0xF >= kb.layers:
        return f"targets a layer this keyboard (layers 0-{kb.layers - 1}) does not have", "invalid_layer_reference"
    base = table["QK_MACRO"]
    if base <= raw < base + 0x80:
        return f"targets a macro this keyboard ({kb.macro_count} macros) does not have", "invalid_keycode"
    return None


def parse_keycode(kb: Keyboard, text: str) -> tuple[KeycodeValue, list[str]]:
    """Validate requested keycode text for this keyboard. Returns the value and any warnings."""
    text = text.strip()
    if not text or len(text) > MAX_KEYCODE_TEXT or not _KEYCODE_TEXT.match(text):
        raise _invalid(text, "unexpected characters or length")
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise _invalid(text, "not a keycode expression") from exc

    # KEYCODES_MAP is rebuilt per keyboard; qmk_id_to_keycode keeps names from other keyboards.
    layer_refs = [] if text in keycode_tables.KEYCODES_MAP else _literal_layers(tree, text)
    try:
        raw = Keycode.deserialize(text, reraise=True)
    except Exception as exc:
        raise _invalid(text, str(exc) or type(exc).__name__) from exc
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 0xFFFF:
        raise _invalid(text, "does not resolve to a 16-bit keycode")

    keycode = Keycode.serialize(raw)
    warnings: list[str] = []
    numeric = keycode.startswith("0x")

    match = _CANONICAL_LAYER.match(keycode)
    if match:
        layer_refs.append(int(match.group(1) or match.group(2)))
    elif numeric:
        problem = _numeric_reference_problem(kb, raw)
        if problem:
            raise _invalid(text, problem[0], problem[1])
    for layer in layer_refs:
        if not 0 <= layer < kb.layers:
            raise _invalid(text, f"references layer {layer}, but this keyboard has layers 0-{kb.layers - 1}",
                           "invalid_layer_reference")

    match = _CANONICAL_TD.match(keycode)
    if match and int(match.group(1)) >= kb.tap_dance_count:
        raise _invalid(text, f"this keyboard exposes {kb.tap_dance_count} tap dance entries")
    match = _CANONICAL_MACRO.match(keycode)
    if match and int(match.group(1)) >= kb.macro_count:
        raise _invalid(text, f"this keyboard exposes {kb.macro_count} macros")

    outer = Keycode.find_outer_keycode(keycode)
    if outer is not None and outer.hidden:
        raise VialticError("unsupported_operation",
                           f"Keycode {keycode} needs firmware feature {outer.requires_feature}, not reported by this keyboard.")
    if numeric and isinstance(tree, ast.Constant):
        warnings.append(f"{keycode} has no name in the pinned Vial tables; it is written numerically and its effect "
                        "depends on the firmware.")
    if keycode == RESET_KEYCODE:
        warnings.append(f"{RESET_KEYCODE} puts the keyboard into bootloader mode when pressed. The firmware only "
                        "accepts it while physically unlocked.")
    return KeycodeValue(raw=raw, keycode=keycode, label=display_label(keycode)), warnings


def list_keycodes(query: str | None = None) -> list[KeycodeInfo]:
    needle = query.lower() if query else None
    out = []
    for kc in keycode_tables.KEYCODES:
        if kc.hidden or kc in keycode_tables.KEYCODES_HIDDEN:
            continue
        info = KeycodeInfo(keycode=kc.qmk_id, label=kc.label.replace("\n", " "), description=kc.tooltip,
                           masked=kc.masked, aliases=[a for a in kc.alias if a != kc.qmk_id])
        if needle is None or needle in " ".join([info.keycode, info.label, info.description or "", *info.aliases]).lower():
            out.append(info)
    return out


# Geometry.

def _layout_option_size(item) -> int:
    return 1 if isinstance(item, str) else (len(item) - 2).bit_length()


def layout_choices(labels, value: int) -> list[int]:
    """Decode the packed layout option value. Port of LayoutEditor.unpack/get_choice."""
    if not labels or value < 0:
        return []
    bits = bin(value)[2:].zfill(32)
    choices = []
    # VIA stores option choices backwards, parse the value in reverse.
    for item in reversed(labels):
        size = _layout_option_size(item)
        if size == 0:
            choices.append(0)
            continue
        choices.append(int(bits[-size:], 2))
        bits = bits[:-size]
    choices.reverse()
    return choices


def layout_option_count(item) -> int:
    """How many alternatives one option offers. A plain label is on or off."""
    return 2 if isinstance(item, str) else len(item) - 1


def set_layout_choice(labels, value: int, index: int, choice: int) -> int:
    """Replace one option in the packed value, leaving every other bit as it was."""
    shift = sum(_layout_option_size(item) for item in labels[index + 1:])
    mask = ((1 << _layout_option_size(labels[index])) - 1) << shift
    return (value & ~mask) | (choice << shift)


def _geometry(key, ident: str, **extra) -> KeyGeometry:
    return KeyGeometry(
        id=ident, x=key.x, y=key.y, width=key.width, height=key.height, x2=key.x2, y2=key.y2,
        width2=key.width2 or key.width, height2=key.height2 or key.height, rotation_x=key.rotation_x,
        rotation_y=key.rotation_y, rotation_angle=key.rotation_angle, layout_index=key.layout_index,
        layout_option=key.layout_option, decal=bool(key.decal), **extra)


def layout_of(kb: Keyboard) -> LayoutGeometry:
    keys = [_geometry(k, f"{k.row},{k.col}", row=k.row, col=k.col) for k in kb.keys]
    encoders = [_geometry(k, f"enc{k.encoder_idx}:{k.encoder_dir}", encoder_index=k.encoder_idx,
                          encoder_direction=k.encoder_dir) for k in kb.encoders]
    return LayoutGeometry(keys=keys, encoders=encoders, layout_options=kb.layout_options,
                          layout_choices=layout_choices(kb.layout_labels, kb.layout_options))


# Reads.

def lighting_kind(kb: Keyboard) -> str:
    if kb.lighting_vialrgb:
        return "vialrgb"
    if kb.lighting_qmk_rgblight and kb.lighting_qmk_backlight:
        return "qmk_backlight_rgblight"
    if kb.lighting_qmk_rgblight:
        return "qmk_rgblight"
    if kb.lighting_qmk_backlight:
        return "qmk_backlight"
    return "none"


def writable_operations(kb: Keyboard) -> list[str]:
    """Operations this keyboard can take; a family it lacks is not offered."""
    available = {"set_key": True, "set_encoder": bool(kb.encoder_count), "set_layout_option": bool(kb.layout_labels),
                 "set_tap_dance": bool(kb.tap_dance_count), "set_combo": bool(kb.combo_count),
                 "set_key_override": bool(kb.key_override_count), "set_alt_repeat_key": bool(kb.alt_repeat_key_count),
                 "set_qmk_setting": bool(set(kb.settings))}
    return [name for name, present in available.items() if present]


def capabilities_of(kb: Keyboard) -> Capabilities:
    return Capabilities(
        layers=kb.layers, rows=kb.rows, cols=kb.cols, key_count=len(kb.keys), encoder_count=kb.encoder_count,
        macro_count=kb.macro_count, macro_memory_bytes=kb.macro_memory, tap_dance_count=kb.tap_dance_count,
        combo_count=kb.combo_count, key_override_count=kb.key_override_count,
        alt_repeat_key_count=kb.alt_repeat_key_count,
        qmk_settings=sorted(q for q in kb.supported_settings if QmkSettings.is_qsid_supported(q)),
        unknown_qmk_settings=sorted(q for q in kb.supported_settings if not QmkSettings.is_qsid_supported(q)),
        lighting=lighting_kind(kb), layout_option_labels=kb.layout_labels or [],
        supported_features=sorted(kb.supported_features), custom_keycode_count=len(kb.custom_keycodes or []),
        midi=kb.midi if isinstance(kb.midi, str) else None, writable_operations=writable_operations(kb))


def read_configuration(kb: Keyboard) -> Configuration:
    """Convert a freshly reloaded Keyboard. Raw codes come back through upstream's codec."""
    load_raw_options(kb)
    keymap = [KeymapEntry(layer=l, row=r, col=c, **to_value(code).model_dump())
              for (l, r, c), code in sorted(kb.layout.items())]
    encoders = [EncoderEntry(layer=l, index=i, direction=d, **to_value(code).model_dump())
                for (l, i, d), code in sorted(kb.encoder_layout.items())]
    buffer = bytes(kb.macro)
    macros = MacroState(count=kb.macro_count, memory_bytes=kb.macro_memory, used_bytes=len(buffer),
                        buffer_b64=base64.b64encode(buffer).decode("ascii"), macros=kb.save_macro())

    lighting = LightingState(kind=lighting_kind(kb))
    updates: dict = {}
    if kb.lighting_qmk_rgblight:
        updates["rgblight"] = {"brightness": kb.underglow_brightness, "effect": kb.underglow_effect,
                               "effect_speed": kb.underglow_effect_speed, "hue": kb.underglow_color[0],
                               "saturation": kb.underglow_color[1]}
    if kb.lighting_qmk_backlight:
        updates["backlight"] = {"brightness": kb.backlight_brightness, "effect": kb.backlight_effect}
    if kb.lighting_vialrgb:
        updates["vialrgb"] = {"mode": kb.rgb_mode, "speed": kb.rgb_speed, "hue": kb.rgb_hsv[0],
                              "saturation": kb.rgb_hsv[1], "value": kb.rgb_hsv[2],
                              "maximum_brightness": kb.rgb_maximum_brightness,
                              "supported_effects": sorted(kb.rgb_supported_effects)}
    if updates:
        lighting = lighting.model_copy(update=updates)

    return Configuration(
        keymap=keymap, encoders=encoders, layout_options=kb.layout_options, macros=macros,
        tap_dance=[list(e) for e in kb.save_tap_dance()], combos=[list(e) for e in kb.save_combo()],
        key_overrides=kb.save_key_override(), alt_repeat_keys=kb.save_alt_repeat_key(),
        settings=_read_settings(kb),
        lighting=lighting)


def _read_settings(kb: Keyboard) -> SettingsState:
    unknown = sorted(q for q in kb.supported_settings if not QmkSettings.is_qsid_supported(q))
    raw = {}
    for qsid in unknown:
        data = kb.usb_send(kb.dev, struct.pack("<BBH", proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_QMK_SETTINGS_GET, qsid),
                           retries=20)
        if data[0] == 0:
            raw[str(qsid)] = bytes(data[1:]).hex()
    return SettingsState(values={str(k): v for k, v in sorted(kb.settings.items())}, unknown_qsids=unknown,
                         unreadable_qsids=sorted(q for q in kb.supported_settings
                                                 if QmkSettings.is_qsid_supported(q) and q not in kb.settings),
                         unknown_raw=raw)


def setting_fields(qsid: int) -> list[dict]:
    return QmkSettings.qsid_fields[qsid]


def setting_change_title(qsid: int, before: int, after: int) -> str:
    """Name what changes, using the pinned table's own titles."""
    fields = setting_fields(qsid)
    if fields[0]["type"] == "integer":
        return fields[0]["title"]
    changed = before ^ after
    names = [f["title"] for f in fields if changed & (1 << f.get("bit", 0))]
    return "; ".join(names) or fields[0]["title"]


def coverage_of(kb: Keyboard, config: Configuration) -> dict[str, str]:
    """What each family's state in a snapshot amounts to. Every read family is backed up."""
    def present(count: int) -> str:
        return "complete" if count else "absent_on_device"

    return {
        "keymap": "complete",
        "encoders": present(kb.encoder_count),
        "layout_options": present(1 if kb.layout_labels else 0),
        "macros": present(kb.macro_count),
        "tap_dance": present(kb.tap_dance_count),
        "combos": present(kb.combo_count),
        "key_overrides": present(kb.key_override_count),
        "alt_repeat_keys": present(kb.alt_repeat_key_count),
        "settings": ("partial_unknown_qsids" if config.settings.unknown_qsids
                     else "partial_unreadable_qsids" if config.settings.unreadable_qsids
                     else present(len(kb.supported_settings))),
        "lighting": present(0 if lighting_kind(kb) == "none" else 1),
    }


KEY_OVERRIDE_KNOWN_OPTIONS = 0b10111111
ALT_REPEAT_KNOWN_OPTIONS = 0b00001111


# Writes. Upstream's setters update their in-memory copy before sending and some ignore the
# outcome, so callers must prove every write by reloading from the keyboard.

def tap_dance_value(entry) -> TapDanceValue:
    return TapDanceValue(on_tap=to_value(entry[0]), on_hold=to_value(entry[1]), on_double_tap=to_value(entry[2]),
                         on_tap_hold=to_value(entry[3]), tapping_term=entry[4])


def tap_dance_wire(value: TapDanceValue) -> tuple:
    """The form upstream keeps entries in, which is also what snapshots hold."""
    return (value.on_tap.keycode, value.on_hold.keycode, value.on_double_tap.keycode, value.on_tap_hold.keycode,
            value.tapping_term)


def combo_value(entry) -> ComboValue:
    return ComboValue(keys=[to_value(code) for code in entry[:4]], output=to_value(entry[4]))


def combo_wire(value: ComboValue) -> tuple:
    return (*(key.keycode for key in value.keys), value.output.keycode)


class _RawOptions:
    """Stands in for upstream's option classes, which drop every bit they do not name."""

    def __init__(self, value: int):
        self.value = value

    def serialize(self) -> int:
        return self.value


def load_raw_options(kb: Keyboard) -> None:
    """Replace upstream's filtered option bytes with what the firmware holds."""
    for entries, cmd, count, fmt in (
            (kb.key_override_entries, proto.DYNAMIC_VIAL_KEY_OVERRIDE_GET, kb.key_override_count, "<HHHBBBB"),
            (kb.alt_repeat_key_entries, proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_GET, kb.alt_repeat_key_count, "<HHBB")):
        for entry, raw in zip(entries, kb._retrieve_dynamic_entries(cmd, count, fmt)):
            entry.options = _RawOptions(raw[-1])


def key_override_value(entry: dict) -> KeyOverrideValue:
    return KeyOverrideValue(**{**entry, "trigger": to_value(entry["trigger"]), "replacement": to_value(entry["replacement"])})


def key_override_wire(value: KeyOverrideValue) -> dict:
    """The form upstream saves entries in, which is also what snapshots hold."""
    return {**value.model_dump(), "trigger": value.trigger.keycode, "replacement": value.replacement.keycode}


def _key_override_entry(value: KeyOverrideValue) -> KeyOverrideEntry:
    entry = KeyOverrideEntry((value.trigger.keycode, value.replacement.keycode, value.layers, value.trigger_mods,
                              value.negative_mod_mask, value.suppressed_mods, 0))
    entry.options = _RawOptions(value.options)
    return entry


def alt_repeat_value(entry: dict) -> AltRepeatKeyValue:
    return AltRepeatKeyValue(**{**entry, "keycode": to_value(entry["keycode"]), "alt_keycode": to_value(entry["alt_keycode"])})


def alt_repeat_wire(value: AltRepeatKeyValue) -> dict:
    return {**value.model_dump(), "keycode": value.keycode.keycode, "alt_keycode": value.alt_keycode.keycode}


def _alt_repeat_entry(value: AltRepeatKeyValue) -> AltRepeatKeyEntry:
    entry = AltRepeatKeyEntry((value.keycode.keycode, value.alt_keycode.keycode, value.allowed_mods, 0))
    entry.options = _RawOptions(value.options)
    return entry


def write_operation(kb: Keyboard, op) -> None:
    if op.type == "set_key":
        kb.set_key(op.layer, op.row, op.col, op.after.keycode)
    elif op.type == "set_encoder":
        kb.set_encoder(op.layer, op.index, op.direction, op.after.keycode)
    elif op.type == "set_layout_options":
        kb.set_layout_options(op.after)
    elif op.type == "set_tap_dance":
        kb.tap_dance_set(op.index, tap_dance_wire(op.after))
    elif op.type == "set_combo":
        kb.combo_set(op.index, combo_wire(op.after))
    elif op.type == "set_key_override":
        kb.key_override_set(op.index, _key_override_entry(op.after))
    elif op.type == "set_alt_repeat_key":
        kb.alt_repeat_key_set(op.index, _alt_repeat_entry(op.after))
    elif op.type == "set_qmk_setting":
        status = kb.qmk_settings_set(op.qsid, op.after)
        if status != 0:
            raise RuntimeError(f"the firmware refused QMK setting {op.qsid} (status {status})")
    else:
        raise VialticError("unsupported_operation", f"Unknown operation type {op.type}.")


def is_vial_protocol_supported(via: int, vial: int) -> bool:
    return via in SUPPORTED_VIA_PROTOCOL and vial in SUPPORTED_VIAL_PROTOCOL
