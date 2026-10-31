# SPDX-License-Identifier: GPL-2.0-or-later
"""Regressions for the independent review of M3. Each test is a scenario that went wrong."""
import dataclasses
import json
import struct

import pytest

from conftest import Rig
from vialtic import cli, proposals, vial_adapter as va
from vialtic.emulator import Faults
from vialtic.errors import VialticError
from vialtic.models import (
    ChangeSet, SetKeyChange, SetLayoutOptionChange, SetLightingChange, SetMacroChange, SetQmkSettingChange, SetTapDanceChange,
)
from vialtic.proposals import proposal_id
from vialtic.service import attestation_for


def plan(rig, *changes):
    return rig.service.create_proposal(None, ChangeSet(changes=list(changes)))


def firmware(rig, index=0):
    return rig.backend.firmware(index)


# Findings 1: firmware ignores everything but a few commands while an unlock is in progress.

def _started_unlock(rig):
    proposal = plan(rig, SetMacroChange(index=0, actions=[["text", "x"]]))
    status = rig.service.unlock_start(proposal.id, rig.approve(proposal), None)
    assert status.in_progress
    return proposal


def test_commands_during_an_unlock_say_so_instead_of_blaming_the_protocol(tmp_path):
    rig = Rig(tmp_path, "full")
    proposal = _started_unlock(rig)
    for call in (lambda: rig.service.read_snapshot(), lambda: plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z")),
                 lambda: rig.service.lock(None), lambda: rig.apply(proposal)):
        with pytest.raises(VialticError) as error:
            call()
        assert error.value.code == "unlock_in_progress" and "replug" in error.value.next_steps[0]
    entry = rig.service.list_devices().devices[0]
    assert entry.status == "busy" and entry.problem.code == "unlock_in_progress"
    assert rig.write_packets() == 0


def test_unlock_can_be_polled_to_completion_and_the_proposal_still_applies(tmp_path):
    rig = Rig(tmp_path, "full")
    proposal = _started_unlock(rig)
    assert rig.service.unlock_status(None).in_progress
    for _ in range(5):
        status = rig.service.unlock_poll(None)
    assert status.unlocked and rig.service.unlock_status(None).unlocked
    result = rig.apply(proposal)
    assert result.outcome == "verified" and result.relocked is True


def test_timed_out_unlock_says_a_replug_is_needed_and_a_replug_recovers(make_rig):
    rig = make_rig("full", faults=Faults(unlock_never=True), unlock_timeout=0.02)
    proposal = plan(rig, SetMacroChange(index=0, actions=[["text", "x"]]))
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "unlock_timeout" and "Replug" in error.value.next_steps[0]
    assert firmware(rig).state["unlock_in_progress"], "the host cannot end it"
    firmware(rig).power_cycle()
    firmware(rig).unlock_never = False
    assert rig.apply(proposal).outcome == "verified"


def test_lock_packet_is_really_ignored_during_an_unlock(tmp_path):
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    fw.state["unlock_in_progress"] = True
    fw.handle(bytes([va.proto.CMD_VIA_VIAL_PREFIX, va.proto.CMD_VIAL_LOCK]))
    assert fw.state["unlock_in_progress"]
    assert fw.handle(bytes([va.proto.CMD_VIA_GET_PROTOCOL_VERSION]))[1:3] == b"\x00\x00"


# Finding 2: a firmware update must not leave a keyboard with no way forward.

def test_recovery_across_a_protocol_change_says_how_to_continue(make_rig):
    rig = make_rig(faults=Faults(disconnect_after_writes=0))
    proposal = rig.plan((1, 0, 2, "KC_A"))
    assert rig.apply(proposal).outcome == "unknown"
    restarted = rig.restart()
    fw = firmware(restarted)
    fw.spec = dataclasses.replace(fw.spec, vial_protocol=5)
    binding = proposal.device.binding_id
    with pytest.raises(VialticError) as error:
        restarted.service.create_recovery_proposal(None)
    path = restarted.store.pending_path(binding)
    assert error.value.code == "device_changed" and str(path) in error.value.next_steps[0]
    assert path.exists()
    # Following that advice works, and nothing is lost that was not moved by the user.
    path.rename(tmp := path.with_suffix(".aside"))
    assert tmp.exists() and restarted.service.read_snapshot()
    assert restarted.plan((1, 0, 3, "KC_B")).operations


def test_undo_refuses_across_the_macro_format_change(make_rig):
    rig = make_rig("vintage")
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    fw = firmware(rig)
    fw.spec = dataclasses.replace(fw.spec, vial_protocol=2)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "device_changed"


@pytest.mark.parametrize("first, second, same", [(6, 6, True), (5, 6, False), (4, 5, False), (1, 2, False), (2, 4, True)])
def test_encoding_boundaries(first, second, same):
    assert va.same_encoding(first, second) is same


# Findings 3 and 7: restoring must not save lighting nobody saved, nor claim a save it did not make.

def test_failed_apply_does_not_save_lighting_it_never_saved(tmp_path):
    rig = Rig(tmp_path, "full", Faults(settings_status_fail=(7,)))
    fw = firmware(rig)
    fw.state["lighting"]["v"] = 10  # transient, the saved value is 64
    result = rig.apply(plan(rig, SetLightingChange(system="vialrgb", save=True, value=99), SetQmkSettingChange(qsid=7, value=1)))
    assert result.outcome == "failed_restored"
    assert fw.state["lighting"]["v"] == 10 and fw.state["lighting_saved"]["v"] == 64
    fw.power_cycle()
    assert fw.state["lighting"]["v"] == 64


def _losing_save_answers(rig, count):
    """The save reaches the firmware, then the keyboard fails to answer `count` times."""
    fw = firmware(rig)
    seen = []

    def hook(msg):
        if msg[0] == va.proto.CMD_VIA_LIGHTING_SAVE and len(seen) < count:
            seen.append(msg)
            if len(seen) == 1:
                fw.state["lighting_saved"] = dict(fw.state["lighting"])
            raise OSError("device disconnected")

    rig.backend.packet_hook = hook
    return seen


def test_save_answer_lost_restores_and_saves_again(tmp_path):
    rig = Rig(tmp_path, "full")
    _losing_save_answers(rig, 1)
    result = rig.apply(plan(rig, SetLightingChange(system="vialrgb", save=True, value=99)))
    assert result.outcome == "failed_restored"
    assert firmware(rig).state["lighting_saved"]["v"] == 64


def test_restore_whose_own_save_fails_is_not_reported_as_restored(tmp_path):
    rig = Rig(tmp_path, "full", reconnect_window=0.05)
    seen = _losing_save_answers(rig, 10_000)
    proposal = plan(rig, SetLightingChange(system="vialrgb", save=True, value=99))
    result = rig.apply(proposal)
    assert len(seen) > 1 and result.outcome == "partial"
    assert firmware(rig).state["lighting_saved"]["v"] == 99, "still the rejected value, and it was said so"
    assert rig.store.pending_path(proposal.device.binding_id).exists()


def test_save_retried_on_a_later_pass_still_counts(tmp_path):
    rig = Rig(tmp_path, "full", reconnect_window=2.0)
    _losing_save_answers(rig, 2)
    assert rig.apply(plan(rig, SetLightingChange(system="vialrgb", save=True, value=99))).outcome == "failed_restored"
    assert firmware(rig).state["lighting_saved"]["v"] == 64


def test_recovery_saves_only_if_the_interrupted_write_got_that_far(tmp_path):
    class Crash(BaseException):
        pass

    rig = Rig(tmp_path, "full", Faults())
    fw = firmware(rig)
    fw.state["lighting"]["v"] = 10  # transient
    proposal = plan(rig, SetLightingChange(system="vialrgb", save=True, value=99))
    original = rig.service._differences

    def dies_before_the_save(*args):
        raise Crash

    rig.service._differences = dies_before_the_save
    with pytest.raises(Crash):
        rig.apply(proposal)
    rig.service._differences = original
    restarted = rig.restart()
    recovery = restarted.service.create_recovery_proposal(None)
    assert recovery.operations[0].save is False and "lighting" in recovery.affected
    assert restarted.apply(recovery).outcome == "verified"
    assert firmware(restarted).state["lighting_saved"]["v"] == 64


# Finding 4: backlight brightness is a level in firmware.

@pytest.mark.parametrize("levels", [1, 2, 3, 4, 5, 7, 10, 15, 17])
def test_every_reported_backlight_value_can_be_written_back(tmp_path, levels):
    for level in range(levels + 1):
        rig = Rig(tmp_path / f"{levels}-{level}", "legacy")
        fw = firmware(rig)
        fw.spec = dataclasses.replace(fw.spec, backlight_levels=levels)
        target = level * 255 // levels
        start = 255 if target != 255 else 0
        fw.state["lighting"]["bl_brightness"] = start
        fw.state["lighting_saved"]["bl_brightness"] = start
        result = rig.apply(plan(rig, SetLightingChange(system="backlight", save=False, brightness=target)))
        assert result.outcome == "verified", (levels, level, result.differences)
        assert fw.state["lighting"]["bl_brightness"] == target
        undo = rig.service.create_undo_proposal(None)
        assert rig.apply(undo).outcome == "verified"
        assert fw.state["lighting"]["bl_brightness"] == start


def test_value_between_backlight_levels_fails_honestly_and_restores(tmp_path):
    rig = Rig(tmp_path, "legacy")
    fw = firmware(rig)
    fw.spec = dataclasses.replace(fw.spec, backlight_levels=4)
    fw.state["lighting"]["bl_brightness"] = 63
    result = rig.apply(plan(rig, SetLightingChange(system="backlight", save=False, brightness=130)))
    assert result.outcome == "failed_restored" and fw.state["lighting"]["bl_brightness"] == 63
    assert result.differences[0].observed["brightness"] == 127, "the reported value is what to ask for instead"
    assert rig.apply(plan(rig, SetLightingChange(system="backlight", save=False, brightness=127))).outcome == "verified"


# Finding 5: --output belongs to the user.

def test_snapshot_output_leaves_similarly_named_files_alone(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    work = tmp_path / "work"
    work.mkdir()
    (work / ".notes.json.swp").write_text("theirs")
    (work / ".notes.json.bak").write_text("theirs")
    assert cli.main(["snapshot", "--output", str(work / "notes.json"), "--emulator", "split"]) == 0
    assert (work / ".notes.json.swp").exists() and (work / ".notes.json.bak").exists()


# Finding 6: a backup may belong to an identical unit.

def _twin_rig(tmp_path):
    rig = Rig(tmp_path, "twins")
    first = rig.service.list_devices().devices[0].identity.device_id
    assert rig.apply(rig.service.create_proposal(first, ChangeSet(changes=[
        SetKeyChange(layer=1, row=0, col=2, keycode="KC_A")])), first).outcome == "verified"
    return rig


def _only_second_unit(rig):
    """The first unit is unplugged and the identical second one stays."""
    original = rig.backend.enumerate
    rig.backend.enumerate = lambda: original()[1:]


def test_undo_on_an_identical_unit_needs_an_explicit_choice(tmp_path):
    rig = _twin_rig(tmp_path)
    rig.external_set(1, 0, 3, 0x1D, device=1)  # the second unit has its own settings
    _only_second_unit(rig)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "device_ambiguous" and "--device" in error.value.next_steps[0]
    device = rig.service.list_devices().devices[0].identity.device_id
    chosen = rig.service.create_undo_proposal(device)
    assert any("selected explicitly" in w for w in chosen.warnings)


def test_recovery_on_an_identical_unit_needs_an_explicit_choice(tmp_path):
    rig = Rig(tmp_path, "twins", Faults(disconnect_after_writes=0))
    first = rig.service.list_devices().devices[0].identity.device_id
    proposal = rig.service.create_proposal(first, ChangeSet(changes=[SetKeyChange(layer=1, row=0, col=2, keycode="KC_A")]))
    rig.apply(proposal, first)
    restarted = rig.restart()
    _only_second_unit(restarted)
    with pytest.raises(VialticError) as error:
        restarted.service.create_recovery_proposal(None)
    assert error.value.code == "device_ambiguous"


# Finding 8: RGB light effect 0 means off.

def test_turning_rgb_light_off_and_undoing_it(tmp_path):
    rig = Rig(tmp_path, "split")
    fw = firmware(rig)
    fw.state["lighting"]["rgb_effect"] = 5
    assert rig.apply(plan(rig, SetLightingChange(system="rgblight", save=False, effect=0))).outcome == "verified"
    assert fw.state["lighting"]["rgb_enabled"] is False
    assert rig.apply(rig.service.create_undo_proposal(None)).outcome == "verified"
    assert fw.state["lighting"]["rgb_enabled"] is True and fw.state["lighting"]["rgb_effect"] == 5


def test_rgb_effect_and_colour_from_off_in_one_write(tmp_path):
    rig = Rig(tmp_path, "split")
    fw = firmware(rig)
    fw.state["lighting"]["rgb_enabled"] = False
    result = rig.apply(plan(rig, SetLightingChange(system="rgblight", save=False, effect=7, hue=99, brightness=50)))
    assert result.outcome == "verified" and fw.state["lighting"]["rgb_effect"] == 7 and fw.state["lighting"]["rgb_h"] == 99


def test_colour_and_off_in_one_write(tmp_path):
    rig = Rig(tmp_path, "split")
    result = rig.apply(plan(rig, SetLightingChange(system="rgblight", save=False, effect=0, hue=99)))
    assert result.outcome == "verified" and firmware(rig).state["lighting"]["rgb_h"] == 99


def test_colour_on_light_that_stays_off_fails_read_back(tmp_path):
    rig = Rig(tmp_path, "split")
    firmware(rig).state["lighting"]["rgb_enabled"] = False
    assert rig.apply(plan(rig, SetLightingChange(system="rgblight", save=False, hue=99))).outcome != "verified"


# Finding 9: an unlock from earlier must be visible in everything that writes.

def test_plan_and_apply_report_an_unlock_left_over_from_another_proposal(tmp_path):
    rig = Rig(tmp_path, "full")
    earlier = plan(rig, SetMacroChange(index=0, actions=[["text", "x"]]))
    rig.service.unlock_start(earlier.id, rig.approve(earlier), None)
    for _ in range(5):
        rig.service.unlock_poll(None)
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    assert any("has not locked it again" in w for w in proposal.warnings)
    result = rig.apply(proposal)
    assert result.outcome == "verified" and any("has not locked it again" in w for w in result.warnings)


# Finding 10: only the lighting system the reversed write touched.

def test_undo_leaves_the_other_lighting_system_alone(tmp_path):
    rig = Rig(tmp_path, "dual")
    assert rig.apply(plan(rig, SetLightingChange(system="rgblight", save=True, brightness=9))).outcome == "verified"
    fw = firmware(rig)
    fw.state["lighting"]["bl_effect"] = 1  # a backlight key
    undo = rig.service.create_undo_proposal(None)
    assert [op.system for op in undo.operations] == ["rgblight"]
    assert any("Lighting differs" in w for w in undo.warnings)
    assert rig.apply(undo).outcome == "verified" and fw.state["lighting"]["bl_effect"] == 1


def test_other_lighting_system_moving_during_a_write_is_not_a_failure(tmp_path):
    rig = Rig(tmp_path, "dual")
    proposal = plan(rig, SetLightingChange(system="rgblight", save=False, brightness=9))
    original = rig.service._write_operation

    def key_press(session, op):
        original(session, op)
        firmware(rig).state["lighting"]["bl_effect"] = 1

    rig.service._write_operation = staticmethod(key_press)
    assert rig.apply(proposal).outcome == "verified"


# Finding 11: single use.

def test_proposal_cannot_be_applied_again_after_it_failed(tmp_path):
    rig = Rig(tmp_path, "full", Faults(settings_status_fail=(7,)))
    proposal = plan(rig, SetQmkSettingChange(qsid=7, value=1))
    assert rig.apply(proposal).outcome == "failed_restored"
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_not_found"


# Findings 12 and 13: saved and current lighting are separate things.

def test_transient_lighting_can_be_made_permanent(tmp_path):
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    fw.state["lighting"]["v"] = 10
    proposal = plan(rig, SetLightingChange(system="vialrgb", save=True))
    assert "no value changes" in proposals.describe_operation(proposal.operations[0])
    assert rig.apply(proposal).outcome == "verified" and fw.state["lighting_saved"]["v"] == 10


def test_undo_of_a_save_still_saves_when_a_key_put_the_value_back(tmp_path):
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    assert rig.apply(plan(rig, SetLightingChange(system="vialrgb", save=True, value=99))).outcome == "verified"
    fw.state["lighting"]["v"] = 64
    undo = rig.service.create_undo_proposal(None)
    assert rig.apply(undo).outcome == "verified" and fw.state["lighting_saved"]["v"] == 64


# Finding 14: a stored file can be edited and its id recomputed.

def _rewrite(rig, proposal, edit):
    path = rig.store.root / "proposals" / f"{proposal.id}.json"
    data = json.loads(path.read_text())
    edit(data)
    data.pop("id")
    new_id = proposal_id(data)
    data["id"] = new_id
    (rig.store.root / "proposals" / f"{new_id}.json").write_text(json.dumps(data))
    return new_id


@pytest.mark.parametrize("edit", [
    lambda data: data["baseline"].update(checked_families=[]),
    lambda data: data.update(affected=["combos"]),
])
def test_edited_proposal_with_a_recomputed_id_is_refused(tmp_path, edit):
    rig = Rig(tmp_path, "full")
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    with pytest.raises(VialticError) as error:
        rig.service.get_proposal(_rewrite(rig, proposal, edit))
    assert error.value.code == "proposal_tampered"


def test_edited_macro_preview_or_label_is_refused_before_writing(tmp_path):
    rig = Rig(tmp_path, "full")
    proposal = plan(rig, SetMacroChange(index=0, actions=[["text", "safe"]]))

    def lie(data):
        data["operations"][0]["changed"][0]["after"] = [["text", "harmless"]]

    new_id = _rewrite(rig, proposal, lie)
    stored = rig.service.get_proposal(new_id)
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(new_id, attestation_for(stored, rig.options.now()))
    assert error.value.code == "proposal_tampered" and rig.write_packets() == 0

    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    new_id = _rewrite(rig, proposal, lambda data: data["operations"][0]["after"].update(label="harmless"))
    stored = rig.service.get_proposal(new_id)
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(new_id, attestation_for(stored, rig.options.now()))
    assert error.value.code == "proposal_stale" and rig.write_packets() == 0


@pytest.mark.parametrize("edit", [
    lambda c: c.update(tap_dance=[["KC_A"]] * len(c["tap_dance"])),
    lambda c: c["tap_dance"][0].__setitem__(0, "open('x')"),
    lambda c: c["macros"].update(buffer_b64="!!!!"),
])
def test_malformed_backup_is_a_clear_refusal_and_is_kept(tmp_path, edit):
    rig = Rig(tmp_path, "full")
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    rig.apply(proposal)
    path = rig.store.previous_path(proposal.device.binding_id)
    data = json.loads(path.read_text())
    edit(data["configuration"])
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "invalid_file" and str(path) in error.value.next_steps[0]
    assert path.read_bytes() == original


# Finding 16: what a macro write leaves behind.

def test_clearing_a_macro_leaves_no_text_in_keyboard_memory(tmp_path):
    rig = Rig(tmp_path, "full")
    assert rig.apply(plan(rig, SetMacroChange(index=0, actions=[["text", "SECRETPASS"]]))).outcome == "verified"
    assert rig.apply(plan(rig, SetMacroChange(index=0, actions=[]))).outcome == "verified"
    assert b"ETPASS" not in bytes(firmware(rig).macro)


def test_stray_last_byte_does_not_survive_an_edit(tmp_path):
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    fw.macro[-1] = 0x41
    fw.sync()
    assert rig.apply(plan(rig, SetMacroChange(index=0, actions=[["text", "a"]]))).outcome == "verified"
    assert fw.macro[-1] == 0


def test_capacity_message_counts_terminators(tmp_path):
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    fw.macro[:] = b"a\x00" + b"z" * 62
    with pytest.raises(VialticError) as error:
        plan(rig, SetMacroChange(index=0, actions=[["text", "b"]]))
    assert error.value.code == "capacity_exceeded" and "terminator" in error.value.message


# Emulator gaps the review named.

def test_second_encoder_and_custom_keycode(tmp_path):
    rig = Rig(tmp_path, "full")
    snapshot = rig.service.read_snapshot()
    assert snapshot.capabilities.encoder_count == 2 and snapshot.capabilities.custom_keycode_count == 1
    result = rig.apply(plan(rig, SetTapDanceChange(index=0, on_tap="USER00")))
    assert result.outcome == "verified" and rig.service.read_snapshot().configuration.tap_dance[0][0] == "USER00"


def test_reconnect_window_option_applies_to_an_existing_proposal(tmp_path):
    rig = Rig(tmp_path, "full", reconnect_window=30.0)
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    short = Rig(tmp_path, "full", Faults(disconnect_after_writes=0), reconnect_window=0.05)
    assert short.apply(proposal).outcome == "unknown"


# Second review.

def test_backup_of_a_connected_twin_is_never_applied_to_the_other(tmp_path):
    rig = Rig(tmp_path, "twins")
    devices = [e.identity.device_id for e in rig.service.list_devices().devices]
    proposal = rig.service.create_proposal(devices[0], ChangeSet(changes=[SetKeyChange(layer=1, row=0, col=2, keycode="KC_A")]))
    assert rig.apply(proposal, devices[0]).outcome == "verified"
    rig.external_set(1, 0, 3, 0x1D, device=1)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(devices[1])
    assert error.value.code == "device_ambiguous" and devices[0] in error.value.next_steps[0]
    assert rig.service.create_undo_proposal(devices[0]).operations


def test_recovery_of_a_connected_twin_keeps_the_backup_for_its_owner(tmp_path):
    rig = Rig(tmp_path, "twins", Faults(disconnect_after_writes=1), reconnect_window=0.02)
    devices = [e.identity.device_id for e in rig.service.list_devices().devices]
    proposal = rig.service.create_proposal(devices[0], ChangeSet(changes=[
        SetKeyChange(layer=1, row=0, col=2, keycode="KC_A"), SetKeyChange(layer=1, row=0, col=3, keycode="KC_B")]))
    rig.apply(proposal, devices[0])
    restarted = rig.restart()
    with pytest.raises(VialticError) as error:
        restarted.service.create_recovery_proposal(devices[1])
    assert error.value.code == "device_ambiguous"
    assert restarted.store.pending_path(proposal.device.binding_id).exists()
    assert restarted.apply(restarted.service.create_recovery_proposal(devices[0]), devices[0]).outcome == "verified"


def _saved_then_disconnected(tmp_path):
    rig = Rig(tmp_path, "full", reconnect_window=0.02)
    proposal = plan(rig, SetLightingChange(system="vialrgb", save=True, hue=200))
    fw = firmware(rig)

    def hook(msg):
        if msg[0] == va.proto.CMD_VIA_LIGHTING_SAVE:
            fw.state["lighting_saved"] = dict(fw.state["lighting"])
            rig.backend.disconnect()
            raise OSError("device disconnected")

    rig.backend.packet_hook = hook
    assert rig.apply(proposal).outcome == "unknown"
    return rig


def test_a_failed_recovery_remembers_the_firmware_saved_the_bad_lighting(tmp_path):
    rig = _saved_then_disconnected(tmp_path)
    second = rig.restart(Faults(disconnect_after_writes=0))
    recovery = second.service.create_recovery_proposal(None)
    assert recovery.operations[0].save is True
    assert second.apply(recovery).outcome == "unknown"
    third = rig.restart()
    again = third.service.create_recovery_proposal(None)
    assert again.operations[0].save is True, "the fact must survive the failed attempt"
    assert third.apply(again).outcome == "verified"
    fw = firmware(third)
    assert fw.state["lighting_saved"]["h"] == 16
    fw.power_cycle()
    assert fw.state["lighting"]["h"] == 16


def test_a_glitch_during_recovery_does_not_claim_restored_without_the_save(tmp_path):
    rig = _saved_then_disconnected(tmp_path)
    second = rig.restart()
    recovery = second.service.create_recovery_proposal(None)
    fw = firmware(second)
    glitched = []

    def glitch(msg):
        if msg[0] == va.proto.CMD_VIA_LIGHTING_SET_VALUE and not glitched:
            glitched.append(msg)
            raise OSError("glitch")

    second.backend.packet_hook = glitch
    assert second.apply(recovery).outcome == "failed_restored"
    assert fw.state["lighting_saved"]["h"] == 16


@pytest.mark.parametrize("start, change, expect", [
    (1, dict(effect=3, brightness=50), ("rgb_effect", 3, "rgb_brightness", 50)),
    (3, dict(effect=1, brightness=50), ("rgb_effect", 1, "rgb_brightness", 50)),
    (3, dict(brightness=50), ("rgb_effect", 3, "rgb_brightness", 50)),
    (7, dict(effect=1, hue=99), ("rgb_effect", 1, "rgb_h", 99)),
    (7, dict(hue=99), ("rgb_effect", 7, "rgb_h", 99)),
])
def test_rgb_values_firmware_ignores_in_some_effects_still_land_and_undo(tmp_path, start, change, expect):
    rig = Rig(tmp_path, "split")
    fw = firmware(rig)
    fw.state["lighting"]["rgb_effect"] = start
    before = dict(fw.state["lighting"])
    result = rig.apply(plan(rig, SetLightingChange(system="rgblight", save=False, **change)))
    assert result.outcome == "verified", result.differences
    assert fw.state["lighting"][expect[0]] == expect[1] and fw.state["lighting"][expect[2]] == expect[3]
    assert rig.apply(rig.service.create_undo_proposal(None)).outcome == "verified"
    assert {k: fw.state["lighting"][k] for k in before} == before


def test_timed_out_unlock_keeps_its_marker_so_a_later_unlock_is_reported(make_rig):
    rig = make_rig("full", faults=Faults(unlock_never=True), unlock_timeout=0.02)
    proposal = plan(rig, SetMacroChange(index=0, actions=[["text", "x"]]))
    with pytest.raises(VialticError):
        rig.apply(proposal)
    assert rig.store.read_unlock_marker(proposal.device.binding_id) == proposal.id
    fw = firmware(rig)
    fw.unlock_never = False
    for _ in range(5):
        rig.service.unlock_poll(None)
    assert fw.state["unlocked"]
    assert any("has not locked" in w for w in rig.service.inspect_device().warnings)
    result = rig.apply(proposal)
    assert result.outcome == "verified" and result.relocked is True and not fw.state["unlocked"]


def test_identical_locked_keyboard_does_not_clear_the_other_ones_marker(tmp_path):
    rig = Rig(tmp_path, "twins")
    devices = [e.identity.device_id for e in rig.service.list_devices().devices]
    proposal = rig.service.create_proposal(devices[0], ChangeSet(changes=[SetKeyChange(layer=1, row=0, col=2, keycode="QK_BOOT")]))
    rig.service.unlock_start(proposal.id, rig.approve(proposal), devices[0])
    for _ in range(5):
        rig.service.unlock_poll(devices[0])
    assert rig.service.inspect_device(devices[1]).warnings == []
    assert any("has not locked" in w for w in rig.service.inspect_device(devices[0]).warnings)
    assert rig.apply(proposal, devices[0]).relocked is True


def test_recovery_spends_the_proposal_it_recovers_from(make_rig):
    class Crash(BaseException):
        pass

    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "KC_A"), (1, 0, 3, "KC_B"))
    original = rig.service._write_operation

    def dies(session, op):
        original(session, op)
        raise Crash

    rig.service._write_operation = staticmethod(dies)
    with pytest.raises(Crash):
        rig.apply(proposal)
    restarted = rig.restart()
    assert restarted.apply(restarted.service.create_recovery_proposal(None)).outcome == "verified"
    with pytest.raises(VialticError) as error:
        restarted.apply(proposal)
    assert error.value.code == "proposal_not_found"


@pytest.mark.parametrize("text", ["LT(1,LCTL(KC_A))", "LT1(LSFT(KC_B))", "MT(MOD_LCTL,LSFT(KC_A))", "LSFT(MO(1))", "LSFT(TD(1))",
                                  "LT(0,MO(1))", "MT(0x3F,KC_A)", "LSFT_T(KC_EXLM)", "LCTL(0x5100)"])
def test_keycode_arguments_that_do_not_fit_are_refused(tmp_path, text):
    rig = Rig(tmp_path, "full")
    with pytest.raises(VialticError) as error:
        plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode=text))
    assert error.value.code == "invalid_keycode"


@pytest.mark.parametrize("text, name", [("LCTL(LSFT(KC_A))", "C_S(KC_A)"), ("LCTL(KC_EXLM)", None), ("LT(1, KC_A)", "LT1(KC_A)"),
                                        ("MT(MOD_LCTL | MOD_LSFT, KC_A)", None), ("LSFT_T(KC_F)", "LSFT_T(KC_F)")])
def test_keycode_arguments_that_fit_still_work(tmp_path, text, name):
    rig = Rig(tmp_path, "full")
    op = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode=text)).operations[0]
    assert name is None or op.after.keycode == name


def test_recovery_leaves_a_family_it_said_it_would_leave(make_rig):
    class Crash(BaseException):
        pass

    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "KC_A"), (1, 0, 3, "KC_B"))
    original = rig.service._write_operation

    def dies(session, op):
        original(session, op)
        raise Crash

    rig.service._write_operation = staticmethod(dies)
    with pytest.raises(Crash):
        rig.apply(proposal)
    restarted = rig.restart()
    fw = firmware(restarted)
    fw.spec = dataclasses.replace(fw.spec, layers=fw.spec.layers - 1)
    recovery = restarted.service.create_recovery_proposal(None)
    assert "keymap" not in recovery.affected and any("keymap" in w and "stay as they are" in w for w in recovery.warnings)
    assert restarted.apply(recovery).outcome == "verified"
    assert not restarted.store.has_unresolved_write(proposal.device.binding_id)


def test_dropped_tail_of_a_macro_write_is_not_verified(tmp_path):
    rig = Rig(tmp_path, "generic")
    assert rig.apply(plan(rig, SetMacroChange(index=0, actions=[["text", "x" * 200]]))).outcome == "verified"
    packets = []
    fw = firmware(rig)
    original = fw.handle

    def drops_after_two(msg):
        if msg[0] == va.proto.CMD_VIA_MACRO_SET_BUFFER:
            packets.append(msg)
            if len(packets) > 2:
                return msg.ljust(va.MSG_LEN, b"\x00")
        return original(msg)

    fw.handle = drops_after_two
    result = rig.apply(plan(rig, SetMacroChange(index=0, actions=[])))
    assert result.outcome != "verified" and "macro memory does not hold" in result.message


def test_replug_during_a_write_says_lighting_may_differ(tmp_path):
    rig = Rig(tmp_path, "full", Faults(disconnect_after_writes=0, reconnect_after_seconds=0.0), reconnect_window=2.0)
    fw = firmware(rig)
    fw.state["lighting"]["v"] = 77  # transient
    result = rig.apply(plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z")))
    assert result.outcome == "failed_restored" and "except lighting" in result.message


@pytest.mark.parametrize("edit", [
    lambda c: c["tap_dance"][0].__setitem__(4, 70000),
    lambda c: c["settings"]["values"].__setitem__("7", 70000),
    lambda c: c.update(layout_options=-5),
])
def test_damaged_pending_file_is_an_apply_outcome_not_a_crash(make_rig, edit):
    class Crash(BaseException):
        pass

    rig = make_rig("full")
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"), SetKeyChange(layer=0, row=1, col=0, keycode="KC_Y"))
    original = rig.service._write_operation

    def dies(session, op):
        original(session, op)
        raise Crash

    rig.service._write_operation = staticmethod(dies)
    with pytest.raises(Crash):
        rig.apply(proposal)
    path = rig.store.pending_path(proposal.device.binding_id)
    data = json.loads(path.read_text())
    edit(data["configuration"])
    path.write_text(json.dumps(data))
    restarted = rig.restart()
    recovery = restarted.service.create_recovery_proposal(None)
    assert restarted.apply(recovery).outcome in ("partial", "unknown")
    assert path.exists()


def test_backup_with_no_protocol_is_a_clear_refusal(tmp_path):
    rig = Rig(tmp_path, "full")
    proposal = plan(rig, SetKeyChange(layer=0, row=0, col=0, keycode="KC_Z"))
    rig.apply(proposal)
    path = rig.store.previous_path(proposal.device.binding_id)
    data = json.loads(path.read_text())
    data["device"]["vial_protocol"] = None
    path.write_text(json.dumps(data))
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "invalid_file"


def test_undo_text_shows_layout_choices(tmp_path):
    rig = Rig(tmp_path, "full")
    rig.apply(plan(rig, SetLayoutOptionChange(index=1, choice=2)))
    undo = rig.service.create_undo_proposal(None)
    assert proposals.describe_operation(undo.operations[0]) == "layout options: 2 [0, 2] -> 0 [0, 0]"


def test_default_output_has_no_hash_of_macro_contents(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.main(["snapshot", "--emulator", "full", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)["result"]
    assert snapshot["family_fingerprints"]["macros"] == "redacted"
    assert cli.main(["plan", "--emulator", "full", "--set", "0:0,0=KC_Z", "--json"]) == 0
    proposal = json.loads(capsys.readouterr().out)["result"]
    assert proposal["baseline"]["family_fingerprints"]["macros"] == "redacted"


@pytest.mark.parametrize("change", [
    {"type": "set_key", "layer": 0, "row": True, "col": 0, "keycode": "KC_A"},
    {"type": "set_key", "layer": "0", "row": 0, "col": 0, "keycode": "KC_A"},
    {"type": "set_tap_dance", "index": 0, "tapping_term": True},
    {"type": "set_key_override", "index": 0, "options": 135.0},
    {"type": "set_qmk_setting", "qsid": 7, "value": True},
])
def test_change_files_do_not_coerce_types(tmp_path, capsys, monkeypatch, change):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"schema_version": 1, "changes": [change]}))
    code = cli.main(["plan", "--emulator", "full", "--changes", str(path), "--json"])
    assert code != 0 and json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"


def test_firmware_that_never_ends_a_settings_query_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("vialtic.device.MAX_READS_PER_SESSION", 3000)
    rig = Rig(tmp_path, "full")
    fw = firmware(rig)
    original = fw._vial

    def echoes_the_query(msg, resp):
        if msg[1] != va.proto.CMD_VIAL_QMK_SETTINGS_QUERY:
            original(msg, resp)

    fw._vial = echoes_the_query
    with pytest.raises(VialticError) as error:
        rig.service.read_snapshot()
    assert error.value.code == "malformed_device_data"
