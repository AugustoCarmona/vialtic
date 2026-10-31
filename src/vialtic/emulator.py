# SPDX-License-Identifier: GPL-2.0-or-later
"""Development emulator. Answers the Vial wire protocol from a small JSON state file.

It sits below upstream's Keyboard class rather than replacing it, unlike upstream's
DummyKeyboard, so packet codecs, the transport gate and recovery all run the same
code as on hardware. Default capacities follow DummyKeyboard. Explicit activation
only; emulator results say nothing about physical compatibility.
"""
from __future__ import annotations

import base64
import json
import lzma
import os
import pathlib
import struct
import time
from dataclasses import dataclass
from typing import Any

from . import vial_adapter as va
from .device import classify_packet
from .errors import VialticError
from .storage import atomic_write

proto = va.proto

RELEASE = 0x0100
UNHANDLED = 0xFF  # id_unhandled: what firmware answers to a command it does not implement


def _key(label: str, **props) -> list[Any]:
    return [props, label] if props else [label]


GENERIC_DEFINITION = {
    "name": "Vialtic Generic", "lighting": "vialrgb", "matrix": {"rows": 4, "cols": 4},
    "layouts": {
        "labels": ["Wide bottom key"],
        "keymap": [
            ["0,0", "0,1", "0,2", "0,3", {"x": 0.5}, "0,0\n\n\n\n\n\n\n\n\ne", "0,1\n\n\n\n\n\n\n\n\ne"],
            ["1,0", "1,1", "1,2", "1,3"],
            ["2,0", "2,1", "2,2", "2,3"],
            ["3,0\n\n\n0,0", "3,1\n\n\n0,0", {"w": 2}, "3,2\n\n\n0,0"],
            [{"y": 0.5}, {"w": 4}, "3,0\n\n\n0,1"],
        ],
    },
}

SPLIT_DEFINITION = {
    "name": "Vialtic Split", "lighting": "qmk_rgblight", "matrix": {"rows": 8, "cols": 5},
    "layouts": {
        "keymap": [
            [{"x": 2}, "0,2", {"x": 6}, "4,2"],
            [{"y": -0.75, "x": 1}, "0,1", {"x": 1}, "0,3", {"x": 4}, "4,3", {"x": 1}, "4,1"],
            [{"y": -0.875}, "0,0", {"x": 3}, "0,4", {"x": 2}, "4,4", {"x": 3}, "4,0"],
            [{"y": -0.375, "x": 2}, "1,2", {"x": 6}, "5,2"],
            [{"y": -0.75, "x": 1}, "1,1", {"x": 1}, "1,3", {"x": 4}, "5,3", {"x": 1}, "5,1"],
            [{"y": -0.875}, "1,0", {"x": 3}, "1,4", {"x": 2}, "5,4", {"x": 3}, "5,0"],
            [{"y": -0.375, "x": 2}, "2,2", {"x": 6}, "6,2"],
            [{"y": -0.75, "x": 1}, "2,1", {"x": 1}, "2,3", {"x": 4}, "6,3", {"x": 1}, "6,1"],
            [{"y": -0.875}, "2,0", {"x": 3}, "2,4", {"x": 2}, "6,4", {"x": 3}, "6,0"],
            [{"r": 15, "rx": 4.5, "ry": 3.3, "y": 0.2, "x": -0.5}, "3,0", "3,1"],
            [{"r": -15, "rx": 7, "ry": 3.3, "y": 0.2, "x": -1.5}, "7,1", "7,0"],
        ],
    },
}

LEGACY_DEFINITION = {
    "name": "Vialtic Legacy", "lighting": "qmk_backlight", "matrix": {"rows": 2, "cols": 3},
    "layouts": {"keymap": [["0,0", "0,1", "0,2"], ["1,0", "1,1", "1,2"]]},
}


FULL_DEFINITION = {
    "name": "Vialtic Full", "lighting": "vialrgb", "matrix": {"rows": 2, "cols": 3},
    "layouts": {
        "labels": ["Split backspace", ["Bottom row", "ANSI", "ISO", "Compact"]],
        "keymap": [
            ["0,0", "0,1\n\n\n0,0", {"x": 0.5}, "0,0\n\n\n\n\n\n\n\n\ne", "0,1\n\n\n\n\n\n\n\n\ne"],
            ["1,0\n\n\n1,0", "1,1\n\n\n1,1", "1,2\n\n\n1,2"],
        ],
    },
}

DUAL_DEFINITION = {
    "name": "Vialtic Dual", "lighting": "qmk_backlight_rgblight", "matrix": {"rows": 1, "cols": 2},
    "layouts": {"keymap": [["0,0", "0,1"]]},
}

MINIMAL_DEFINITION = {
    "name": "Vialtic Minimal", "matrix": {"rows": 1, "cols": 2},
    "layouts": {"keymap": [["0,0", "0,1"]]},
}


@dataclass(frozen=True)
class DeviceSpec:
    definition: dict[str, Any]
    name: str
    serial: str = va.VIAL_SERIAL_NUMBER_MAGIC
    uid: int = 0x1122334455667788
    vendor_id: int = 0xFEED
    product_id: int = 0x0001
    via_protocol: int = 9
    vial_protocol: int = 6
    layers: int = 4
    macro_count: int = 16
    macro_memory: int = 900
    tap_dance_count: int = 4
    combo_count: int = 4
    key_override_count: int = 2
    alt_repeat_key_count: int = 2
    settings: tuple[int, ...] = (1, 2, 7)
    unlock_keys: tuple[tuple[int, int], ...] = ((0, 0), (0, 1))


# Every qsid in the pinned table: widths of 1, 2 and 4 bytes, plain booleans and bit fields.
ALL_QSIDS = tuple(sorted(va.QmkSettings.qsid_fields))

SCENARIOS: dict[str, list[DeviceSpec]] = {
    "generic": [DeviceSpec(GENERIC_DEFINITION, "Generic", uid=0xA1)],
    "split": [DeviceSpec(SPLIT_DEFINITION, "Split", uid=0xB2, vendor_id=0xFEEE, product_id=0x0002)],
    "multi": [DeviceSpec(GENERIC_DEFINITION, "Generic", uid=0xA1),
              DeviceSpec(SPLIT_DEFINITION, "Split", uid=0xB2, vendor_id=0xFEEE, product_id=0x0002)],
    "twins": [DeviceSpec(SPLIT_DEFINITION, "Split", uid=0xB2, vendor_id=0xFEEE, product_id=0x0002),
              DeviceSpec(SPLIT_DEFINITION, "Split", uid=0xB2, vendor_id=0xFEEE, product_id=0x0002)],
    "serial": [DeviceSpec(GENERIC_DEFINITION, "Generic", uid=0xA1, serial=va.VIAL_SERIAL_NUMBER_MAGIC + ":unit-0042")],
    "legacy": [DeviceSpec(LEGACY_DEFINITION, "Legacy", uid=0xC3, vial_protocol=5, macro_memory=128, macro_count=2,
                          tap_dance_count=0, combo_count=0, key_override_count=0, alt_repeat_key_count=0)],
    "future": [DeviceSpec(GENERIC_DEFINITION, "Future", uid=0xD4, vial_protocol=99)],
    # Every configuration family at once, small enough for exhaustive fault sweeps.
    "full": [DeviceSpec(FULL_DEFINITION, "Full", uid=0xE5, layers=2, macro_count=4, macro_memory=64, tap_dance_count=2,
                        combo_count=2, key_override_count=2, alt_repeat_key_count=2, settings=ALL_QSIDS)],
    "dual": [DeviceSpec(DUAL_DEFINITION, "Dual", uid=0xE6, layers=2, settings=())],
    # Keys and nothing else: every other family must answer with a capability error.
    "minimal": [DeviceSpec(MINIMAL_DEFINITION, "Minimal", uid=0xE7, layers=2, macro_count=0, macro_memory=0, tap_dance_count=0,
                           combo_count=0, key_override_count=0, alt_repeat_key_count=0, settings=())],
    # Vial protocol 1: no delays or 16-bit codes in macros, no dynamic entries and no QMK settings.
    "vintage": [DeviceSpec(LEGACY_DEFINITION, "Vintage", uid=0xE8, vial_protocol=1, macro_count=2, macro_memory=32,
                           tap_dance_count=0, combo_count=0, key_override_count=0, alt_repeat_key_count=0, settings=())],
}


@dataclass
class Faults:
    """Injected failures, counted in mutating packets accepted by this process."""

    disconnect_after_writes: int | None = None
    reconnect_after_seconds: float | None = None
    timeout_after_writes: int | None = None
    drop_writes_after: int | None = None
    readonly_after_disconnect: bool = False
    disconnect_only_device: int | None = None
    fail_lock: bool = False
    unlock_never: bool = False
    settings_status_fail: tuple[int, ...] = ()  # qsids whose SET answers a nonzero status and stores nothing
    settings_get_fail: tuple[int, ...] = ()  # qsids whose GET answers a nonzero status

    @classmethod
    def from_env(cls) -> "Faults":
        raw = os.environ.get("VIALTIC_EMULATOR_FAULTS")
        if not raw:
            return cls()
        try:
            return cls(**json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise VialticError("invalid_request", f"Invalid VIALTIC_EMULATOR_FAULTS: {exc}") from exc


def _initial_keymap(spec: DeviceSpec) -> bytes:
    rows, cols = spec.definition["matrix"]["rows"], spec.definition["matrix"]["cols"]
    cells = bytearray()
    for layer in range(spec.layers):
        for row in range(rows):
            for col in range(cols):
                if layer == 0:
                    code = 0x04 + (row * cols + col) % 26  # KC_A onwards
                elif layer == 1:
                    code = 0x1E + (row * cols + col) % 10  # KC_1 onwards
                else:
                    code = 0x01  # KC_TRNS
                cells += struct.pack(">H", code)
    return bytes(cells)


INITIAL_LIGHTING = {"rgb_brightness": 128, "rgb_effect": 1, "rgb_speed": 5, "rgb_h": 32, "rgb_s": 64, "bl_brightness": 42,
                    "bl_effect": 0, "mode": 2, "speed": 90, "h": 16, "s": 32, "v": 64}
VIALRGB_EFFECTS = (1, 2, 3)
VIALRGB_MAX_BRIGHTNESS = 128


def _initial_state(spec: DeviceSpec) -> dict[str, Any]:
    labels = spec.definition["layouts"].get("labels") or []
    encoders = max([0] + [1 for row in spec.definition["layouts"]["keymap"] for k in row
                          if isinstance(k, str) and k.endswith("\ne")])
    qsids = {str(q): 0 for q in spec.settings}
    macro = b"\x00" * spec.macro_count
    return {
        "keymap": base64.b64encode(_initial_keymap(spec)).decode(),
        "encoders": {f"{l}:{i}:{d}": 0x01 for l in range(spec.layers) for i in range(encoders) for d in (0, 1)},
        "layout_options": 0 if labels else -1,
        "macro": base64.b64encode(macro.ljust(spec.macro_memory, b"\x00")).decode(),
        "tap_dance": [[0, 0, 0, 0, 200] for _ in range(spec.tap_dance_count)],
        "combos": [[0, 0, 0, 0, 0] for _ in range(spec.combo_count)],
        "key_overrides": [[0, 0, 0xFFFF, 0, 0, 0, 0] for _ in range(spec.key_override_count)],
        "alt_repeat": [[0, 0, 0, 0] for _ in range(spec.alt_repeat_key_count)],
        "settings": qsids,
        "lighting": dict(INITIAL_LIGHTING),
        "lighting_saved": dict(INITIAL_LIGHTING),
        "unlocked": False,
        "unlock_in_progress": False,
        "unlock_counter": 0,
    }


class Firmware:
    """Firmware side of the protocol for one emulated keyboard."""

    def __init__(self, spec: DeviceSpec, state: dict[str, Any]):
        self.spec = spec
        self.state = state
        self.rows = spec.definition["matrix"]["rows"]
        self.cols = spec.definition["matrix"]["cols"]
        self.keymap = bytearray(base64.b64decode(state["keymap"]))
        self.macro = bytearray(base64.b64decode(state["macro"]))
        self.definition = lzma.compress(json.dumps(spec.definition).encode())
        self.unlock_never = False
        self.settings_status_fail: tuple[int, ...] = ()
        self.settings_get_fail: tuple[int, ...] = ()
        state.setdefault("lighting_saved", dict(state["lighting"]))

    def power_cycle(self) -> None:
        """What a replug does: the firmware locks and lighting returns to its saved values."""
        self.state["unlocked"], self.state["unlock_in_progress"], self.state["unlock_counter"] = False, False, 0
        self.state["lighting"] = dict(self.state["lighting_saved"])
        self.sync()

    def sync(self) -> None:
        self.state["keymap"] = base64.b64encode(bytes(self.keymap)).decode()
        self.state["macro"] = base64.b64encode(bytes(self.macro)).decode()

    def key_offset(self, layer: int, row: int, col: int) -> int:
        return (layer * self.rows * self.cols + row * self.cols + col) * 2

    @property
    def boot_code(self) -> int:
        tables = va.keycode_tables
        return (tables.keycodes_v6 if self.spec.vial_protocol == 6 else tables.keycodes_v5).kc[va.RESET_KEYCODE]

    def firewall(self, code: int) -> int:
        """vial_keycode_firewall: while locked the firmware stores KC_NO instead of QK_BOOT, without saying so."""
        return 0 if code == self.boot_code and not self.state["unlocked"] else code

    def handle(self, msg: bytes) -> bytes:
        resp = bytearray(msg.ljust(va.MSG_LEN, b"\x00"))
        cmd = msg[0]
        if cmd == proto.CMD_VIA_GET_PROTOCOL_VERSION:
            resp[1:3] = struct.pack(">H", self.spec.via_protocol)
        elif cmd == proto.CMD_VIA_GET_KEYBOARD_VALUE:
            if msg[1] == proto.VIA_LAYOUT_OPTIONS:
                resp[2:6] = struct.pack(">I", max(self.state["layout_options"], 0))
            else:
                resp[0] = UNHANDLED
        elif cmd == proto.CMD_VIA_SET_KEYBOARD_VALUE:
            if msg[1] == proto.VIA_LAYOUT_OPTIONS:
                self.state["layout_options"] = struct.unpack(">I", msg[2:6])[0]
            else:
                resp[0] = UNHANDLED
        elif cmd == proto.CMD_VIA_GET_LAYER_COUNT:
            resp[1] = self.spec.layers
        elif cmd == proto.CMD_VIA_KEYMAP_GET_BUFFER:
            offset, size = struct.unpack(">HB", msg[1:4])
            resp[4:4 + size] = self.keymap[offset:offset + size]
        elif cmd == proto.CMD_VIA_SET_KEYCODE:
            layer, row, col, code = struct.unpack(">BBBH", msg[1:6])
            if code != self.boot_code or self.state["unlocked"]:
                self.keymap[self.key_offset(layer, row, col):self.key_offset(layer, row, col) + 2] = struct.pack(">H", code)
        elif cmd == proto.CMD_VIA_MACRO_GET_COUNT:
            resp[1] = self.spec.macro_count
        elif cmd == proto.CMD_VIA_MACRO_GET_BUFFER_SIZE:
            resp[1:3] = struct.pack(">H", self.spec.macro_memory)
        elif cmd == proto.CMD_VIA_MACRO_GET_BUFFER:
            offset, size = struct.unpack(">HB", msg[1:4])
            resp[4:4 + size] = self.macro[offset:offset + size]
        elif cmd == proto.CMD_VIA_MACRO_SET_BUFFER:
            offset, size = struct.unpack(">HB", msg[1:4])
            # Locked firmware ignores macro writes without reporting it, and out of range ones are dropped.
            if self.state["unlocked"] and offset + size <= self.spec.macro_memory:
                self.macro[offset:offset + size] = msg[4:4 + size]
        elif cmd == proto.CMD_VIA_LIGHTING_GET_VALUE:
            self._lighting_get(msg, resp)
        elif cmd == proto.CMD_VIA_LIGHTING_SET_VALUE:
            self._lighting_set(msg)
        elif cmd == proto.CMD_VIA_LIGHTING_SAVE:
            self.state["lighting_saved"] = dict(self.state["lighting"])
        elif cmd == proto.CMD_VIA_VIAL_PREFIX:
            self._vial(msg, resp)
        return bytes(resp)

    def _lighting_get(self, msg: bytes, resp: bytearray) -> None:
        light = self.state["lighting"]
        sub = msg[1]
        if sub == proto.QMK_RGBLIGHT_BRIGHTNESS:
            resp[2] = light["rgb_brightness"]
        elif sub == proto.QMK_RGBLIGHT_EFFECT:
            resp[2] = light["rgb_effect"]
        elif sub == proto.QMK_RGBLIGHT_EFFECT_SPEED:
            resp[2] = light["rgb_speed"]
        elif sub == proto.QMK_RGBLIGHT_COLOR:
            resp[2], resp[3] = light["rgb_h"], light["rgb_s"]
        elif sub == proto.QMK_BACKLIGHT_BRIGHTNESS:
            resp[2] = light["bl_brightness"]
        elif sub == proto.QMK_BACKLIGHT_EFFECT:
            resp[2] = light["bl_effect"]
        elif sub == proto.VIALRGB_GET_INFO:
            resp[2:5] = struct.pack("<HB", 1, VIALRGB_MAX_BRIGHTNESS)
        elif sub == proto.VIALRGB_GET_MODE:
            resp[2:8] = struct.pack("<HBBBB", light["mode"], light["speed"], light["h"], light["s"], light["v"])
        elif sub == proto.VIALRGB_GET_SUPPORTED:
            cur = struct.unpack("<H", msg[2:4])[0]
            effects = [e for e in VIALRGB_EFFECTS if e > cur]
            payload = effects + [0xFFFF] * (15 - len(effects))
            resp[2:32] = struct.pack("<15H", *payload)

    def _lighting_set(self, msg: bytes) -> None:
        light = self.state["lighting"]
        sub = msg[1]
        names = {proto.QMK_RGBLIGHT_BRIGHTNESS: "rgb_brightness", proto.QMK_RGBLIGHT_EFFECT: "rgb_effect",
                 proto.QMK_RGBLIGHT_EFFECT_SPEED: "rgb_speed", proto.QMK_BACKLIGHT_BRIGHTNESS: "bl_brightness",
                 proto.QMK_BACKLIGHT_EFFECT: "bl_effect"}
        if sub in names:
            light[names[sub]] = msg[2]
        elif sub == proto.QMK_RGBLIGHT_COLOR:
            light["rgb_h"], light["rgb_s"] = msg[2], msg[3]
        elif sub == proto.VIALRGB_SET_MODE:
            mode, speed, hue, saturation, value = struct.unpack("<HBBBB", msg[2:8])
            # Like the firmware: unknown effects are ignored and brightness is clamped.
            if mode == 0 or mode in VIALRGB_EFFECTS:
                light["mode"] = mode
            light["speed"], light["h"], light["s"], light["v"] = speed, hue, saturation, min(value, VIALRGB_MAX_BRIGHTNESS)

    def _vial(self, msg: bytes, resp: bytearray) -> None:
        sub = msg[1]
        if sub == proto.CMD_VIAL_GET_KEYBOARD_ID:
            resp[0:12] = struct.pack("<IQ", self.spec.vial_protocol, self.spec.uid)
        elif sub == proto.CMD_VIAL_GET_SIZE:
            resp[0:4] = struct.pack("<I", len(self.definition))
        elif sub == proto.CMD_VIAL_GET_DEFINITION:
            block = struct.unpack("<I", msg[2:6])[0]
            resp[0:32] = self.definition[block * 32:(block + 1) * 32].ljust(32, b"\x00")
        elif sub == proto.CMD_VIAL_GET_ENCODER:
            codes = self.state["encoders"]
            resp[0:4] = struct.pack(">HH", codes[f"{msg[2]}:{msg[3]}:0"], codes[f"{msg[2]}:{msg[3]}:1"])
        elif sub == proto.CMD_VIAL_SET_ENCODER:
            self.state["encoders"][f"{msg[2]}:{msg[3]}:{msg[4]}"] = self.firewall(struct.unpack(">H", msg[5:7])[0])
        elif sub == proto.CMD_VIAL_GET_UNLOCK_STATUS:
            resp[0:32] = b"\xff" * 32
            resp[0], resp[1] = int(self.state["unlocked"]), int(self.state["unlock_in_progress"])
            for i, (row, col) in enumerate(self.spec.unlock_keys):
                resp[2 + i * 2], resp[3 + i * 2] = row, col
        elif sub == proto.CMD_VIAL_UNLOCK_START:
            self.state["unlock_in_progress"], self.state["unlock_counter"] = True, 3
        elif sub == proto.CMD_VIAL_UNLOCK_POLL:
            if self.state["unlock_in_progress"] and not self.unlock_never:
                self.state["unlock_counter"] -= 1
                if self.state["unlock_counter"] <= 0:
                    self.state["unlocked"], self.state["unlock_in_progress"] = True, False
            resp[0], resp[1] = int(self.state["unlocked"]), int(self.state["unlock_in_progress"])
            resp[2] = max(self.state["unlock_counter"], 0)
        elif sub == proto.CMD_VIAL_LOCK:
            self.state["unlocked"], self.state["unlock_in_progress"] = False, False
        elif sub == proto.CMD_VIAL_QMK_SETTINGS_QUERY:
            cur = struct.unpack("<H", msg[2:4])[0]
            ids = [int(q) for q in sorted(self.state["settings"], key=int) if int(q) > cur][:16]
            resp[0:32] = struct.pack("<16H", *(ids + [0xFFFF] * (16 - len(ids))))
        elif sub == proto.CMD_VIAL_QMK_SETTINGS_GET:
            qsid = struct.unpack("<H", msg[2:4])[0]
            value = self.state["settings"].get(str(qsid))
            resp[0:32] = bytes(32)
            resp[0] = 0 if value is not None and qsid not in self.settings_get_fail else 1
            resp[1:5] = struct.pack("<I", value or 0)
        elif sub == proto.CMD_VIAL_QMK_SETTINGS_SET:
            qsid = struct.unpack("<H", msg[2:4])[0]
            width = self._setting_width(qsid)
            resp[0:32] = bytes(32)
            if width is None or str(qsid) not in self.state["settings"] or qsid in self.settings_status_fail:
                resp[0] = 1
            else:
                self.state["settings"][str(qsid)] = int.from_bytes(msg[4:4 + width], "little")
        elif sub == proto.CMD_VIAL_DYNAMIC_ENTRY_OP:
            self._dynamic(msg, resp)

    @staticmethod
    def _setting_width(qsid: int) -> int | None:
        """Firmware stores each setting in a fixed width, which the pinned table mirrors."""
        fields = va.QmkSettings.qsid_fields.get(qsid) if va.QmkSettings.is_qsid_supported(qsid) else None
        return fields[0].get("width", 1) if fields else None

    def _dynamic(self, msg: bytes, resp: bytearray) -> None:
        op = msg[2]
        idx = msg[3]
        if op == proto.DYNAMIC_VIAL_GET_NUMBER_OF_ENTRIES:
            resp[:] = bytes(va.MSG_LEN)
            resp[0:4] = bytes([self.spec.tap_dance_count, self.spec.combo_count, self.spec.key_override_count,
                               self.spec.alt_repeat_key_count])
            resp[-1] = 0b11
            return
        tables = {
            proto.DYNAMIC_VIAL_TAP_DANCE_GET: ("tap_dance", "<HHHHH"), proto.DYNAMIC_VIAL_TAP_DANCE_SET: ("tap_dance", "<HHHHH"),
            proto.DYNAMIC_VIAL_COMBO_GET: ("combos", "<HHHHH"), proto.DYNAMIC_VIAL_COMBO_SET: ("combos", "<HHHHH"),
            proto.DYNAMIC_VIAL_KEY_OVERRIDE_GET: ("key_overrides", "<HHHBBBB"),
            proto.DYNAMIC_VIAL_KEY_OVERRIDE_SET: ("key_overrides", "<HHHBBBB"),
            proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_GET: ("alt_repeat", "<HHBB"), proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_SET: ("alt_repeat", "<HHBB"),
        }
        firewalled = {proto.DYNAMIC_VIAL_TAP_DANCE_SET: (0, 1, 2, 3), proto.DYNAMIC_VIAL_COMBO_SET: (4,),
                      proto.DYNAMIC_VIAL_KEY_OVERRIDE_SET: (1,), proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_SET: (0, 1)}
        table, fmt = tables[op]
        if idx >= len(self.state[table]):
            resp[0] = 1
        elif op in firewalled:
            entry = list(struct.unpack(fmt, msg[4:4 + struct.calcsize(fmt)]))
            for position in firewalled[op]:
                entry[position] = self.firewall(entry[position])
            self.state[table][idx] = entry
            resp[0] = 0
        else:
            resp[0] = 0
            resp[1:1 + struct.calcsize(fmt)] = struct.pack(fmt, *self.state[table][idx])


class EmulatedHandle:
    """The hidapi device surface: write a report, read the answer."""

    def __init__(self, backend: "EmulatorBackend", index: int):
        self.backend = backend
        self.index = index
        self.firmware = backend.firmware(index)
        self.response = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        backend = self.backend
        if self.closed or backend.is_disconnected(self.index):
            raise OSError("device disconnected")
        msg = bytes(data[1:])  # drop report id
        if backend.packet_hook is not None:
            backend.packet_hook(msg)
        if backend.faults.fail_lock and msg[:2] == bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_LOCK]):
            raise OSError("lock refused")
        mutating = classify_packet(msg) == "write"
        if mutating:
            backend.writes_seen += 1
            if backend.reached(backend.faults.disconnect_after_writes):
                backend.disconnect()
                raise OSError("device disconnected")
            if backend.reached(backend.faults.timeout_after_writes):
                self.response = b""
                return len(data)
            if backend.reached(backend.faults.drop_writes_after) or (
                    backend.reconnected and backend.faults.readonly_after_disconnect):
                self.response = msg.ljust(va.MSG_LEN, b"\x00")
                return len(data)
        self.response = self.firmware.handle(msg)
        if mutating or classify_packet(msg) == "security":
            self.firmware.sync()
            backend.save()
        return len(data)

    def read(self, length: int, timeout_ms: int = 0) -> bytes:
        if self.closed or self.backend.is_disconnected(self.index):
            raise OSError("device disconnected")
        data, self.response = self.response, b""
        return data[:length]

    def close(self) -> None:
        self.closed = True


class EmulatorBackend:
    """Explicitly activated stand-in for HidBackend. State lives under its own directory."""

    simulated = True

    def __init__(self, scenario: str, directory: pathlib.Path, faults: Faults | None = None):
        if scenario not in SCENARIOS:
            raise VialticError("invalid_request", f"Unknown emulator scenario {scenario!r}.",
                               details={"scenarios": sorted(SCENARIOS)})
        self.scenario = scenario
        self.specs = SCENARIOS[scenario]
        self.path = directory / "emulator.json"
        self.faults = faults or Faults()
        self.writes_seen = 0
        self.disconnected_at: float | None = None
        self.reconnected = False
        self.packet_hook: Any = None  # test seam: called with every outgoing packet, may raise
        self.states = self._load()
        self._firmware: dict[int, Firmware] = {}

    def _load(self) -> list[dict[str, Any]]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        states = [_initial_state(spec) for spec in self.specs]
        atomic_write(self.path, json.dumps(states).encode())
        return states

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.states).encode())

    def firmware(self, index: int) -> Firmware:
        if index not in self._firmware:
            firmware = Firmware(self.specs[index], self.states[index])
            firmware.unlock_never = self.faults.unlock_never
            firmware.settings_status_fail = tuple(self.faults.settings_status_fail)
            firmware.settings_get_fail = tuple(self.faults.settings_get_fail)
            self._firmware[index] = firmware
        return self._firmware[index]

    def reached(self, limit: int | None) -> bool:
        return limit is not None and self.writes_seen > limit

    def disconnect(self) -> None:
        self.disconnected_at = time.monotonic()
        self.faults.disconnect_after_writes = None
        only = self.faults.disconnect_only_device
        for index in range(len(self.specs)):
            if only is None or index == only:
                self.firmware(index).power_cycle()
        self.save()

    def is_disconnected(self, index: int | None = None) -> bool:
        if self.disconnected_at is None:
            return False
        delay = self.faults.reconnect_after_seconds
        if delay is not None and time.monotonic() - self.disconnected_at >= delay:
            self.disconnected_at = None
            self.reconnected = True
            return False
        only = self.faults.disconnect_only_device
        return only is None or index is None or index == only

    def enumerate(self) -> list[dict[str, Any]]:
        return [{
            "path": f"emulator://{self.scenario}/{index}".encode(), "vendor_id": spec.vendor_id, "product_id": spec.product_id,
            "serial_number": spec.serial, "manufacturer_string": "Vialtic Emulator", "product_string": spec.name,
            "usage_page": 0xFF60, "usage": 0x61, "interface_number": 1, "release_number": RELEASE, "bus_type": 1,
        } for index, spec in enumerate(self.specs) if not self.is_disconnected(index)]

    def open(self, path: Any) -> EmulatedHandle:
        index = int(path.decode().rsplit("/", 1)[1])
        if self.is_disconnected(index):
            raise OSError("device disconnected")
        return EmulatedHandle(self, index)
