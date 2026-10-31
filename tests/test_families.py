# SPDX-License-Identifier: GPL-2.0-or-later
"""M3 gate: each family is read, validated, proposed, applied, verified, backed up and restored."""
import json
import stat

import pytest

from conftest import Rig
from vialtic import proposals
from vialtic.emulator import Faults
from vialtic.errors import VialticError
from vialtic.models import ChangeSet, SetKeyChange, SetLayoutOptionChange, SetTapDanceChange
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
