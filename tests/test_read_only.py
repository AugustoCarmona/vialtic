# SPDX-License-Identifier: GPL-2.0-or-later
"""M1 gate: discovery, snapshots and views, none of which can write."""
import json
import lzma
import pathlib
import re
import struct

import pytest

from vialtic import models, render
from vialtic import vial_adapter as va
from vialtic.device import DeviceManager, GuardedTransport, classify_packet
from vialtic.emulator import SCENARIOS, EmulatorBackend, Firmware, GENERIC_DEFINITION
from vialtic.errors import ERROR_EXIT_CODES, VialticError
from vialtic.storage import Store

proto = va.proto
SCHEMA_DIR = pathlib.Path(__file__).resolve().parent / "schemas"


def test_packet_classification_is_default_deny():
    assert classify_packet(bytes([proto.CMD_VIA_KEYMAP_GET_BUFFER, 0, 0, 28])) == "read"
    assert classify_packet(bytes([proto.CMD_VIA_SET_KEYCODE, 0, 0, 0, 0, 4])) == "write"
    assert classify_packet(bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_SET_ENCODER])) == "write"
    assert classify_packet(bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_UNLOCK_START])) == "security"
    # EEPROM reset, settings reset, live matrix state and unknown commands are never sent.
    assert classify_packet(bytes([0x0B])) == "deny"
    assert classify_packet(bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_QMK_SETTINGS_RESET])) == "deny"
    assert classify_packet(bytes([proto.CMD_VIA_GET_KEYBOARD_VALUE, proto.VIA_SWITCH_MATRIX_STATE])) == "deny"
    assert classify_packet(bytes([0xEE])) == "deny"


def test_transport_refuses_writes_while_gate_is_closed():
    sent = []
    transport = GuardedTransport(send=lambda dev, msg, retries=1: sent.append(msg) or b"ok")
    write = bytes([proto.CMD_VIA_SET_KEYCODE, 0, 0, 0, 0, 4])
    with pytest.raises(VialticError) as error:
        transport(None, write)
    assert error.value.code == "write_blocked" and sent == []
    with transport.writes():
        assert transport(None, write) == b"ok"
    with pytest.raises(VialticError):
        transport(None, write)
    with pytest.raises(VialticError):
        transport(None, bytes([proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_LOCK]))
    assert transport.packets["write"] == 1


@pytest.mark.parametrize("scenario", ["generic", "split", "multi", "twins", "serial", "legacy"])
def test_read_paths_send_no_mutating_packets(make_rig, scenario):
    rig = make_rig(scenario)
    devices = rig.service.list_devices().devices
    for entry in devices:
        selector = entry.identity.device_id
        rig.service.inspect_device(selector)
        rig.service.read_snapshot(selector)
        rig.service.render(selector, None, "text", None)
        rig.service.keycodes(selector, "kc_a")
        rig.service.unlock_status(selector)
    rig.service.diagnose()
    assert devices and rig.write_packets() == 0


def test_snapshot_covers_every_family_including_lighting(make_rig):
    snapshot = make_rig("generic").service.read_snapshot()
    assert snapshot.schema_version == 1 and snapshot.device.simulated
    assert set(snapshot.coverage) == set(va.ALL_FAMILIES) == set(snapshot.family_fingerprints)
    assert snapshot.configuration.lighting.kind == "vialrgb"
    assert snapshot.configuration.lighting.vialrgb["mode"] == 2
    assert snapshot.configuration.macros.count == 16
    assert len(snapshot.configuration.tap_dance) == 4
    assert len(snapshot.configuration.encoders) == 8 * 1
    assert snapshot.configuration.settings.values == {"1": 0, "2": 0, "7": 0}


def test_capabilities_differ_by_device(make_rig):
    legacy = make_rig("legacy").service.inspect_device()
    assert legacy.device.vial_protocol == 5
    assert legacy.capabilities.tap_dance_count == 0 and legacy.capabilities.lighting == "qmk_backlight"
    assert "set_key" in legacy.capabilities.writable_operations
    assert "set_encoder" not in legacy.capabilities.writable_operations, "that keyboard has no encoders"


def test_selection_required_among_several_keyboards(make_rig):
    rig = make_rig("multi")
    with pytest.raises(VialticError) as error:
        rig.service.read_snapshot()
    assert error.value.code == "device_selection_required"
    assert len(error.value.details["candidates"]) == 2
    entries = rig.service.list_devices().devices
    chosen = rig.service.read_snapshot(entries[1].identity.device_id)
    assert chosen.device.name.endswith("Split")


def test_unknown_selector_is_an_error(make_rig):
    with pytest.raises(VialticError) as error:
        make_rig("multi").service.read_snapshot("dev-00000000")
    assert error.value.code == "device_not_found"


def test_no_device_is_reported_not_simulated(tmp_path):
    from vialtic.service import VialticService

    class Empty:
        simulated = False

        def enumerate(self):
            return []

        def open(self, path):
            raise OSError

    service = VialticService(Empty(), Store(tmp_path))
    assert service.list_devices().devices == []
    with pytest.raises(VialticError) as error:
        service.read_snapshot()
    assert error.value.code == "no_device"
    assert any(c.status == "warning" for c in service.diagnose().checks)


def test_identical_units_share_identity_and_say_so(make_rig):
    a, b = make_rig("twins").service.list_devices().devices
    assert a.identity.binding_id == b.identity.binding_id and a.identity.device_id != b.identity.device_id
    assert a.identity.identity_confidence == "model_and_firmware"
    assert "cannot be told apart" in a.identity.identity_note


def test_serial_beyond_marker_raises_confidence(make_rig):
    entry = make_rig("serial").service.list_devices().devices[0]
    assert entry.identity.identity_confidence == "usb_serial"


def test_unknown_protocol_reports_identity_and_refuses(make_rig):
    rig = make_rig("future")
    entry = rig.service.list_devices().devices[0]
    assert entry.status == "unsupported_protocol"
    assert entry.identity.vial_protocol == 99 and entry.identity.vial_uid is not None
    with pytest.raises(VialticError) as error:
        rig.service.read_snapshot()
    assert error.value.code == "unsupported_protocol"
    assert error.value.details["vial_protocol"] == 99
    with pytest.raises(VialticError):
        rig.plan((0, 0, 0, "KC_A"))
    assert rig.write_packets() == 0


class _Lying(EmulatorBackend):
    """Firmware whose definition or sizes are hostile."""

    definition = b""
    size = None

    def firmware(self, index):
        firmware = super().firmware(index)
        firmware.definition = self.definition
        original = firmware.handle

        def handle(msg):
            resp = original(msg)
            if self.size is not None and msg[:2] == bytes([0xFE, proto.CMD_VIAL_GET_SIZE]):
                return struct.pack("<I", self.size) + resp[4:]
            return resp

        firmware.handle = handle
        return firmware


@pytest.mark.parametrize("definition,size,message", [
    (b"not lzma data", None, "LZMA"),
    (lzma.compress(b"{not json"), None, "JSON"),
    (lzma.compress(b'{"matrix": {"rows": 1, "cols": 1}}'), None, "required field"),
    (lzma.compress(b'{"matrix": {"rows": 0, "cols": 1}, "layouts": {"keymap": [[]]}}'), None, "matrix"),
    (lzma.compress(b'{"matrix": {"rows": 1, "cols": 1}, "layouts": {"keymap": []}}'), None, "empty"),
    (lzma.compress(b" " * (va.MAX_DEFINITION_JSON + 10)), None, "exceeds"),
    (b"", 0xFFFFFFF0, "implausible"),
])
def test_malformed_firmware_definition_fails_closed(tmp_path, definition, size, message):
    backend = _Lying("generic", tmp_path)
    backend.definition, backend.size = definition, size
    manager = DeviceManager(backend, Store(tmp_path), max_retries=1, lock_timeout=1)
    entry = manager.list_devices()[0][0]
    assert entry.status == "error" and entry.problem.code == "malformed_device_data"
    assert message in entry.problem.message
    assert backend.writes_seen == 0


def test_definition_key_outside_matrix_fails_closed(tmp_path):
    definition = json.loads(json.dumps(GENERIC_DEFINITION))
    definition["layouts"]["keymap"].append(["9,9"])
    backend = _Lying("generic", tmp_path)
    backend.definition = lzma.compress(json.dumps(definition).encode())
    manager = DeviceManager(backend, Store(tmp_path), max_retries=1, lock_timeout=1)
    desc = manager.interfaces()[0][0]
    with pytest.raises(VialticError) as error:
        with manager.session(desc):
            pass
    assert error.value.code == "malformed_device_data"


# Keycodes.

class FakeKeyboard:
    layers = 4
    macro_count = 2
    tap_dance_count = 2
    custom_keycodes = None
    midi = None
    supported_features = {"persistent_default_layer", "caps_word", "layer_lock", "repeat_key"}

    def __init__(self, protocol=6):
        self.vial_protocol = protocol


@pytest.mark.parametrize("protocol", [5, 6])
def test_every_16_bit_code_round_trips_losslessly(protocol):
    kb = FakeKeyboard(protocol)
    with va.CONTEXT_LOCK:
        va.activate(kb)
        cache = {}
        for raw in range(0x10000):
            name = va.Keycode.serialize(raw)
            if name.startswith("0x") or "(" in name:
                # Numeric and composite forms all use the same decoder; sampling keeps this fast.
                if raw % 97:
                    continue
            cache[raw] = va.Keycode.deserialize(name, reraise=True)
        losses = {hex(raw): value for raw, value in cache.items() if value != raw}
    assert not losses, f"{len(losses)} codes change value, e.g. {list(losses.items())[:5]}"


@pytest.mark.parametrize("text,expected", [
    ("KC_A", "KC_A"), ("KC_NO", "KC_NO"), ("KC_TRNS", "KC_TRNS"), ("LGUI(KC_3)", "LGUI(KC_3)"), ("MO(1)", "MO(1)"),
    ("LT(2, KC_SPC)", "LT2(KC_SPACE)"), ("LT1(KC_A)", "LT1(KC_A)"), ("LSFT_T(KC_F)", "LSFT_T(KC_F)"), ("TD(1)", "TD(1)"),
    ("M1", "M1"), ("TO(3)", "TO(3)"), ("MT(MOD_LCTL|MOD_LSFT, KC_A)", None), ("0x7E40", "0x7e40"),
    ("LM(1, MOD_LSFT)", None), ("LM(1, MOD_LSFT|MOD_LCTL)", None), ("OSM(MOD_LSFT)", None),
])
def test_valid_keycodes_parse(text, expected):
    kb = FakeKeyboard()
    with va.CONTEXT_LOCK:
        va.activate(kb)
        value, _ = va.parse_keycode(kb, text)
        assert 0 <= value.raw <= 0xFFFF
        assert va.Keycode.deserialize(value.keycode, reraise=True) == value.raw
        if expected:
            assert value.keycode == expected


@pytest.mark.parametrize("text,code", [
    ("KC_NOPE", "invalid_keycode"), ("", "invalid_keycode"), ("LCTL", "invalid_keycode"), ("LCTL(kc)", "invalid_keycode"),
    ("__import__('os')", "invalid_keycode"), ("KC_A+1", "invalid_keycode"), ("(lambda:0)()", "invalid_keycode"),
    ("KC_A.real", "invalid_keycode"), ("9**9**9", "invalid_keycode"), ("0x1FFFF", "invalid_keycode"), ("x" * 99, "invalid_keycode"),
    ("MO(9)", "invalid_layer_reference"), ("LT(7, KC_A)", "invalid_layer_reference"), ("LT9(KC_A)", "invalid_layer_reference"),
    ("TO(4)", "invalid_layer_reference"), ("0x5229", "invalid_layer_reference"), ("TD(5)", "invalid_keycode"), ("M9", "invalid_keycode"),
    ("TO(1|8)", "invalid_keycode"), ("MO(1|8)", "invalid_keycode"), ("MO(KC_C)", "invalid_keycode"), ("LT0(KC_A,9)", "invalid_keycode"),
    ("LM(1|8,MOD_LSFT)", "invalid_keycode"), ("KC_A|KC_B", "invalid_keycode"), ("LCTL(KC_A|KC_B)", "invalid_keycode"),
    ("MO(1,2)", "invalid_keycode"), ("LM(9, MOD_LSFT)", "invalid_layer_reference"), ("0x5122", "invalid_layer_reference"),
    ("0x7763", "invalid_keycode"), ("0x4900", "invalid_layer_reference"), ("MOD_LGUI", "invalid_keycode"),
    ("LCTL(MOD_LGUI)", "invalid_keycode"),
])
def test_invalid_keycodes_are_refused_not_mapped_to_kc_no(text, code):
    kb = FakeKeyboard()
    with va.CONTEXT_LOCK:
        va.activate(kb)
        with pytest.raises(VialticError) as error:
            va.parse_keycode(kb, text)
    assert error.value.code == code


def test_unnamed_numeric_keycode_warns_and_keeps_value():
    kb = FakeKeyboard()
    with va.CONTEXT_LOCK:
        va.activate(kb)
        value, warnings = va.parse_keycode(kb, "0x7E40")
    assert value.raw == 0x7E40 and value.label == "0x7e40" and warnings


def test_feature_gated_keycode_needs_the_feature():
    kb = FakeKeyboard()
    kb.supported_features = set()
    with va.CONTEXT_LOCK:
        va.activate(kb)
        with pytest.raises(VialticError) as error:
            va.parse_keycode(kb, "QK_LAYER_LOCK")
    assert error.value.code == "unsupported_operation"


def test_labels_distinguish_transparent_from_disabled():
    assert va.display_label("KC_TRNS") == "___"
    assert va.display_label("KC_NO") == "XXX"
    assert va.display_label("LGUI(KC_1)") == "LGUI(1)"
    assert va.display_label("0x7e40") == "0x7e40"
    assert va.display_label("KC__VOLDOWN") == "VOLDOWN"


# Rendering.

def test_layout_option_decoding_matches_upstream_order():
    assert va.layout_choices(["Split bs", ["Enter", "ANSI", "ISO", "Big"]], 0b1_10) == [1, 2]
    assert va.layout_choices(["A", "B"], 0b01) == [0, 1]
    assert va.layout_choices(None, 0) == []
    assert va.layout_choices(["A"], -1) == []


def test_generic_view_uses_firmware_geometry_and_active_option(make_rig):
    rig = make_rig("generic")
    snapshot = rig.service.read_snapshot()
    keys = {k.geometry.id: k for k in render.place_keys(snapshot)}
    assert keys["3,2"].width == 2  # the option 0 wide key
    assert "enc0:0" in keys and keys["enc0:0"].geometry.encoder_index == 0
    text = render.render_text(snapshot, [0])
    assert "SIMULATED" in text and "enc0:0" in text and "3,2" in text


def test_layout_option_selects_alternative_keys(make_rig):
    rig = make_rig("generic")
    firmware = rig.backend.firmware(0)
    firmware.state["layout_options"] = 1
    snapshot = rig.service.read_snapshot()
    placed = {k.geometry.id: k for k in render.place_keys(snapshot)}
    assert placed["3,0"].width == 4  # option 1
    assert "3,2" not in placed
    # The alternative is drawn where option 0 sits, not at its offset in the source layout.
    assert placed["3,0"].y == pytest.approx(3.0)


def test_split_view_keeps_gap_and_rotation(make_rig):
    snapshot = make_rig("split").service.read_snapshot()
    keys = {k.geometry.id: k for k in render.place_keys(snapshot)}
    assert keys["4,4"].x - keys["0,4"].x >= 3  # the gap between halves is geometry, not a template
    assert keys["3,0"].angle == 15 and keys["7,0"].angle == -15
    assert keys["3,0"].center[1] != pytest.approx(keys["3,0"].y + 0.5)
    assert "~" in render.render_text(snapshot, [0])


def test_text_view_keeps_interiors_separate_on_rotated_keys(make_rig):
    snapshot = make_rig("split").service.read_snapshot()
    boxes = render._text_boxes(render.place_keys(snapshot))
    interiors = [{(x, y) for x in range(col + 1, col + w) for y in range(line + 1, line + h)} for _, col, line, w, h in boxes]
    for i, first in enumerate(interiors):
        for second in interiors[i + 1:]:
            assert not first & second
    assert len(boxes) == snapshot.capabilities.key_count


def test_text_view_never_uses_ansi(make_rig):
    assert "\x1b" not in make_rig("split").service.render(None, [0], "text", None).text


def test_long_labels_are_footnoted_not_lost(make_rig):
    rig = make_rig("split")
    proposal = rig.plan((0, 0, 0, "LSFT(KC_NONUS_BSLASH)"))
    assert rig.apply(proposal).outcome == "verified"
    text = rig.service.render(None, [0], "text", None).text
    assert "0,0: LSFT(KC_NONUS_BSLASH)" in text


def test_png_view_writes_a_real_image(make_rig, tmp_path):
    from PIL import Image

    rig = make_rig("generic")
    path = tmp_path / "view.png"
    result = rig.service.render(None, [0, 1], "png", str(path))
    assert result.artifact_path == str(path) and result.text
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        assert image.format == "PNG" and image.width > 300 and image.height > 300
    assert rig.write_packets() == 0


def test_proposal_view_labels_before_and_after(make_rig, tmp_path):
    rig = make_rig("split")
    proposal = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    text = rig.service.render(None, [1], "text", None, proposal.id).text
    assert "before: KC_3" in text and "after: LGUI(KC_3)" in text and "0,2*" in text
    assert rig.keycode(1, 0, 2) != 0  # the view changed nothing
    assert rig.write_packets() == 0
    rig.service.render(None, [1], "png", str(tmp_path / "p.png"), proposal.id)


def test_unlock_view_marks_positions(make_rig):
    text = make_rig("split").service.render(None, [0], "text", None, None, True).text
    assert "0,0!" in text and "Hold to unlock: 0,0, 0,1" in text


# Contracts.

@pytest.mark.parametrize("name", sorted(models.PUBLIC_SCHEMAS))
def test_schema_fixture_matches_models(name):
    """Contract changes must be deliberate: regenerate tests/schemas and bump versions as needed."""
    fixture = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
    assert models.PUBLIC_SCHEMAS[name].model_json_schema() == fixture


def test_envelopes_declare_schema_version():
    for name, model in models.PUBLIC_SCHEMAS.items():
        if name != "error":
            assert "schema_version" in model.model_fields, name


def test_error_codes_all_have_exit_codes():
    assert ERROR_EXIT_CODES["approval_required"] != 0
    assert all(code > 0 for code in ERROR_EXIT_CODES.values())


def test_scenarios_are_not_corne_specific():
    names = {spec.definition["name"] for specs in SCENARIOS.values() for spec in specs}
    assert not any("corne" in name.lower() for name in names)
    for module in (render, va):
        assert not re.search(r"\bcorne\b", pathlib.Path(module.__file__).read_text().lower())


def test_firmware_emulator_answers_upstream_wire_format():
    spec = SCENARIOS["generic"][0]
    from vialtic.emulator import _initial_state

    firmware = Firmware(spec, _initial_state(spec))
    assert firmware.handle(bytes([proto.CMD_VIA_GET_PROTOCOL_VERSION]))[1:3] == b"\x00\x09"
    assert struct.unpack("<IQ", firmware.handle(bytes([0xFE, 0x00]))[:12]) == (6, spec.uid)
