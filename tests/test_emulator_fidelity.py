# SPDX-License-Identifier: GPL-2.0-or-later
"""The emulator must be as strict as firmware; a permissive one produces false greens."""
import struct

import pytest

from conftest import Rig
from vialtic import vial_adapter as va
from vialtic.device import GuardedTransport, classify_packet
from vialtic.emulator import ALL_QSIDS, Faults, SCENARIOS
from vialtic.errors import VialticError

proto = va.proto


def firmware(tmp_path, scenario="full", faults=None):
    rig = Rig(tmp_path, scenario, faults)
    return rig, rig.backend.firmware(0)


def vial(*parts):
    return bytes([proto.CMD_VIA_VIAL_PREFIX, *parts])


def test_settings_set_reads_the_width_in_the_table(tmp_path):
    _, fw = firmware(tmp_path)
    one_byte = next(q for q in ALL_QSIDS if va.QmkSettings.qsid_fields[q][0].get("width", 1) == 1)
    four_bytes = next(q for q in ALL_QSIDS if va.QmkSettings.qsid_fields[q][0].get("width", 1) == 4)
    assert fw.handle(vial(proto.CMD_VIAL_QMK_SETTINGS_SET) + struct.pack("<H", one_byte) + b"\x07\xAA\xAA\xAA")[0] == 0
    assert fw.state["settings"][str(one_byte)] == 7, "bytes past the width must be ignored"
    assert fw.handle(vial(proto.CMD_VIAL_QMK_SETTINGS_SET) + struct.pack("<H", four_bytes) + struct.pack("<I", 0x030201))[0] == 0
    assert fw.state["settings"][str(four_bytes)] == 0x030201


def test_settings_set_answers_a_status_byte(tmp_path):
    _, fw = firmware(tmp_path)
    assert fw.handle(vial(proto.CMD_VIAL_QMK_SETTINGS_SET) + struct.pack("<H", 60000) + b"\x01")[0] != 0
    assert "60000" not in fw.state["settings"]
    fw.settings_status_fail = (7,)
    assert fw.handle(vial(proto.CMD_VIAL_QMK_SETTINGS_SET) + struct.pack("<H", 7) + b"\x01\x00")[0] != 0
    assert fw.state["settings"]["7"] == 0


def test_settings_query_pages_more_than_one_packet(tmp_path):
    rig, _ = firmware(tmp_path)
    assert len(ALL_QSIDS) > 16
    assert rig.service.read_snapshot().capabilities.qmk_settings == list(ALL_QSIDS)


def test_locked_firmware_firewalls_boot_in_dynamic_entries(tmp_path):
    _, fw = firmware(tmp_path)
    boot = fw.boot_code
    for op, table, payload, position in (
            (proto.DYNAMIC_VIAL_TAP_DANCE_SET, "tap_dance", struct.pack("<HHHHH", boot, boot, boot, boot, 9), 0),
            (proto.DYNAMIC_VIAL_COMBO_SET, "combos", struct.pack("<HHHHH", 4, 5, 0, 0, boot), 4),
            (proto.DYNAMIC_VIAL_KEY_OVERRIDE_SET, "key_overrides", struct.pack("<HHHBBBB", 4, boot, 1, 0, 0, 0, 0x80), 1),
            (proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_SET, "alt_repeat", struct.pack("<HHBB", boot, boot, 0, 8), 1)):
        fw.handle(vial(proto.CMD_VIAL_DYNAMIC_ENTRY_OP, op, 0) + payload)
        assert fw.state[table][0][position] == 0, table
        fw.state["unlocked"] = True
        fw.handle(vial(proto.CMD_VIAL_DYNAMIC_ENTRY_OP, op, 0) + payload)
        assert fw.state[table][0][position] == boot, table
        fw.state["unlocked"] = False


def test_dynamic_entry_out_of_range_answers_an_error(tmp_path):
    _, fw = firmware(tmp_path)
    assert fw.handle(vial(proto.CMD_VIAL_DYNAMIC_ENTRY_OP, proto.DYNAMIC_VIAL_COMBO_GET, 200))[0] != 0
    assert fw.handle(vial(proto.CMD_VIAL_DYNAMIC_ENTRY_OP, proto.DYNAMIC_VIAL_COMBO_SET, 200) + bytes(10))[0] != 0


def test_macro_writes_are_ignored_when_locked_and_out_of_range(tmp_path):
    _, fw = firmware(tmp_path)
    before = bytes(fw.macro)
    fw.handle(struct.pack(">BHB", proto.CMD_VIA_MACRO_SET_BUFFER, 0, 2) + b"zz")
    assert bytes(fw.macro) == before
    fw.state["unlocked"] = True
    fw.handle(struct.pack(">BHB", proto.CMD_VIA_MACRO_SET_BUFFER, len(before) - 1, 2) + b"zz")
    assert bytes(fw.macro) == before and len(fw.macro) == len(before)
    fw.handle(struct.pack(">BHB", proto.CMD_VIA_MACRO_SET_BUFFER, 0, 2) + b"zz")
    assert bytes(fw.macro[:2]) == b"zz"


def test_lighting_values_are_transient_until_saved(tmp_path):
    _, fw = firmware(tmp_path, "dual")
    fw.handle(bytes([proto.CMD_VIA_LIGHTING_SET_VALUE, proto.QMK_RGBLIGHT_BRIGHTNESS, 9]))
    assert fw.state["lighting"]["rgb_brightness"] == 9
    fw.power_cycle()
    assert fw.state["lighting"]["rgb_brightness"] == 128
    fw.handle(bytes([proto.CMD_VIA_LIGHTING_SET_VALUE, proto.QMK_RGBLIGHT_BRIGHTNESS, 9]))
    fw.handle(bytes([proto.CMD_VIA_LIGHTING_SAVE]))
    fw.power_cycle()
    assert fw.state["lighting"]["rgb_brightness"] == 9


def test_vialrgb_ignores_unknown_effects_and_clamps_brightness(tmp_path):
    _, fw = firmware(tmp_path)
    mode = fw.state["lighting"]["mode"]
    fw.handle(struct.pack("<BBHBBBB", proto.CMD_VIA_LIGHTING_SET_VALUE, proto.VIALRGB_SET_MODE, 999, 1, 2, 3, 255))
    assert fw.state["lighting"]["mode"] == mode and fw.state["lighting"]["v"] == 128


def test_replugging_locks_the_keyboard(tmp_path):
    rig, fw = firmware(tmp_path, "full", Faults(disconnect_after_writes=0))
    fw.state["unlocked"] = True
    rig.backend.disconnect()
    assert fw.state["unlocked"] is False


def test_unknown_keyboard_value_is_unhandled_not_a_layout_change(tmp_path):
    _, fw = firmware(tmp_path)
    before = fw.state["layout_options"]
    answer = fw.handle(bytes([proto.CMD_VIA_SET_KEYBOARD_VALUE, proto.VIA_SWITCH_MATRIX_STATE, 0, 0, 0, 7]))
    assert answer[0] != proto.CMD_VIA_SET_KEYBOARD_VALUE and fw.state["layout_options"] == before


@pytest.mark.parametrize("packet", [
    bytes([proto.CMD_VIA_SET_KEYBOARD_VALUE, proto.VIA_SWITCH_MATRIX_STATE]),
    bytes([proto.CMD_VIA_SET_KEYBOARD_VALUE, 0x01]),
    bytes([proto.CMD_VIA_LIGHTING_SET_VALUE, 0x7F]),
    bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_QMK_SETTINGS_RESET]),
    bytes([0x0B]),
    bytes([proto.CMD_VIA_GET_KEYBOARD_VALUE, proto.VIA_SWITCH_MATRIX_STATE]),
])
def test_gate_denies_non_configuration_packets_even_with_writes_open(packet):
    sent = []
    transport = GuardedTransport(send=lambda dev, msg, retries=1: sent.append(msg) or b"\x00" * 32)
    assert classify_packet(packet) == "deny"
    with transport.writes(), transport.security(), pytest.raises(VialticError) as error:
        transport(None, packet)
    assert error.value.code == "write_blocked" and sent == []


def test_gate_allows_layout_options_as_a_write_only():
    assert classify_packet(bytes([proto.CMD_VIA_SET_KEYBOARD_VALUE, proto.VIA_LAYOUT_OPTIONS])) == "write"
    assert classify_packet(bytes([proto.CMD_VIA_LIGHTING_SET_VALUE, proto.VIALRGB_SET_MODE])) == "write"
    assert classify_packet(bytes([proto.CMD_VIA_LIGHTING_SAVE])) == "write"


def test_every_write_packet_gets_the_same_retries():
    seen = []
    transport = GuardedTransport(send=lambda dev, msg, retries=1: seen.append(retries) or b"\x00" * 32)
    with transport.writes():
        transport(None, bytes([proto.CMD_VIA_LIGHTING_SAVE]), retries=1)
    transport(None, bytes([proto.CMD_VIA_GET_LAYER_COUNT]), retries=1)
    assert seen == [20, 1]


@pytest.mark.parametrize("scenario", sorted(set(SCENARIOS) - {"future"}))
def test_all_scenarios_read_cleanly(tmp_path, scenario):
    rig = Rig(tmp_path, scenario)
    for entry in rig.service.list_devices().devices:
        rig.service.read_snapshot(entry.identity.device_id)
    assert rig.write_packets() == 0
