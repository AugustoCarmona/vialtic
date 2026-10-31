# SPDX-License-Identifier: GPL-2.0-or-later
"""M3 gate: each family is read, validated, proposed, applied, verified, backed up and restored."""
import json
import stat

import pytest

from conftest import Rig
from vialtic import proposals
from vialtic.emulator import Faults
from vialtic.errors import VialticError
from vialtic.models import (
    ChangeSet, SetAltRepeatKeyChange, SetComboChange, SetKeyChange, SetKeyOverrideChange, SetLayoutOptionChange,
    SetQmkSettingChange, SetTapDanceChange,
)
from vialtic.service import attestation_for


def full(tmp_path, faults=None, **options):
    return Rig(tmp_path, "full", faults, **options)


def plan(rig, *changes):
    return rig.service.create_proposal(None, ChangeSet(changes=list(changes)))


def refused(rig, code, *changes):
    packets = rig.write_packets()
    with pytest.raises(VialticError) as error:
        plan(rig, *changes)
    assert error.value.code == code, error.value.message
    assert rig.write_packets() == packets
    return error.value


def firmware(rig):
    return rig.backend.firmware(0)


def undo(rig):
    proposal = rig.service.create_undo_proposal(None)
    result = rig.apply(proposal)
    assert result.outcome == "verified", result.message
    return proposal


def check_family_cycle(rig, proposal, read, family):
    """Apply, verify, previous.json holds the old value, undo restores it, and the undo can itself be undone."""
    binding = proposal.device.binding_id
    original = read()
    assert proposal.affected == [family] and rig.write_packets() == 0
    result = rig.apply(proposal)
    assert result.outcome == "verified", (result.message, result.differences)
    changed = read()
    assert changed != original
    assert rig.store.previous_path(binding).exists()
    assert not rig.store.has_unresolved_write(binding)
    undo(rig)
    assert read() == original
    undo(rig)
    assert read() == changed
    undo(rig)
    assert read() == original


# Layout options.

def test_layout_option_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetLayoutOptionChange(index=1, choice=2))
    op = proposal.operations[0]
    assert (op.before, op.after) == (0, 2) and op.after_choices == [0, 2]
    check_family_cycle(rig, proposal, lambda: firmware(rig).state["layout_options"], "layout_options")


def test_layout_options_merge_and_keep_unknown_bits(tmp_path):
    rig = full(tmp_path)
    firmware(rig).state["layout_options"] = 0x40000000  # a bit no option claims
    proposal = plan(rig, SetLayoutOptionChange(index=0, choice=1), SetLayoutOptionChange(index=1, choice=2))
    assert len(proposal.operations) == 1 and proposal.operations[0].after == 0x40000000 | 0b110
    assert rig.apply(proposal).outcome == "verified"
    assert firmware(rig).state["layout_options"] == 0x40000000 | 0b110


@pytest.mark.parametrize("index, choice, code", [(5, 0, "invalid_coordinate"), (-1, 0, "invalid_coordinate"),
                                                 (0, 2, "invalid_request"), (1, 3, "invalid_request"),
                                                 (1, -1, "invalid_request")])
def test_layout_option_bounds(tmp_path, index, choice, code):
    refused(full(tmp_path), code, SetLayoutOptionChange(index=index, choice=choice))


def test_layout_option_change_set_twice_is_refused(tmp_path):
    refused(full(tmp_path), "invalid_request", SetLayoutOptionChange(index=0, choice=1),
            SetLayoutOptionChange(index=0, choice=0))


def test_layout_option_without_capability(tmp_path):
    error = refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetLayoutOptionChange(index=0, choice=1))
    assert error.details["family"] == "layout_options"


def test_layout_option_lost_write_does_not_verify(tmp_path):
    rig = full(tmp_path, Faults(drop_writes_after=0))
    result = rig.apply(plan(rig, SetLayoutOptionChange(index=1, choice=2)))
    assert result.outcome != "verified" and firmware(rig).state["layout_options"] == 0


def test_layout_option_view_lists_the_change(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetLayoutOptionChange(index=1, choice=2))
    view = rig.service.render(None, [0], "text", None, proposal.id)
    assert "layout options: 0 [0, 0] -> 2 [0, 2]" in view.text


# Tap dance.

def test_tap_dance_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetTapDanceChange(index=1, on_tap="KC_A", on_hold="LCTL(KC_B)", tapping_term=175))
    op = proposal.operations[0]
    assert op.before.on_tap.keycode == "KC_NO" and op.after.on_hold.keycode == "LCTL(KC_B)"
    assert op.after.on_double_tap.keycode == "KC_NO", "a field left out keeps its value"
    assert "on_tap KC_NO -> KC_A" in proposals.describe_operation(op)
    check_family_cycle(rig, proposal, lambda: list(firmware(rig).state["tap_dance"][1]), "tap_dance")


def test_tap_dance_writes_the_expected_wire_values(tmp_path):
    rig = full(tmp_path)
    assert rig.apply(plan(rig, SetTapDanceChange(index=0, on_tap="KC_A", on_tap_hold="LT(1, KC_B)", tapping_term=321))).outcome == "verified"
    entry = firmware(rig).state["tap_dance"][0]
    assert entry[0] == 0x04 and entry[1] == 0 and entry[4] == 321 and entry[3] != 0
    assert rig.service.read_snapshot().configuration.tap_dance[0][3] == "LT1(KC_B)"


@pytest.mark.parametrize("change, code", [
    (dict(index=2), "invalid_coordinate"), (dict(index=-1), "invalid_coordinate"),
    (dict(index=0, on_tap="KC_NOPE"), "invalid_keycode"), (dict(index=0, on_tap="MO(9)"), "invalid_layer_reference"),
    (dict(index=0, on_hold="TD(9)"), "invalid_keycode"), (dict(index=0, tapping_term=65536), "invalid_request"),
    (dict(index=0, tapping_term=-1), "invalid_request"),
])
def test_tap_dance_bounds(tmp_path, change, code):
    refused(full(tmp_path), code, SetTapDanceChange(**change))


def test_tap_dance_same_values_is_no_change(tmp_path):
    rig = full(tmp_path)
    assert refused(rig, "no_changes", SetTapDanceChange(index=0, tapping_term=200))


def test_tap_dance_without_capability(tmp_path):
    error = refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetTapDanceChange(index=0, on_tap="KC_A"))
    assert error.details["family"] == "tap_dance"


def test_tap_dance_with_boot_needs_unlock_even_when_only_kept(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetTapDanceChange(index=0, on_double_tap="QK_BOOT"))
    assert proposal.unlock.required and proposal.unlock.positions
    assert rig.apply(proposal).outcome == "verified" and firmware(rig).state["tap_dance"][0][2] == firmware(rig).boot_code
    assert rig.service.unlock_status(None).unlocked is False, "locked again afterwards"
    # Rewriting that entry would turn the kept QK_BOOT into KC_NO on a locked keyboard.
    proposal = plan(rig, SetTapDanceChange(index=0, tapping_term=111))
    assert proposal.unlock.required
    assert rig.apply(proposal).outcome == "verified" and firmware(rig).state["tap_dance"][0][2] == firmware(rig).boot_code


def test_tap_dance_firewalled_write_is_not_reported_as_success(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetTapDanceChange(index=0, on_tap="QK_BOOT"))
    tampered = proposal.model_copy(update={"unlock": proposal.unlock.model_copy(update={"required": False, "positions": []})})
    rig.service._load_proposal = lambda _: tampered
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(proposal.id, attestation_for(proposal, rig.options.now()))
    assert error.value.code == "unlock_required" and firmware(rig).state["tap_dance"][0][0] == 0


def test_tap_dance_lost_write_restores(tmp_path):
    rig = full(tmp_path, Faults(drop_writes_after=1))
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"), SetTapDanceChange(index=0, on_tap="KC_A"))
    result = rig.apply(proposal)
    assert result.outcome == "partial", "a lost write also blocks the restore"
    assert rig.store.has_unresolved_write(proposal.device.binding_id)


# Combos.

def test_combo_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetComboChange(index=1, keys=["KC_A", "KC_B"], output="LCTL(KC_C)"))
    op = proposal.operations[0]
    assert [k.keycode for k in op.after.keys] == ["KC_A", "KC_B", "KC_NO", "KC_NO"] and op.after.output.keycode == "LCTL(KC_C)"
    assert "=> LCTL(KC_C)" in proposals.describe_operation(op)
    check_family_cycle(rig, proposal, lambda: list(firmware(rig).state["combos"][1]), "combos")


def test_combo_partial_change_keeps_the_rest(tmp_path):
    rig = full(tmp_path)
    assert rig.apply(plan(rig, SetComboChange(index=0, keys=["KC_A", "KC_B", "KC_C", "KC_D"], output="KC_E"))).outcome == "verified"
    assert rig.apply(plan(rig, SetComboChange(index=0, output="KC_F"))).outcome == "verified"
    assert firmware(rig).state["combos"][0] == [4, 5, 6, 7, 9]


@pytest.mark.parametrize("change, code", [
    (dict(index=2, output="KC_A"), "invalid_coordinate"), (dict(index=0, keys=["KC_NOPE"]), "invalid_keycode"),
    (dict(index=0, output="MO(9)"), "invalid_layer_reference"),
])
def test_combo_bounds(tmp_path, change, code):
    refused(full(tmp_path), code, SetComboChange(**change))


def test_combo_rejects_too_many_inputs():
    with pytest.raises(ValueError):
        SetComboChange(index=0, keys=["KC_A"] * 5)


def test_combo_gap_warns(tmp_path):
    rig = full(tmp_path)
    assert any("KC_NO before another input" in w for w in plan(rig, SetComboChange(index=0, keys=["KC_NO", "KC_A"])).warnings)


def test_combo_without_capability(tmp_path):
    assert refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetComboChange(index=0, output="KC_A")).details["family"] == "combos"


def test_combo_boot_output_needs_unlock(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetComboChange(index=0, keys=["KC_A", "KC_B"], output="QK_BOOT"))
    assert proposal.unlock.required
    assert rig.apply(proposal).outcome == "verified" and firmware(rig).state["combos"][0][4] == firmware(rig).boot_code
    undo(rig)
    assert firmware(rig).state["combos"][0][4] == 0
    assert rig.service.create_undo_proposal(None).unlock.required, "restoring QK_BOOT needs the same unlock"


# Key overrides.

def test_key_override_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetKeyOverrideChange(index=1, trigger="KC_A", replacement="KC_B", layers=0b11, trigger_mods=1, options=0x87))
    assert "replacement KC_NO -> KC_B" in proposals.describe_operation(proposal.operations[0])
    check_family_cycle(rig, proposal, lambda: list(firmware(rig).state["key_overrides"][1]), "key_overrides")


def test_key_override_keeps_option_bits_upstream_drops(tmp_path):
    rig = full(tmp_path)
    firmware(rig).state["key_overrides"][0] = [4, 5, 0xFFFF, 0, 0, 0, 0b11000000]  # bit 6 is not named upstream
    assert rig.service.read_snapshot().configuration.key_overrides[0]["options"] == 0b11000000
    assert rig.apply(plan(rig, SetKeyOverrideChange(index=0, trigger_mods=2))).outcome == "verified"
    assert firmware(rig).state["key_overrides"][0][6] == 0b11000000
    undo(rig)
    assert firmware(rig).state["key_overrides"][0] == [4, 5, 0xFFFF, 0, 0, 0, 0b11000000]


def test_key_override_unknown_bit_cannot_be_changed(tmp_path):
    refused(full(tmp_path), "invalid_request", SetKeyOverrideChange(index=0, options=0b01000000))


def test_lost_unknown_option_bit_is_a_difference(tmp_path):
    """Upstream's filter would hide this: both sides look equal once the bit is dropped."""
    rig = full(tmp_path)
    firmware(rig).state["key_overrides"][0] = [4, 5, 0xFFFF, 0, 0, 0, 0b11000000]
    proposal = plan(rig, SetKeyOverrideChange(index=0, trigger_mods=2))
    original = rig.service._write_operation

    def clears_the_bit(session, op):
        original(session, op)
        firmware(rig).state["key_overrides"][0][6] = 0b10000000

    rig.service._write_operation = staticmethod(clears_the_bit)
    assert rig.apply(proposal).outcome != "verified"


@pytest.mark.parametrize("change, code", [
    (dict(index=2, trigger="KC_A"), "invalid_coordinate"), (dict(index=0, trigger="KC_NOPE"), "invalid_keycode"),
    (dict(index=0, layers=0x10000), "invalid_request"), (dict(index=0, layers=0b100), "invalid_layer_reference"),
    (dict(index=0, trigger_mods=256), "invalid_request"), (dict(index=0, negative_mod_mask=-1), "invalid_request"),
    (dict(index=0, suppressed_mods=999), "invalid_request"), (dict(index=0, options=256), "invalid_request"),
])
def test_key_override_bounds(tmp_path, change, code):
    refused(full(tmp_path), code, SetKeyOverrideChange(**change))


def test_key_override_all_layers_mask_is_allowed(tmp_path):
    rig = full(tmp_path)
    firmware(rig).state["key_overrides"][0][2] = 1
    assert plan(rig, SetKeyOverrideChange(index=0, layers=0xFFFF)).operations[0].after.layers == 0xFFFF


def test_key_override_without_capability(tmp_path):
    error = refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetKeyOverrideChange(index=0, trigger="KC_A"))
    assert error.details["family"] == "key_overrides"


def test_key_override_boot_replacement_needs_unlock(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetKeyOverrideChange(index=0, trigger="KC_A", replacement="QK_BOOT", options=0x80))
    assert proposal.unlock.required
    assert rig.apply(proposal).outcome == "verified" and firmware(rig).state["key_overrides"][0][1] == firmware(rig).boot_code


# Alternate repeat keys.

def test_alt_repeat_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetAltRepeatKeyChange(index=1, keycode="KC_A", alt_keycode="KC_B", allowed_mods=3, options=0b1001))
    assert "alt_keycode KC_NO -> KC_B" in proposals.describe_operation(proposal.operations[0])
    check_family_cycle(rig, proposal, lambda: list(firmware(rig).state["alt_repeat"][1]), "alt_repeat_keys")


def test_alt_repeat_keeps_option_bits_upstream_drops(tmp_path):
    rig = full(tmp_path)
    firmware(rig).state["alt_repeat"][0] = [4, 5, 0, 0b10001000]
    assert rig.apply(plan(rig, SetAltRepeatKeyChange(index=0, allowed_mods=1))).outcome == "verified"
    assert firmware(rig).state["alt_repeat"][0][3] == 0b10001000
    refused(rig, "invalid_request", SetAltRepeatKeyChange(index=0, options=0b00001000))


@pytest.mark.parametrize("change, code", [
    (dict(index=2, keycode="KC_A"), "invalid_coordinate"), (dict(index=0, alt_keycode="KC_NOPE"), "invalid_keycode"),
    (dict(index=0, allowed_mods=256), "invalid_request"), (dict(index=0, options=0x10), "invalid_request"),
])
def test_alt_repeat_bounds(tmp_path, change, code):
    refused(full(tmp_path), code, SetAltRepeatKeyChange(**change))


def test_alt_repeat_without_capability(tmp_path):
    error = refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetAltRepeatKeyChange(index=0, keycode="KC_A"))
    assert error.details["family"] == "alt_repeat_keys"


@pytest.mark.parametrize("field", ["keycode", "alt_keycode"])
def test_alt_repeat_boot_needs_unlock(tmp_path, field):
    rig = full(tmp_path)
    proposal = plan(rig, SetAltRepeatKeyChange(index=0, **{field: "QK_BOOT"}))
    assert proposal.unlock.required and rig.apply(proposal).outcome == "verified"
    assert rig.service.read_snapshot().configuration.alt_repeat_keys[0][field] == "QK_BOOT"


# QMK settings.

def _setting(rig, qsid):
    return firmware(rig).state["settings"][str(qsid)]


def test_setting_cycle(tmp_path):
    rig = full(tmp_path)
    proposal = plan(rig, SetQmkSettingChange(qsid=7, value=175))
    assert proposals.describe_operation(proposal.operations[0]) == "QMK setting 7 (Tapping Term): 0 -> 175"
    check_family_cycle(rig, proposal, lambda: _setting(rig, 7), "settings")


@pytest.mark.parametrize("qsid, value", [(20, 100), (7, 10000), (21, 0x3FF)])
def test_setting_widths_reach_the_firmware(tmp_path, qsid, value):
    """One, two and four byte settings, at their largest values."""
    rig = full(tmp_path)
    assert rig.apply(plan(rig, SetQmkSettingChange(qsid=qsid, value=value))).outcome == "verified"
    assert _setting(rig, qsid) == value


def test_setting_flags_merge_and_keep_other_bits(tmp_path):
    rig = full(tmp_path)
    firmware(rig).state["settings"]["21"] = 0x40000001  # bit 30 is not in the table
    proposal = plan(rig, SetQmkSettingChange(qsid=21, bit=7, enabled=True), SetQmkSettingChange(qsid=21, bit=0, enabled=False))
    assert len(proposal.operations) == 1 and proposal.operations[0].after == 0x40000080
    assert "Swap Caps Lock and Left Control" in proposal.operations[0].title and "N-key" in proposal.operations[0].title
    assert rig.apply(proposal).outcome == "verified" and _setting(rig, 21) == 0x40000080


def test_setting_plain_boolean(tmp_path):
    rig = full(tmp_path)
    assert rig.apply(plan(rig, SetQmkSettingChange(qsid=22, bit=0, enabled=True))).outcome == "verified"
    assert _setting(rig, 22) == 1


@pytest.mark.parametrize("change, code", [
    (dict(qsid=7, value=10001), "invalid_request"), (dict(qsid=7, value=-1), "invalid_request"),
    (dict(qsid=20, value=101), "invalid_request"), (dict(qsid=7, bit=0, enabled=True), "invalid_request"),
    (dict(qsid=21, bit=10, enabled=True), "invalid_request"), (dict(qsid=21, bit=40, enabled=True), "invalid_request"),
    (dict(qsid=21, value=0x400), "invalid_request"), (dict(qsid=21, value=1 << 32), "invalid_request"),
    (dict(qsid=8, value=256), "invalid_request"), (dict(qsid=22, value=2), "invalid_request"),
    (dict(qsid=7), "invalid_request"), (dict(qsid=7, value=1, bit=0, enabled=True), "invalid_request"),
    (dict(qsid=21, bit=1), "invalid_request"), (dict(qsid=21, enabled=True), "invalid_request"),
    (dict(qsid=999, value=1), "capability_unavailable"),
])
def test_setting_bounds(tmp_path, change, code):
    refused(full(tmp_path), code, SetQmkSettingChange(**change))


def test_setting_twice_is_refused(tmp_path):
    rig = full(tmp_path)
    refused(rig, "invalid_request", SetQmkSettingChange(qsid=7, value=1), SetQmkSettingChange(qsid=7, value=2))
    refused(rig, "invalid_request", SetQmkSettingChange(qsid=21, bit=1, enabled=True), SetQmkSettingChange(qsid=21, bit=1, enabled=False))
    refused(rig, "invalid_request", SetQmkSettingChange(qsid=21, bit=1, enabled=True), SetQmkSettingChange(qsid=21, value=0))
    refused(rig, "invalid_request", SetQmkSettingChange(qsid=21, value=0), SetQmkSettingChange(qsid=21, bit=1, enabled=True))


def test_setting_without_capability(tmp_path):
    assert refused(Rig(tmp_path, "minimal"), "capability_unavailable", SetQmkSettingChange(qsid=7, value=1)).details["family"] == "settings"
    assert refused(Rig(tmp_path / "v", "vintage"), "capability_unavailable", SetQmkSettingChange(qsid=7, value=1)).details["family"] == "settings"


def test_firmware_status_byte_fails_the_write(tmp_path):
    rig = full(tmp_path, Faults(settings_status_fail=(7,)))
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"), SetQmkSettingChange(qsid=7, value=175))
    result = rig.apply(proposal)
    assert result.outcome == "failed_restored" and "status 1" in result.message
    assert _setting(rig, 7) == 0 and rig.keycode(0, 0, 0) == 0x04


def _with_unknown_setting(rig, qsid=60000):
    fw = firmware(rig)
    fw.state["settings"][str(qsid)] = 0x1234


def test_unknown_setting_is_kept_opaque_and_never_written(tmp_path):
    rig = Rig(tmp_path, "split")
    _with_unknown_setting(rig)
    snapshot = rig.service.read_snapshot()
    assert snapshot.configuration.settings.unknown_qsids == [60000]
    assert snapshot.configuration.settings.unknown_raw["60000"].startswith("34120000")
    assert snapshot.coverage["settings"] == "partial_unknown_qsids"
    assert refused(rig, "unsupported_operation", SetQmkSettingChange(qsid=60000, value=1)).details["qsid"] == 60000


def test_unknown_setting_does_not_block_other_writes_and_undo_says_what_it_cannot_do(tmp_path):
    rig = Rig(tmp_path, "split")
    _with_unknown_setting(rig)
    assert rig.apply(rig.plan((1, 0, 2, "KC_A"))).outcome == "verified"
    firmware(rig).state["settings"]["60000"] = 0x4321
    undo_plan = rig.service.create_undo_proposal(None)
    assert any("60000" in w and "cannot restore" in w for w in undo_plan.warnings)
    assert rig.apply(undo_plan).outcome == "verified"
    assert firmware(rig).state["settings"]["60000"] == 0x4321, "left alone, as the proposal said"


def test_unstable_unknown_answer_does_not_make_proposals_stale(tmp_path):
    rig = Rig(tmp_path, "split")
    _with_unknown_setting(rig)
    proposal = rig.plan((1, 0, 2, "KC_A"))
    firmware(rig).state["settings"]["60000"] = 0x9999
    assert rig.apply(proposal).outcome == "verified"


def test_unreadable_setting_is_reported_and_refused(tmp_path):
    rig = full(tmp_path, Faults(settings_get_fail=(7,)))
    snapshot = rig.service.read_snapshot()
    assert snapshot.configuration.settings.unreadable_qsids == [7]
    assert snapshot.coverage["settings"] == "partial_unreadable_qsids"
    assert any("refused to report" in w for w in snapshot.warnings)
    assert refused(rig, "unsupported_operation", SetQmkSettingChange(qsid=7, value=1)).details["qsid"] == 7
