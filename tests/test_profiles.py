# SPDX-License-Identifier: GPL-2.0-or-later
"""M4 gate: compatible files import and export without silent loss, and foreign, malformed
or oversized files cannot cause writes."""
import base64
import dataclasses
import json
import os
import stat

import pytest

from conftest import Rig
from vialtic import cli, profiles
from vialtic.errors import VialticError
from vialtic.models import (
    ChangeSet, SetAltRepeatKeyChange, SetComboChange, SetKeyChange, SetKeyOverrideChange, SetLayoutOptionChange,
    SetLightingChange, SetMacroChange, SetQmkSettingChange, SetTapDanceChange,
)


def full(tmp_path, scenario="full"):
    return Rig(tmp_path / "rig", scenario)


def firmware(rig):
    return rig.backend.firmware(0)


def plan(rig, *changes):
    return rig.service.create_proposal(None, ChangeSet(changes=list(changes)))


def customize(rig):
    """A distinctive state in every family, written through the normal path."""
    assert rig.apply(plan(
        rig, SetKeyChange(layer=0, row=0, col=0, keycode="LGUI(KC_3)"), SetLayoutOptionChange(index=1, choice=2),
        SetTapDanceChange(index=0, on_tap="LT(1, KC_A)", tapping_term=150), SetComboChange(index=0, keys=["KC_A", "KC_B"], output="KC_C"),
        SetKeyOverrideChange(index=0, trigger="KC_A", replacement="KC_B", options=0x80),
        SetAltRepeatKeyChange(index=0, keycode="KC_A", alt_keycode="KC_B", options=8), SetQmkSettingChange(qsid=7, value=175),
        SetMacroChange(index=1, actions=[["text", "secret"], ["tap", "LCTL(KC_A)"], ["delay", 10]]),
        SetLightingChange(system="vialrgb", save=True, mode=3))).outcome == "verified"


def state_of(rig):
    fw = firmware(rig)
    light = fw.state["lighting"]
    return dict(keymap=bytes(fw.keymap), layout=fw.state["layout_options"], td=fw.state["tap_dance"], combos=fw.state["combos"],
                ko=fw.state["key_overrides"], alt=fw.state["alt_repeat"], settings=fw.state["settings"], encoders=fw.state["encoders"],
                macro=bytes(fw.macro).split(b"\x00" * 4)[0], light=dict(light))


def snapshot_state(rig):
    return json.loads(json.dumps(state_of(rig), default=lambda value: value.hex()))


def refused(call, code):
    with pytest.raises(VialticError) as error:
        call()
    assert error.value.code == code, error.value.message
    return error.value


def write_vil(tmp_path, data, name="in.vil"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def upstream_vil(rig):
    """What Vial itself writes for this keyboard."""
    entry, desc, _ = rig.service.devices.select(None)
    with rig.service.devices.session(desc) as session:
        session.snapshot()
        return json.loads(session.kb.save_layout())


# Profiles.

def test_save_list_show_delete(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    saved = rig.service.save_profile(None, "work")
    path = rig.store.profile_path("work")
    assert saved.path == str(path) and path.stat().st_mode & 0o777 == 0o600
    listed = rig.service.list_profiles().profiles
    assert [p.name for p in listed] == ["work"] and listed[0].valid and listed[0].origin == "keyboard"
    profile = rig.service.get_profile("work")
    assert profile.configuration.tap_dance[0][4] == 150 and profile.configuration.lighting.vialrgb["mode"] == 3
    rig.service.delete_profile("work")
    assert rig.service.list_profiles().profiles == []
    refused(lambda: rig.service.delete_profile("work"), "profile_not_found")
    refused(lambda: rig.service.get_profile("work"), "profile_not_found")


def test_save_writes_nothing_and_never_overwrites_by_accident(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "work")
    assert rig.write_packets() == 0
    refused(lambda: rig.service.save_profile(None, "work"), "profile_exists")
    rig.service.save_profile(None, "work", overwrite=True)


def test_applying_a_profile_is_an_ordinary_reversible_proposal(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    wanted = snapshot_state(rig)
    rig.service.save_profile(None, "work")
    assert rig.apply(plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"), SetMacroChange(index=1, actions=[]),
                          SetTapDanceChange(index=0, tapping_term=9), SetLightingChange(system="vialrgb", save=False, mode=2))).outcome == "verified"
    packets = rig.write_packets()
    proposal = rig.service.plan_profile(None, "work")
    assert rig.write_packets() == packets, "planning writes nothing"
    assert proposal.proposal_type == "profile" and proposal.unlock.required
    assert set(proposal.affected) == {"keymap", "macros", "tap_dance", "lighting"}
    refused(lambda: rig.service.apply_proposal(proposal.id, None), "approval_required")
    assert rig.apply(proposal).outcome == "verified"
    assert snapshot_state(rig) == wanted
    undo = rig.service.create_undo_proposal(None)
    assert rig.apply(undo).outcome == "verified"
    assert firmware(rig).state["tap_dance"][0][4] == 9


def test_profile_keeps_raw_macro_bytes(tmp_path):
    rig = full(tmp_path)
    fw = firmware(rig)
    raw = b"hi\x01\x7fthere\x00caf\xe9\x00\x01\x09junk\x00ok\x00"
    fw.macro[:len(raw)] = raw
    fw.sync()
    rig.service.save_profile(None, "raw")
    assert rig.service.get_profile("raw").configuration.lossy_macros
    assert rig.apply(plan(rig, SetMacroChange(index=0, actions=[]))).outcome == "verified"
    assert rig.apply(rig.service.plan_profile(None, "raw")).outcome == "verified"
    assert bytes(fw.macro[:len(raw)]) == raw


def test_plan_matching_profile_is_no_changes(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "same")
    refused(lambda: rig.service.plan_profile(None, "same"), "no_changes")


@pytest.mark.parametrize("mode, op_count", [("skip", 0), ("transient", 1), ("save", 1)])
def test_lighting_mode(tmp_path, mode, op_count):
    rig = full(tmp_path)
    rig.service.save_profile(None, "lit")
    firmware(rig).state["lighting"]["mode"] = 3
    if op_count == 0:
        refused(lambda: rig.service.plan_profile(None, "lit", mode), "no_changes")
    else:
        proposal = rig.service.plan_profile(None, "lit", mode)
        assert proposal.operations[0].save is (mode == "save")


def test_stale_and_pending_recovery_rules_still_apply(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "work")
    rig.apply(plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z")))
    proposal = rig.service.plan_profile(None, "work")
    rig.external_set(0, 1, 0, 0x29)
    refused(lambda: rig.apply(proposal), "proposal_stale")
    assert firmware(rig).keymap[0:2] != b"\x00\x04"


@pytest.mark.parametrize("name", ["../evil", "a/b", "/etc/passwd", "..", ".hidden", "", "x" * 65, "a\x00b", "name.json", "a b", "a\nb"])
def test_hostile_profile_names(tmp_path, name):
    rig = full(tmp_path)
    for call in (lambda: rig.service.save_profile(None, name), lambda: rig.service.get_profile(name),
                 lambda: rig.service.delete_profile(name), lambda: rig.service.plan_profile(None, name),
                 lambda: rig.service.import_profile(None, str(tmp_path / "x"), name, offline=True)):
        refused(call, "invalid_request")
    assert not (tmp_path / "evil").exists()


def test_symlinks_are_refused_for_reading(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "real")
    rig.store.profile_path("link").symlink_to(rig.store.profile_path("real"))
    refused(lambda: rig.service.get_profile("link"), "invalid_file")
    assert [p.name for p in rig.service.list_profiles().profiles] == ["real"]
    target = tmp_path / "in.vil"
    target.write_text("{}")
    link = tmp_path / "link.vil"
    link.symlink_to(target)
    assert "symbolic link" in refused(lambda: rig.service.import_profile(None, str(link), "x", offline=True), "invalid_file").message


def test_delete_removes_a_link_not_its_target(tmp_path):
    rig = full(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    rig.store.profile_path("x").parent.mkdir(parents=True, exist_ok=True)
    rig.store.profile_path("x").symlink_to(outside)
    rig.service.delete_profile("x")
    assert outside.read_text() == "keep"


def test_save_does_not_write_through_a_link(tmp_path):
    rig = full(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    rig.store.profile_path("x").parent.mkdir(parents=True, exist_ok=True)
    rig.store.profile_path("x").symlink_to(outside)
    refused(lambda: rig.service.save_profile(None, "x"), "profile_exists")
    rig.service.save_profile(None, "x", overwrite=True)
    assert outside.read_text() == "keep" and not rig.store.profile_path("x").is_symlink()


def test_special_files_are_refused_without_blocking(tmp_path):
    rig = full(tmp_path)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert "regular file" in refused(lambda: rig.service.import_profile(None, str(fifo), "x", offline=True), "invalid_file").message
    refused(lambda: rig.service.import_profile(None, str(tmp_path), "x", offline=True), "invalid_file")
    refused(lambda: rig.service.import_profile(None, str(tmp_path / "missing"), "x", offline=True), "invalid_file")


def test_damaged_profile_is_listed_kept_and_refused(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "bad")
    path = rig.store.profile_path("bad")
    path.write_text("{broken")
    assert rig.service.list_profiles().profiles[0].valid is False
    refused(lambda: rig.service.plan_profile(None, "bad"), "invalid_file")
    assert path.read_text() == "{broken"
    assert rig.write_packets() == 0


# Hostile files.

def _valid_vil(rig):
    data = upstream_vil(rig)
    return data


@pytest.mark.parametrize("content", [
    b"", b"not json", b"\xff\xfe", b"[]", b"3", b"null", b'{"version": 1}', b'{"version": 2, "uid": 1, "layout": []}',
    b'{"version": true, "uid": 1, "layout": []}', b'{"version": 1, "uid": "x", "layout": []}', b'{"version": 1, "uid": -1, "layout": []}',
    b'{"version": 1, "uid": 1}', b'{"version": 1, "uid": 1, "layout": 3}', b'{"version": 1, "uid": 1, "layout": [[[{}]]]}',
    b'{"version": 1, "uid": 1, "layout": [[[1.5]]]}', b'{"version": 1, "uid": 1, "layout": [[[99999]]], "vial_protocol": 6}',
    b'{"version": 1, "uid": 1, "uid": 2, "layout": []}', b'{"version": 1, "uid": NaN, "layout": []}',
    b'{"version": 1, "uid": 1, "layout": [], "settings": {"abc": 1}}', b'{"version": 1, "uid": 1, "layout": [], "settings": {"7": true}}',
    b'{"version": 1, "uid": 1, "layout": [], "tap_dance": [["KC_A"]]}', b'{"version": 1, "uid": 1, "layout": [], "macro": [[["unknown"]]]}',
    b'{"version": 1, "uid": 1, "layout": [], "key_override": [{"trigger": "KC_A"}]}',
    b'{"version": 1, "uid": 1, "layout": [[[5]]]}', b'{"kind": "profile"}', b'{"kind": "other"}',
    b"[" * 100000, b'{"a":' * 100000, b'{"version": 1, "uid": 1, "layout": [], "x": ' + b"[" * 5000 + b"]" * 5000 + b"}",
])
def test_malformed_files_are_refused_cleanly(tmp_path, content):
    rig = full(tmp_path)
    path = tmp_path / "in.vil"
    path.write_bytes(content)
    refused(lambda: rig.service.import_profile(None, str(path), "x"), "invalid_file")
    assert rig.write_packets() == 0 and not rig.store.profile_path("x").exists()


def test_oversized_file_is_refused_before_it_is_read(tmp_path):
    rig = full(tmp_path)
    path = tmp_path / "big.vil"
    with open(path, "wb") as out:
        out.truncate(profiles.MAX_FILE_BYTES + 1)
    assert "byte limit" in refused(lambda: rig.service.import_profile(None, str(path), "x", offline=True), "invalid_file").message


def test_error_messages_do_not_repeat_file_contents(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["macro"][0] = [["text", "hunter2secret"], ["tap", "hunter2secret"]]
    path = write_vil(tmp_path, data)
    error = refused(lambda: rig.service.import_profile(None, path, "x"), "invalid_request")
    assert "hunter2" not in error.message and "hunter2" not in json.dumps(error.details)


def test_expressions_in_files_are_never_evaluated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rig = full(tmp_path)
    data = upstream_vil(rig)
    marker = tmp_path / "pwned"
    data["layout"][0][0][0] = "open('pwned', 'w')"
    path = write_vil(tmp_path, data)
    refused(lambda: rig.service.import_profile(None, path, "x"), "invalid_keycode")
    assert not marker.exists()
    data["layout"][0][0][0] = "__import__('os').system('true')"
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "invalid_keycode")


def test_unreadable_keycode_is_refused_not_turned_into_kc_no(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["layout"][0][0][0] = "KC_NOT_A_THING"
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "invalid_keycode")
    data["tap_dance"][0][0] = "KC_NOT_A_THING"
    data["layout"][0][0][0] = "KC_A"
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "invalid_keycode")


@pytest.mark.parametrize("edit", [
    lambda d: d.update(uid=12345),
    lambda d: d["layout"].append(d["layout"][0]),
    lambda d: d["layout"].pop(),
    lambda d: d["layout"][0].append([-1, -1, -1]),
    lambda d: d["layout"][0][0].append("KC_A"),
    lambda d: d["layout"][0][0].__setitem__(2, "KC_A"),  # no key there on this keyboard
    lambda d: d["layout"][0][1].__setitem__(0, -1),  # a key this keyboard has is missing
    lambda d: d["encoder_layout"].pop(),
    lambda d: d["tap_dance"].append(["KC_NO", "KC_NO", "KC_NO", "KC_NO", 200]),
    lambda d: d["combo"].append(["KC_NO"] * 5),
    lambda d: d["key_override"].append(dict(d["key_override"][0])),
    lambda d: d["alt_repeat_key"].append(dict(d["alt_repeat_key"][0])),
    lambda d: d["macro"].append([]),
    lambda d: d.update(layout_options=0x40000000),
    lambda d: d["settings"].update({"60000": 1}) or d.update(layout_options=0x40000000),
])
def test_incompatible_files_are_refused_with_no_force(tmp_path, edit):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    edit(data)
    path = write_vil(tmp_path, data)
    error = refused(lambda: rig.service.import_profile(None, path, "x"), "profile_incompatible")
    assert "no way to force" in error.next_steps[0]
    assert rig.write_packets() == 0 and not rig.store.profile_path("x").exists()
    # Storing it offline does not get around the check.
    rig.service.import_profile(None, path, "x", offline=True)
    refused(lambda: rig.service.plan_profile(None, "x"), "profile_incompatible")


def test_unsupported_setting_in_a_file_is_refused(tmp_path):
    rig = Rig(tmp_path / "split", "split")
    data = upstream_vil(rig)
    data["settings"]["20"] = 1  # known to the table, not exposed by this keyboard
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "profile_incompatible")


def test_setting_out_of_range_in_a_file_is_refused(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["settings"]["7"] = 999999
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "invalid_request")


def test_macro_too_big_in_a_file_is_refused_not_truncated(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["macro"][0] = [["text", "x" * 100]]
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "capacity_exceeded")


def test_file_keycode_uses_the_strict_grammar_and_references(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["layout"][0][0][0] = "MO(9)"
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "x"), "invalid_layer_reference")


def test_raw_buffer_with_wrong_macro_count_is_refused(tmp_path):
    rig = full(tmp_path)
    rig.service.export_configuration(None, str(tmp_path / "out.json"), "vialtic", include_macros=True)
    data = json.loads((tmp_path / "out.json").read_text())
    data["configuration"]["macro_buffer_b64"] = base64.b64encode(b"evil\x00").decode()
    (tmp_path / "out.json").write_text(json.dumps(data))
    refused(lambda: rig.service.import_profile(None, str(tmp_path / "out.json"), "x"), "invalid_file")


def test_forged_raw_and_name_cannot_hide_a_boot_key(tmp_path):
    """Only the keycode text counts in a profile, and QK_BOOT still needs the declared unlock."""
    rig = full(tmp_path)
    rig.service.save_profile(None, "work")
    path = rig.store.profile_path("work")
    data = json.loads(path.read_text())
    data["configuration"]["keymap"][0]["keycode"] = "QK_BOOT"
    path.write_text(json.dumps(data))
    assert rig.service.plan_profile(None, "work").unlock.required


@pytest.mark.parametrize("edit", [
    lambda d: d.update(schema_version=99), lambda d: d.update(future=1), lambda d: d["configuration"].update(future=1),
    lambda d: d["configuration"]["keymap"][0].update(layer=99), lambda d: d["configuration"].update(tap_dance=[["KC_A"]]),
    lambda d: d["configuration"].update(macro_buffer_b64="!!!"), lambda d: d.pop("source"),
])
def test_vialtic_files_are_strict(tmp_path, edit):
    rig = full(tmp_path)
    rig.service.export_configuration(None, str(tmp_path / "out.json"), "vialtic", include_macros=True)
    data = json.loads((tmp_path / "out.json").read_text())
    edit(data)
    (tmp_path / "out.json").write_text(json.dumps(data))
    refused(lambda: rig.service.import_profile(None, str(tmp_path / "out.json"), "x"), "invalid_file")


def test_unknown_vialtic_fields_are_named_when_refused(tmp_path):
    rig = full(tmp_path)
    rig.service.export_configuration(None, str(tmp_path / "out.json"), "vialtic")
    data = json.loads((tmp_path / "out.json").read_text())
    data["future"] = 1
    (tmp_path / "out.json").write_text(json.dumps(data))
    error = refused(lambda: rig.service.import_profile(None, str(tmp_path / "out.json"), "x"), "invalid_file")
    assert error.details["unknown_fields"] == ["future"]


def test_other_identity_with_same_firmware_is_allowed_and_said(tmp_path):
    rig = Rig(tmp_path / "rig", "serial")
    rig.service.save_profile(None, "work")
    path = rig.store.profile_path("work")
    data = json.loads(path.read_text())
    data["source"]["binding_id"] = "another"
    data["configuration"]["keymap"][0]["keycode"] = "KC_Z"
    path.write_text(json.dumps(data))
    assert any("different USB identity" in w for w in rig.service.plan_profile(None, "work").warnings)


def test_raw_values_across_a_protocol_generation_are_refused(tmp_path):
    rig = full(tmp_path)
    rig.service.save_profile(None, "work")
    fw = firmware(rig)
    fw.spec = dataclasses.replace(fw.spec, vial_protocol=5)
    refused(lambda: rig.service.plan_profile(None, "work"), "profile_incompatible")


# .vil round trips.

def test_export_matches_what_upstream_writes(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    expected = upstream_vil(rig)
    result = rig.service.export_configuration(None, str(tmp_path / "out.vil"), "vil", include_macros=True)
    assert result.macros_included and any("Lighting" in o for o in result.omissions)
    assert json.loads((tmp_path / "out.vil").read_text()) == json.loads(json.dumps(expected))


def test_upstream_reads_what_vialtic_exports(tmp_path):
    """Vial's own restore, on another emulated keyboard, ends with the same configuration."""
    rig = full(tmp_path)
    customize(rig)
    rig.service.export_configuration(None, str(tmp_path / "out.vil"), "vil", include_macros=True)
    expected = snapshot_state(rig)
    other = Rig(tmp_path / "other", "full")
    entry, desc, _ = other.service.devices.select(None)
    firmware(other).state["unlocked"] = True
    with other.service.devices.session(desc) as session:
        session.snapshot()
        with session.transport.writes():
            session.kb.restore_layout((tmp_path / "out.vil").read_bytes())
    got = snapshot_state(other)
    expected.pop("light"), got.pop("light")
    assert got == expected


def test_file_from_upstream_imports_and_applies_with_typed_operations(tmp_path):
    source = full(tmp_path)
    customize(source)
    data = upstream_vil(source)
    wanted = snapshot_state(source)
    rig = Rig(tmp_path / "target", "full")
    packets = rig.write_packets()
    result = rig.service.import_profile(None, write_vil(tmp_path, data), "vil")
    assert result.compatibility == "compatible" and any("Lighting" in o for o in result.omissions)
    assert rig.write_packets() == packets
    proposal = rig.service.plan_profile(None, "vil")
    assert proposal.proposal_type == "profile" and "lighting" not in proposal.affected
    assert any("no lighting" in w for w in proposal.warnings)
    assert rig.apply(proposal).outcome == "verified"
    got = snapshot_state(rig)
    assert firmware(rig).state["lighting"]["mode"] == 2, ".vil cannot touch lighting"
    wanted.pop("light"), got.pop("light")
    assert got == wanted


def test_vil_round_trip_through_vialtic_loses_nothing(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    original = upstream_vil(rig)
    original["vendor_extension"] = {"keep": ["me"]}
    result = rig.service.import_profile(None, write_vil(tmp_path, original), "rt")
    assert result.unknown_fields == ["vendor_extension"] and any("vendor_extension" in n for n in result.notes)
    refused(lambda: rig.service.plan_profile(None, "rt"), "no_changes")
    exported = rig.service.export_configuration(None, str(tmp_path / "again.vil"), "vil", "rt", include_macros=True)
    assert exported.unknown_fields == ["vendor_extension"]
    assert json.loads((tmp_path / "again.vil").read_text()) == json.loads(json.dumps(original))


def test_old_vil_with_raw_codes_needs_a_protocol(tmp_path):
    rig = full(tmp_path)
    data = upstream_vil(rig)
    data["layout"][0][0][0] = 4
    with_protocol = write_vil(tmp_path, data)
    rig.service.import_profile(None, with_protocol, "raw")
    data.pop("vial_protocol")
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, data), "raw2"), "invalid_file")


def test_vil_without_macros_leaves_macros_alone(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    rig.service.export_configuration(None, str(tmp_path / "plain.vil"), "vil")
    data = json.loads((tmp_path / "plain.vil").read_text())
    assert "macro" not in data
    result = rig.service.import_profile(None, str(tmp_path / "plain.vil"), "plain")
    assert any("no macros" in n for n in result.notes)
    assert rig.apply(plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))).outcome == "verified"
    proposal = rig.service.plan_profile(None, "plain")
    assert "macros" not in proposal.affected and rig.apply(proposal).outcome == "verified"
    assert b"secret" in bytes(firmware(rig).macro)


def test_lossy_macros_are_reported_on_vil_export(tmp_path):
    rig = full(tmp_path)
    fw = firmware(rig)
    fw.macro[:10] = b"caf\xe9\x00\x00\x00\x00\x00\x00"
    fw.sync()
    result = rig.service.export_configuration(None, str(tmp_path / "out.vil"), "vil", include_macros=True)
    assert any("cannot represent exactly" in o for o in result.omissions)
    rig.service.export_configuration(None, str(tmp_path / "out.json"), "vialtic", include_macros=True)
    assert base64.b64decode(json.loads((tmp_path / "out.json").read_text())["configuration"]["macro_buffer_b64"]).startswith(b"caf\xe9")


def test_vial_file_with_bytes_the_strict_encoder_refuses_is_refused(tmp_path):
    """Importing what upstream wrote for such a macro would change its bytes, so it is not done silently."""
    rig = full(tmp_path)
    fw = firmware(rig)
    fw.macro[:10] = b"caf\xe9\x00\x00\x00\x00\x00\x00"
    fw.sync()
    refused(lambda: rig.service.import_profile(None, write_vil(tmp_path, upstream_vil(rig)), "x"), "invalid_request")


# Vialtic format round trip.

def test_vialtic_export_import_is_complete(tmp_path):
    rig = full(tmp_path)
    customize(rig)
    wanted = snapshot_state(rig)
    assert rig.service.export_configuration(None, str(tmp_path / "out.json"), "vialtic", include_macros=True).omissions == []
    result = rig.service.import_profile(None, str(tmp_path / "out.json"), "back")
    assert result.origin == "keyboard"
    assert rig.apply(plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"), SetMacroChange(index=1, actions=[]),
                          SetLightingChange(system="vialrgb", save=True, mode=1))).outcome == "verified"
    assert rig.apply(rig.service.plan_profile(None, "back")).outcome == "verified"
    assert snapshot_state(rig) == wanted


def test_snapshot_file_imports_too_and_says_macros_are_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    target = tmp_path / "snap.json"
    assert cli.main(["snapshot", "--emulator", "full", "--output", str(target)]) == 0
    capsys.readouterr()
    assert cli.main(["import", str(target), "--name", "snap", "--emulator", "full", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["compatibility"] == "compatible" and any("Macros" in o for o in result["omissions"])


# Sensitive contents.

def test_macro_contents_stay_out_of_default_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    changes = tmp_path / "c.json"
    changes.write_text(json.dumps({"schema_version": 1, "changes": [{"type": "set_macro", "index": 0, "actions": [["text", "hunter2secret"]]}]}))
    em = ("--emulator", "full")

    def run(*argv):
        code = cli.main([*argv, *em])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    code, out = run("plan", "--changes", str(changes), "--json")
    assert run("apply", json.loads(out)["result"]["id"], "--user-approved")[0] == 0
    outputs = [run("profile", "save", "p")[1], run("profile", "show", "p")[1], run("profile", "show", "p", "--json")[1],
               run("profile", "list")[1], run("export", "--output", str(tmp_path / "a.json"))[1],
               run("export", "--output", str(tmp_path / "a.vil"), "--format", "vil")[1],
               run("export", "--output", str(tmp_path / "b.json"), "--include-macros")[1],
               run("export", "--output", str(tmp_path / "b.vil"), "--format", "vil", "--include-macros")[1],
               run("import", str(tmp_path / "b.vil"), "--name", "q")[1]]
    encoded = base64.b64encode(b"hunter2secret").decode()[:10]
    assert not any("hunter2" in text or encoded in text for text in outputs)
    for name in ("a.json", "a.vil"):
        assert "hunter2" not in (tmp_path / name).read_text() and encoded not in (tmp_path / name).read_text()
    for name in ("a.json", "a.vil", "b.json", "b.vil"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert "hunter2" in (tmp_path / "b.vil").read_text()
    assert "hunter2" in run("profile", "show", "p", "--include-macros")[1]
    profile_dir = tmp_path / "data" / "vialtic" / "emulator" / "full" / "profiles"
    assert (profile_dir / "p.json").stat().st_mode & 0o777 == 0o600
    assert stat.S_IMODE(profile_dir.stat().st_mode) == 0o700


def test_export_refuses_to_replace_or_follow(tmp_path):
    rig = full(tmp_path)
    existing = tmp_path / "out.json"
    existing.write_text("mine")
    refused(lambda: rig.service.export_configuration(None, str(existing), "vialtic"), "output_exists")
    assert existing.read_text() == "mine"
    link = tmp_path / "link.json"
    link.symlink_to(existing)
    refused(lambda: rig.service.export_configuration(None, str(link), "vialtic", overwrite=True), "output_exists")
    assert existing.read_text() == "mine"
    (tmp_path / ".out.json.swp").write_text("theirs")
    rig.service.export_configuration(None, str(existing), "vialtic", overwrite=True)
    assert (tmp_path / ".out.json.swp").exists()


def test_profiles_are_isolated_between_real_and_emulated(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert cli.main(["profile", "save", "e", "--emulator", "full"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "data" / "vialtic" / "profiles").exists()


def test_cli_journey(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    em = ("--emulator", "split")

    def run(*argv):
        code = cli.main([*argv, *em, "--json"])
        return code, json.loads(capsys.readouterr().out)

    assert run("profile", "save", "base")[0] == 0
    code, data = run("plan", "--set", "1:0,2=KC_A")
    assert run("apply", data["result"]["id"], "--user-approved")[0] == 0
    code, data = run("profile", "plan", "base")
    assert code == 0 and data["result"]["proposal_type"] == "profile"
    assert run("apply", data["result"]["id"], "--user-approved")[1]["result"]["outcome"] == "verified"
    code, data = run("profile", "delete", "base")
    assert code == 0 and run("profile", "list")[1]["result"]["profiles"] == []
    code, data = run("profile", "show", "../x")
    assert code == 7 and data["error"]["code"] == "invalid_request"
