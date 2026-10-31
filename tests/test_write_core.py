# SPDX-License-Identifier: GPL-2.0-or-later
"""M2 gate: no writes before approval, no stale or unsupported writes, backups survive
failures, and incomplete state is never reported as success."""
import dataclasses
import json
import os
import stat
from datetime import timedelta

import pytest

from conftest import KC_A, KC_B, Rig
from vialtic import proposals
from vialtic.emulator import Faults
from vialtic.errors import VialticError
from vialtic.models import ChangeSet, Proposal, SetEncoderChange, SetKeyChange, Snapshot
from vialtic.service import attestation_for
from vialtic.storage import read_model


KC_3 = 0x20
LGUI_3 = 0x0820
ORIGINAL = 0x20  # layer 1, key 0,2 starts as KC_3


def code(rig, layer=1, row=0, col=2):
    return rig.keycode(layer, row, col)


# Proposals.

def test_plan_writes_nothing_and_reports_before_and_after(rig):
    proposal = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    assert rig.write_packets() == 0 and code(rig) == ORIGINAL
    op = proposal.operations[0]
    assert (op.before.keycode, op.before.raw) == ("KC_3", KC_3)
    assert (op.after.keycode, op.after.raw) == ("LGUI(KC_3)", LGUI_3)
    assert proposal.affected == ["keymap"] and proposal.proposal_type == "change"
    assert proposal.expires_at - proposal.created_at == timedelta(minutes=30)
    assert proposal.id.startswith("pr-")
    assert "restore" in proposal.recovery


def test_apply_without_approval_is_refused(rig):
    proposal = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(proposal.id, None)
    assert error.value.code == "approval_required"
    assert rig.write_packets() == 0 and code(rig) == ORIGINAL
    assert not rig.store.pending_path(proposal.device.binding_id).exists()


def test_approval_of_another_proposal_does_not_count(rig):
    first = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    second = rig.plan((1, 0, 3, "KC_A"))
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(second.id, attestation_for(first, rig.options.now()))
    assert error.value.code == "approval_required" and rig.write_packets() == 0


def test_approval_for_another_keyboard_does_not_count(make_rig):
    rig = make_rig("multi")
    generic, split = rig.service.list_devices().devices
    proposal = rig.plan((1, 0, 2, "KC_A"), selector=split.identity.device_id)
    forged = rig.approve(proposal).model_copy(update={"binding_id": generic.identity.binding_id})
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(proposal.id, forged)
    assert error.value.code == "approval_required" and rig.write_packets() == 0


def test_proposal_id_is_content_derived_and_tampering_is_detected(rig):
    proposal = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    path = rig.store._proposal_path(proposal.id)
    data = json.loads(path.read_text())
    data["operations"][0]["after"].update(raw=KC_A, keycode="KC_A", label="A")
    path.write_text(json.dumps(data))
    with pytest.raises(VialticError) as error:
        rig.apply(Proposal.model_validate(data))
    assert error.value.code == "proposal_tampered" and rig.write_packets() == 0


def test_newer_or_malformed_proposal_files_are_refused(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    path = rig.store._proposal_path(proposal.id)
    data = json.loads(path.read_text())
    data["schema_version"] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(VialticError) as error:
        rig.service.get_proposal(proposal.id)
    assert error.value.code == "invalid_file"
    path.write_text("{nonsense")
    with pytest.raises(VialticError):
        rig.service.get_proposal(proposal.id)
    with pytest.raises(VialticError):
        rig.service.get_proposal("../../etc/passwd")


def test_expired_proposal_is_refused(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    later = proposal.expires_at + timedelta(seconds=1)
    attestation = rig.approve(proposal)
    rig.options.now = lambda: later
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(proposal.id, attestation)
    assert error.value.code == "proposal_expired" and rig.write_packets() == 0


def test_expired_proposals_are_pruned_and_bounded(rig):
    from vialtic.storage import MAX_PROPOSALS

    first = rig.plan((1, 0, 2, "KC_A"))
    later = first.expires_at + timedelta(seconds=1)
    rig.options.now = lambda: later
    rig.plan((1, 0, 3, "KC_B"))
    assert not rig.store._proposal_path(first.id).exists()
    for i in range(MAX_PROPOSALS + 5):
        rig.options.now = lambda i=i: later + timedelta(seconds=i)
        rig.plan((1, 0, 3, f"0x{0x7E00 + i:x}"))
    assert len(list((rig.store.root / "proposals").glob("*.json"))) <= MAX_PROPOSALS


@pytest.mark.parametrize("change,error_code", [
    ((9, 0, 0, "KC_A"), "invalid_coordinate"), ((0, 99, 0, "KC_A"), "invalid_coordinate"),
    ((0, 0, 99, "KC_A"), "invalid_coordinate"), ((0, 3, 4, "KC_A"), "invalid_coordinate"),
    ((0, 0, 0, "KC_BOGUS"), "invalid_keycode"), ((0, 0, 0, "MO(9)"), "invalid_layer_reference"),
])
def test_invalid_changes_are_refused_whole(rig, change, error_code):
    with pytest.raises(VialticError) as error:
        rig.plan((1, 0, 2, "KC_A"), change)
    assert error.value.code == error_code
    assert not list((rig.store.root / "proposals").glob("*.json")) if (rig.store.root / "proposals").exists() else True


def test_duplicate_target_and_noop_handling(rig):
    with pytest.raises(VialticError) as error:
        rig.plan((1, 0, 2, "KC_A"), (1, 0, 2, "KC_B"))
    assert error.value.code == "invalid_request"
    with pytest.raises(VialticError) as error:
        rig.plan((1, 0, 2, "KC_3"))
    assert error.value.code == "no_changes"
    proposal = rig.plan((1, 0, 2, "KC_3"), (1, 0, 3, "KC_A"))
    assert len(proposal.operations) == 1 and any("Skipped" in w for w in proposal.warnings)


def test_change_set_bounds():
    with pytest.raises(ValueError):
        ChangeSet(changes=[])
    with pytest.raises(ValueError):
        ChangeSet(changes=[SetKeyChange(layer=0, row=0, col=0, keycode="KC_A")] * 4097)
    with pytest.raises(ValueError):
        ChangeSet.model_validate({"changes": [{"type": "raw_packet", "data": "FF"}]})


# Apply.

def test_apply_verifies_backs_up_and_keeps_one_previous(rig):
    before = rig.service.read_snapshot()
    proposal = rig.plan((1, 0, 2, "LGUI(KC_3)"))
    result = rig.apply(proposal)
    assert result.outcome == "verified" and code(rig) == LGUI_3
    assert result.write_packets_sent == 1
    assert [op.status for op in result.operations] == ["applied"]
    binding = proposal.device.binding_id
    previous = read_model(rig.store.previous_path(binding), Snapshot)
    assert previous.family_fingerprints == before.family_fingerprints
    assert previous.configuration.lighting.kind == "qmk_rgblight"
    assert previous.configuration.macros.buffer_b64 is not None
    assert not rig.store.has_unresolved_write(binding)
    files = sorted(p.name for p in rig.store.device_dir(binding).iterdir())
    assert files == ["previous.json"]

    rig.apply(rig.plan((1, 0, 3, "KC_A")))
    assert sorted(p.name for p in rig.store.device_dir(binding).iterdir()) == ["previous.json"]
    previous = read_model(rig.store.previous_path(binding), Snapshot)
    assert {(e.layer, e.row, e.col): e.raw for e in previous.configuration.keymap}[(1, 0, 2)] == LGUI_3


def test_stored_files_are_private(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    rig.apply(proposal)
    path = rig.store.previous_path(proposal.device.binding_id)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_proposal_is_single_use(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    attestation = rig.approve(proposal)
    assert rig.service.apply_proposal(proposal.id, attestation).outcome == "verified"
    with pytest.raises(VialticError) as error:
        rig.service.apply_proposal(proposal.id, attestation)
    assert error.value.code == "proposal_not_found"


def test_encoder_write_and_readback(make_rig):
    rig = make_rig("generic")
    proposal = rig.service.create_proposal(None, ChangeSet(changes=[
        SetEncoderChange(layer=0, index=0, direction=1, keycode="KC_VOLU")]))
    assert proposal.affected == ["encoders"]
    assert rig.apply(proposal).outcome == "verified"
    entries = {(e.layer, e.index, e.direction): e for e in rig.service.read_snapshot().configuration.encoders}
    assert entries[(0, 0, 1)].keycode == "KC_VOLU" and entries[(0, 0, 0)].keycode == "KC_TRNS"
    undo = rig.service.create_undo_proposal(None)
    assert rig.apply(undo).outcome == "verified"
    entries = {(e.layer, e.index, e.direction): e for e in rig.service.read_snapshot().configuration.encoders}
    assert entries[(0, 0, 1)].keycode == "KC_TRNS"


def test_legacy_protocol_uses_its_own_keycode_table(make_rig):
    rig = make_rig("legacy")
    proposal = rig.plan((0, 0, 0, "LGUI(KC_3)"), (1, 0, 0, "MO(1)"))
    assert rig.apply(proposal).outcome == "verified"
    entries = {(e.layer, e.row, e.col): e for e in rig.service.read_snapshot().configuration.keymap}
    assert entries[(0, 0, 0)].keycode == "LGUI(KC_3)" and entries[(1, 0, 0)].keycode == "MO(1)"
    assert entries[(1, 0, 0)].raw != 0x5221  # the v6 encoding of MO(1)


def test_stale_when_another_app_changes_the_keymap(rig):
    proposal = rig.plan((1, 0, 3, "KC_A"))
    rig.external_set(0, 0, 0, KC_B)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_stale"
    assert error.value.details["changed_families"] == ["keymap"]
    assert rig.write_packets() == 0
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)


def test_stale_when_macros_or_settings_change(rig):
    proposal = rig.plan((1, 0, 3, "KC_A"))
    firmware = rig.backend.firmware(0)
    firmware.state["settings"]["1"] = 1
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.details["changed_families"] == ["settings"]


def test_lighting_keys_on_the_board_do_not_invalidate_a_proposal(rig):
    proposal = rig.plan((1, 0, 3, "KC_A"))
    rig.backend.firmware(0).state["lighting"]["rgb_brightness"] = 7
    assert rig.apply(proposal).outcome == "verified"


def test_wrong_device_is_refused(make_rig, tmp_path):
    rig = make_rig("multi")
    generic, split = rig.service.list_devices().devices
    proposal = rig.plan((1, 0, 2, "KC_A"), selector=generic.identity.device_id)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal, selector=split.identity.device_id)
    assert error.value.code == "device_changed" and rig.write_packets() == 0


def test_identity_change_is_refused(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    rig.backend.specs = [type(rig.backend.specs[0])(**{**rig.backend.specs[0].__dict__, "uid": 0xFF})]
    rig.backend._firmware.clear()
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "device_changed" and rig.write_packets() == 0


def test_identical_units_need_explicit_reselection(make_rig):
    rig = make_rig("twins")
    a, b = rig.service.list_devices().devices
    with pytest.raises(VialticError) as error:
        rig.plan((1, 0, 2, "KC_A"))
    assert error.value.code == "device_selection_required"
    proposal = rig.plan((1, 0, 2, "KC_A"), selector=b.identity.device_id)
    assert any("exact identity" in w for w in proposal.warnings)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "device_ambiguous" and rig.write_packets() == 0
    assert rig.apply(proposal, selector=b.identity.device_id).outcome == "verified"
    assert rig.keycode(1, 0, 2, device=1) == KC_A and rig.keycode(1, 0, 2, device=0) == ORIGINAL


def test_concurrent_apply_is_refused_by_the_os_lock(make_rig):
    rig = make_rig(lock_timeout=0.05)
    proposal = rig.plan((1, 0, 2, "KC_A"))
    with rig.store.binding_lock(proposal.device.binding_id, 1):
        with pytest.raises(VialticError) as error:
            rig.apply(proposal)
    assert error.value.code == "lock_timeout" and rig.write_packets() == 0


def test_blocked_backup_blocks_the_write(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    original = rig.store.write_pending

    def broken(binding, snapshot):
        raise VialticError("backup_failed", "disk full")

    rig.store.write_pending = broken
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "backup_failed"
    assert rig.write_packets() == 0 and code(rig) == ORIGINAL
    rig.store.write_pending = original


def test_backup_io_error_blocks_the_write(rig, monkeypatch):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    import vialtic.storage as storage

    def no_space(path, model):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "write_model", no_space)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "backup_failed" and rig.write_packets() == 0


# Failure recovery.

def _multi_plan(rig):
    return rig.plan((1, 0, 2, "KC_A"), (1, 0, 3, "KC_B"), (1, 0, 4, "KC_C"))


def test_disconnect_then_reconnect_restores_automatically(make_rig):
    rig = make_rig(faults=Faults(disconnect_after_writes=1, reconnect_after_seconds=0.05))
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    result = rig.apply(proposal)
    assert result.outcome == "failed_restored"
    after = rig.service.read_snapshot()
    assert after.family_fingerprints["keymap"] == before.family_fingerprints["keymap"]
    assert [op.status for op in result.operations] == ["applied", "failed", "not_attempted"]
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)
    # A failed write does not rotate the backup.
    assert not rig.store.previous_path(proposal.device.binding_id).exists()


def test_disconnect_without_reconnect_reports_unknown_and_keeps_backup(make_rig):
    rig = make_rig(faults=Faults(disconnect_after_writes=1))
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    binding = proposal.device.binding_id
    result = rig.apply(proposal)
    assert result.outcome == "unknown" and "recover plan" in " ".join(result.recovery_steps)
    assert result.backup_path == str(rig.store.pending_path(binding))
    saved = read_model(rig.store.pending_path(binding), Snapshot)
    assert saved.family_fingerprints == before.family_fingerprints
    journal = rig.store.read_journal(binding)
    assert journal.status == "needs_recovery" and journal.completed_operations == 1 and journal.total_operations == 3


def test_failed_restore_reports_partial_and_previous_backup_survives(make_rig):
    rig = make_rig()
    rig.apply(rig.plan((1, 0, 1, "KC_A")))  # creates the one previous state
    binding = rig.service.list_devices().devices[0].identity.binding_id
    previous_bytes = rig.store.previous_path(binding).read_bytes()

    rig = rig.restart(Faults(disconnect_after_writes=1, reconnect_after_seconds=0.02, readonly_after_disconnect=True))
    keymap_before = rig.service.read_snapshot().family_fingerprints["keymap"]
    result = rig.apply(_multi_plan(rig))
    assert result.outcome == "partial"
    assert result.differences, "partial results must say what differs"
    assert rig.store.pending_path(binding).exists()
    assert rig.store.previous_path(binding).read_bytes() == previous_bytes

    # Nothing else may write until the interruption is resolved.
    with pytest.raises(VialticError) as error:
        rig.plan((0, 0, 0, "KC_Z"))
    assert error.value.code == "recovery_pending"
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "recovery_pending"

    # Next process: reads warn, recovery goes through approval, and baseline returns.
    healthy = rig.restart()
    assert any("recover plan" in w for w in healthy.service.read_snapshot().warnings)
    assert healthy.service.inspect_device().recovery_pending
    assert any(c.status == "problem" and "recovery" in c.name for c in healthy.service.diagnose().checks)
    writes = healthy.write_packets()
    plan = healthy.service.create_recovery_proposal(None)
    assert plan.proposal_type == "recovery" and plan.operations and healthy.write_packets() == writes
    with pytest.raises(VialticError):
        healthy.service.apply_proposal(plan.id, None)
    assert healthy.apply(plan).outcome == "verified"
    assert healthy.service.read_snapshot().family_fingerprints["keymap"] == keymap_before
    assert not healthy.store.has_unresolved_write(binding)
    assert healthy.store.previous_path(binding).read_bytes() == previous_bytes


def test_timeout_mid_write_is_not_reported_as_success(make_rig):
    rig = make_rig(faults=Faults(timeout_after_writes=0))
    proposal = _multi_plan(rig)
    result = rig.apply(proposal)
    assert result.outcome in {"partial", "unknown", "failed_restored"}
    assert result.outcome != "verified"
    assert [op.status for op in result.operations][0] == "failed"


def test_silently_dropped_write_is_never_reported_as_success(make_rig):
    rig = make_rig(faults=Faults(drop_writes_after=1))
    proposal = _multi_plan(rig)
    result = rig.apply(proposal)
    # The first write landed and later ones, including the restore, are acknowledged but dropped.
    assert result.outcome == "partial"
    assert rig.keycode(1, 0, 2) == KC_A and rig.keycode(1, 0, 3) != KC_B
    assert [(d.family, d.target) for d in result.differences] == [("keymap", "1:0,2")]
    assert rig.store.pending_path(proposal.device.binding_id).exists()


def test_cancel_during_write_restores(make_rig):
    rig = make_rig()
    proposal = _multi_plan(rig)
    original = rig.service._write_operation
    calls = []

    def interrupted(session, op):
        calls.append(op)
        if len(calls) == 2:
            raise KeyboardInterrupt
        original(session, op)

    rig.service._write_operation = staticmethod(interrupted)
    before = code(rig)
    result = rig.apply(proposal)
    assert result.outcome == "failed_restored"
    assert code(rig) == before


def test_process_crash_never_writes_on_next_start_and_offers_recovery(make_rig):
    rig = make_rig()
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    binding = proposal.device.binding_id

    class Crash(BaseException):
        pass

    original = rig.service._write_operation
    calls = []

    def crashing(session, op):
        calls.append(op)
        if len(calls) == 2:
            raise Crash
        original(session, op)

    rig.service._write_operation = staticmethod(crashing)
    with pytest.raises(Crash):
        rig.apply(proposal)
    assert rig.keycode(1, 0, 2) == KC_A, "the first operation landed"
    assert rig.store.pending_path(binding).exists() and rig.store.read_journal(binding).status == "applying"

    restarted = rig.restart()
    writes = restarted.write_packets()
    restarted.service.list_devices()
    restarted.service.read_snapshot()
    assert restarted.write_packets() == writes and restarted.keycode(1, 0, 2) == KC_A, "startup must not write"
    with pytest.raises(VialticError) as error:
        restarted.plan((0, 0, 0, "KC_Z"))
    assert error.value.code == "recovery_pending"

    recovery = restarted.service.create_recovery_proposal(None)
    assert restarted.apply(recovery).outcome == "verified"
    assert restarted.service.read_snapshot().family_fingerprints["keymap"] == before.family_fingerprints["keymap"]
    assert not restarted.store.has_unresolved_write(binding)


def test_crash_after_verification_settles_rotation_without_device_access(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "KC_A"))
    binding = proposal.device.binding_id
    original = rig.store.promote_pending

    class Crash(BaseException):
        pass

    def crashing(binding_id):
        raise Crash

    rig.store.promote_pending = crashing
    with pytest.raises(Crash):
        rig.apply(proposal)
    rig.store.promote_pending = original
    assert rig.store.read_journal(binding).status == "verified"

    restarted = rig.restart()
    restarted.service.read_snapshot()
    assert not restarted.store.has_unresolved_write(binding)
    assert restarted.store.previous_path(binding).exists()
    assert restarted.write_packets() == 0


def test_recovery_with_ambiguous_identity_stops(make_rig):
    rig = make_rig("twins", faults=Faults(disconnect_after_writes=1, reconnect_after_seconds=0.02))
    b = rig.service.list_devices().devices[1]
    proposal = _multi_plan_for(rig, b.identity.device_id)
    result = rig.apply(proposal, selector=b.identity.device_id)
    assert result.outcome in {"partial", "unknown"}
    assert "several connected keyboards" in result.message
    assert rig.store.pending_path(proposal.device.binding_id).exists()


def _multi_plan_for(rig, selector):
    return rig.plan((1, 0, 2, "KC_A"), (1, 0, 3, "KC_B"), selector=selector)


def test_nothing_to_recover_is_explicit(rig):
    with pytest.raises(VialticError) as error:
        rig.service.create_recovery_proposal(None)
    assert error.value.code == "nothing_to_recover"


def test_recovery_plan_for_crash_before_first_write_is_empty_and_clears(make_rig):
    rig = make_rig()
    snapshot = rig.service.read_snapshot()
    binding = snapshot.device.binding_id
    rig.store.write_pending(binding, snapshot)
    plan = rig.service.create_recovery_proposal(None)
    assert plan.operations == []
    assert rig.apply(plan).outcome == "verified"
    assert not rig.store.has_unresolved_write(binding)
    assert rig.write_packets() == 0


# Undo.

def test_undo_restores_and_can_itself_be_undone(rig):
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "nothing_to_undo"

    rig.apply(rig.plan((1, 0, 2, "LGUI(KC_3)")))
    assert code(rig) == LGUI_3
    writes = rig.write_packets()
    undo = rig.service.create_undo_proposal(None)
    assert undo.proposal_type == "undo" and rig.write_packets() == writes
    assert undo.operations[0].before.raw == LGUI_3 and undo.operations[0].after.raw == ORIGINAL
    with pytest.raises(VialticError):
        rig.service.apply_proposal(undo.id, None)
    assert code(rig) == LGUI_3

    assert rig.apply(undo).outcome == "verified"
    assert code(rig) == ORIGINAL

    redo = rig.service.create_undo_proposal(None)
    assert rig.apply(redo).outcome == "verified"
    assert code(rig) == LGUI_3
    binding = undo.device.binding_id
    assert sorted(p.name for p in rig.store.device_dir(binding).iterdir()) == ["previous.json"]


def test_undo_with_nothing_different_is_explicit(rig):
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    rig.external_set(1, 0, 2, ORIGINAL)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "no_changes"


def _shrink_tap_dance(rig):
    """The firmware was rebuilt with fewer entries; the definition, and so the binding, are the same."""
    firmware = rig.backend.firmware(0)
    firmware.spec = dataclasses.replace(firmware.spec, tap_dance_count=firmware.spec.tap_dance_count - 1)
    firmware.state["tap_dance"].pop()


def test_undo_restores_a_family_that_moved_after_the_write(rig):
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    rig.backend.firmware(0).state["tap_dance"][0][4] = 123
    undo = rig.service.create_undo_proposal(None)
    assert sorted(undo.affected) == ["keymap", "tap_dance"]
    assert rig.apply(undo).outcome == "verified"
    assert rig.backend.firmware(0).state["tap_dance"][0][4] == 200 and code(rig) == ORIGINAL


def test_undo_refuses_when_the_firmware_no_longer_has_the_same_entries(rig):
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    _shrink_tap_dance(rig)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "restore_unsupported"
    assert error.value.details["families"] == ["tap_dance"]
    assert code(rig) == KC_A


def test_undo_refuses_across_a_keycode_generation(make_rig):
    rig = make_rig()
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    firmware = rig.backend.firmware(0)
    firmware.spec = dataclasses.replace(firmware.spec, vial_protocol=5)
    with pytest.raises(VialticError) as error:
        rig.service.create_undo_proposal(None)
    assert error.value.code == "device_changed" and "encodes keycodes or macros differently" in error.value.message


def test_undo_warns_about_lighting_it_cannot_restore(rig):
    rig.apply(rig.plan((1, 0, 2, "KC_A")))
    rig.backend.firmware(0).state["lighting"]["rgb_brightness"] = 7
    undo = rig.service.create_undo_proposal(None)
    assert any("Lighting" in w for w in undo.warnings)


# Physical unlock.

def test_qk_boot_needs_unlock_and_relocks(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    assert proposal.unlock.required and [(p.row, p.col) for p in proposal.unlock.positions] == [(0, 0), (0, 1)]
    assert any("bootloader" in w for w in proposal.warnings)
    result = rig.apply(proposal)
    assert result.outcome == "verified" and result.relocked is True
    assert any("Hold these keys" in message for message in rig.progress)
    assert not rig.service.unlock_status(None).unlocked
    assert rig.service.read_snapshot().configuration.keymap


def test_unlock_timeout_writes_nothing_and_discards_the_temporary_backup(make_rig):
    rig = make_rig(faults=Faults(unlock_never=True), unlock_timeout=0.02)
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "unlock_timeout"
    assert rig.write_packets() == 0 and code(rig) == ORIGINAL
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)
    assert not rig.service.unlock_status(None).unlocked


def test_preexisting_unlock_is_left_alone(make_rig):
    rig = make_rig()
    rig.backend.firmware(0).state["unlocked"] = True
    result = rig.apply(rig.plan((1, 0, 2, "QK_BOOT")))
    assert result.outcome == "verified" and result.relocked is None
    assert rig.service.unlock_status(None).unlocked


def test_forged_unlock_field_is_detected(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    # A stored proposal cannot drop its unlock requirement: the id covers the field.
    forged = proposal.model_copy(update={"unlock": proposal.unlock.model_copy(update={"required": False})})
    with pytest.raises(VialticError) as error:
        proposals.check_integrity(forged)
    assert error.value.code == "proposal_tampered"


def test_unlock_commands_require_an_approved_proposal_that_needs_it(make_rig):
    rig = make_rig()
    plain = rig.plan((1, 0, 3, "KC_A"))
    with pytest.raises(VialticError) as error:
        rig.service.unlock_start(plain.id, rig.approve(plain), None)
    assert error.value.code == "invalid_request"
    boot = rig.plan((1, 0, 2, "QK_BOOT"))
    with pytest.raises(VialticError) as error:
        rig.service.unlock_start(boot.id, None, None)
    assert error.value.code == "approval_required"

    status = rig.service.unlock_start(boot.id, rig.approve(boot), None)
    assert status.in_progress
    for _ in range(5):
        status = rig.service.unlock_poll(None)
    assert status.unlocked
    # Unlocked through the commands, so apply still relocks when finished.
    result = rig.apply(boot)
    assert result.outcome == "verified" and result.relocked is True
    assert not rig.service.unlock_status(None).unlocked


# Restart behavior of the emulator itself: state persists, data stays isolated.

def test_emulator_state_is_isolated_and_persistent(tmp_path):
    first = Rig(tmp_path / "a")
    first.apply(first.plan((1, 0, 2, "KC_A")))
    assert first.restart().keycode(1, 0, 2) == KC_A
    other = Rig(tmp_path / "b")
    assert other.keycode(1, 0, 2) == ORIGINAL
    assert not list((tmp_path / "b").rglob("previous.json"))


# Regressions from the independent review of this path.

def _crash_on_write(rig, nth=2):
    class Crash(BaseException):
        pass

    original = rig.service._write_operation
    calls = []

    def crashing(session, op):
        calls.append(op)
        if len(calls) == nth:
            raise Crash
        original(session, op)

    rig.service._write_operation = staticmethod(crashing)
    return Crash


def _lost_unit(**overrides):
    return Faults(disconnect_only_device=1, **overrides)


@pytest.mark.parametrize("twin_has_own_keys", [False, True])
def test_restore_never_touches_an_identical_unit(make_rig, twin_has_own_keys):
    rig = make_rig("twins", faults=_lost_unit(disconnect_after_writes=1))
    a, b = rig.service.list_devices().devices
    if twin_has_own_keys:
        rig.external_set(1, 0, 2, 0x29, device=0)
        rig.external_set(1, 0, 3, 0x2A, device=0)
    a_keymap = rig.service.read_snapshot(a.identity.device_id).family_fingerprints["keymap"]
    proposal = _multi_plan_for(rig, b.identity.device_id)
    result = rig.apply(proposal, selector=b.identity.device_id)

    assert result.outcome in {"partial", "unknown"}, "a different unit must not count as restored"
    assert rig.service.read_snapshot(a.identity.device_id).family_fingerprints["keymap"] == a_keymap
    assert rig.store.pending_path(proposal.device.binding_id).exists(), "the backup must survive"
    assert rig.store.read_journal(proposal.device.binding_id).status == "needs_recovery"


def test_restore_stops_when_values_are_unexplained(make_rig):
    rig = make_rig(faults=Faults(drop_writes_after=0))
    proposal = rig.plan((1, 0, 2, "KC_A"))
    original = rig.service._write_operation

    def external_edit_then_write(session, op):
        rig.external_set(0, 0, 0, 0x29)  # lands while the write is in flight
        original(session, op)

    rig.service._write_operation = staticmethod(external_edit_then_write)
    result = rig.apply(proposal)
    assert result.outcome == "partial" and "neither the saved state nor this write" in result.message
    assert rig.keycode(0, 0, 0) == 0x29, "another application's change is not overwritten without approval"


def test_proposal_for_one_unit_is_not_written_to_its_twin(make_rig):
    rig = make_rig("twins", faults=_lost_unit())
    a, b = rig.service.list_devices().devices
    proposal = rig.plan((1, 0, 2, "KC_A"), selector=b.identity.device_id)

    with pytest.raises(VialticError) as error:  # both connected, the other one selected
        rig.apply(proposal, selector=a.identity.device_id)
    assert error.value.code == "device_ambiguous"

    rig.backend.disconnect()  # B is gone and A has the same identity
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "device_ambiguous" and "may be a different unit" in error.value.message
    assert rig.write_packets() == 0 and rig.keycode(1, 0, 2, device=0) == ORIGINAL

    # Only an explicit reselection continues, and the path change is still reported.
    result = rig.apply(proposal, selector=a.identity.device_id)
    assert result.outcome == "verified" and any("path changed" in w for w in result.warnings)


def test_proposal_file_copied_over_another_id_is_refused(rig):
    first = rig.plan((1, 0, 2, "KC_A"))
    second = rig.plan((1, 0, 3, "KC_B"))
    rig.store._proposal_path(first.id).write_bytes(rig.store._proposal_path(second.id).read_bytes())
    with pytest.raises(VialticError) as error:
        rig.service.get_proposal(first.id)
    assert error.value.code == "proposal_tampered"
    assert rig.write_packets() == 0


def test_recovery_restores_a_family_that_moved_after_the_interrupted_write(make_rig):
    rig = make_rig()
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)

    restarted = rig.restart()
    restarted.backend.firmware(0).state["tap_dance"][0][4] = 123
    recovery = restarted.service.create_recovery_proposal(None)
    assert "tap_dance" in recovery.affected
    assert restarted.apply(recovery).outcome == "verified"
    after = restarted.service.read_snapshot()
    assert after.family_fingerprints == before.family_fingerprints


def test_recovery_is_not_blocked_by_entries_the_firmware_no_longer_has(make_rig):
    rig = make_rig()
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)

    restarted = rig.restart()
    _shrink_tap_dance(restarted)
    recovery = restarted.service.create_recovery_proposal(None)
    assert any("tap_dance" in w and "stay as they are" in w for w in recovery.warnings)
    result = restarted.apply(recovery)
    assert result.outcome == "verified"

    after = restarted.service.read_snapshot()
    assert after.family_fingerprints["keymap"] == before.family_fingerprints["keymap"]
    assert not restarted.store.has_unresolved_write(proposal.device.binding_id)
    restarted.plan((0, 0, 0, "KC_Z"))


def test_recovery_verification_still_notices_a_skipped_family_moving(make_rig):
    rig = make_rig()
    proposal = _multi_plan(rig)
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)
    restarted = rig.restart()
    firmware = restarted.backend.firmware(0)
    _shrink_tap_dance(restarted)
    recovery = restarted.service.create_recovery_proposal(None)
    original = restarted.service._write_operation

    def moves_during_write(session, op):
        firmware.state["tap_dance"][0][4] = 124
        original(session, op)

    restarted.service._write_operation = staticmethod(moves_during_write)
    assert restarted.apply(recovery).outcome != "verified"


def _interrupt_once(obj, name):
    original = getattr(obj, name)
    fired = []

    def wrapper(*args, **kwargs):
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    setattr(obj, name, wrapper)


@pytest.mark.parametrize("site", ["promote_pending", "clear_journal", "delete_proposal"])
def test_interrupt_after_verification_keeps_the_write(rig, site):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    binding = proposal.device.binding_id
    _interrupt_once(rig.store, site)
    result = rig.apply(proposal)
    assert result.outcome == "verified" and any("Interrupted" in w for w in result.warnings)
    assert rig.keycode(1, 0, 2) == KC_A, "a verified write must not be rolled back"
    assert rig.store.previous_path(binding).exists() and not rig.store.has_unresolved_write(binding)


def test_interrupt_during_relock_keeps_the_write_and_locks(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    _interrupt_once(rig.service, "_relock")
    result = rig.apply(proposal)
    assert result.outcome == "verified" and result.relocked is True
    assert not rig.service.unlock_status(None).unlocked


def test_crash_while_unlocked_is_reported_and_recovery_relocks(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"), (1, 0, 3, "KC_A"))
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)

    restarted = rig.restart()
    assert restarted.service.unlock_status(None).unlocked
    assert any("vialtic lock" in w for w in restarted.service.inspect_device().warnings)
    assert any("vialtic lock" in w for w in restarted.service.read_snapshot().warnings)
    result = restarted.apply(restarted.service.create_recovery_proposal(None))
    assert result.outcome == "verified" and result.relocked is True
    assert not restarted.service.unlock_status(None).unlocked
    assert restarted.store.read_unlock_marker(proposal.device.binding_id) is None


def test_partial_outcome_tries_to_lock_and_says_how_it_went(make_rig):
    rig = make_rig(faults=Faults(drop_writes_after=1))
    result = rig.apply(rig.plan((1, 0, 2, "KC_A"), (1, 0, 3, "QK_BOOT"), (1, 0, 4, "KC_B")))
    assert result.outcome == "partial" and result.relocked is True and result.relock_error is None
    assert not rig.service.unlock_status(None).unlocked


def test_failed_lock_is_reported_and_marker_kept(make_rig):
    rig = make_rig(faults=Faults(fail_lock=True))
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    result = rig.apply(proposal)
    assert result.outcome == "verified" and result.relocked is False and "vialtic lock" in result.relock_error
    assert rig.service.unlock_status(None).unlocked
    assert rig.store.read_unlock_marker(proposal.device.binding_id) == proposal.id


def test_failed_lock_after_restore_is_reported(make_rig):
    rig = make_rig(faults=Faults(fail_lock=True))
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    original = rig.service._write_and_verify

    def failing(session, state):
        original(session, state)
        from vialtic.service import _WriteFailure
        raise _WriteFailure("forced")

    rig.service._write_and_verify = failing
    result = rig.apply(proposal)
    assert result.outcome == "failed_restored" and result.relocked is False and result.relock_error


def test_full_disk_mid_write_still_restores_the_keyboard(rig):
    before = rig.service.read_snapshot()
    proposal = _multi_plan(rig)
    original = rig.store.write_journal
    calls = []

    def failing(journal):
        calls.append(journal)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        original(journal)

    rig.store.write_journal = failing
    result = rig.apply(proposal)
    assert result.outcome == "failed_restored"
    assert "metadata" in result.message and any("metadata" in w for w in result.warnings)
    assert rig.service.read_snapshot().family_fingerprints["keymap"] == before.family_fingerprints["keymap"]


def test_storage_failure_after_verification_is_not_a_failed_write(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))

    def failing(binding):
        raise OSError(28, "No space left on device")

    original = rig.store.promote_pending
    rig.store.promote_pending = failing
    result = rig.apply(proposal)
    assert result.outcome == "verified" and any("local files" in w for w in result.warnings)
    assert rig.keycode(1, 0, 2) == KC_A
    # The next invocation finishes the rotation without touching the keyboard.
    rig.store.promote_pending = original
    rig.service.read_snapshot()
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)
    assert rig.store.previous_path(proposal.device.binding_id).exists()


def _during_unlock_wait(rig, action):
    def progress(message):
        if message.startswith("Hold these keys"):
            action()

    rig.options.progress = progress


def test_external_change_during_unlock_wait_is_stale_and_relocks(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    _during_unlock_wait(rig, lambda: rig.external_set(0, 0, 0, 0x29))
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_stale" and rig.write_packets() == 0
    assert rig.keycode(0, 0, 0) == 0x29
    assert not rig.service.unlock_status(None).unlocked, "the unlock this attempt made is undone"
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)
    assert rig.store.read_unlock_marker(proposal.device.binding_id) is None


def test_expiry_during_unlock_wait_is_refused(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))

    def expire():
        later = proposal.expires_at + timedelta(seconds=1)
        rig.options.now = lambda: later

    _during_unlock_wait(rig, expire)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_expired" and rig.write_packets() == 0
    assert not rig.service.unlock_status(None).unlocked


def test_stale_proposal_does_not_ask_for_an_unlock(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    rig.external_set(0, 0, 0, 0x29)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_stale"
    assert not any("Hold these keys" in message for message in rig.progress)


def test_unlock_start_applies_the_approval_checks(make_rig):
    rig = make_rig("multi")
    generic, split = rig.service.list_devices().devices
    proposal = rig.plan((1, 0, 2, "QK_BOOT"), selector=split.identity.device_id)
    forged = rig.approve(proposal).model_copy(update={"binding_id": generic.identity.binding_id})
    with pytest.raises(VialticError) as error:
        rig.service.unlock_start(proposal.id, forged, None)
    assert error.value.code == "approval_required"

    attestation = rig.approve(proposal)
    later = proposal.expires_at + timedelta(seconds=1)
    rig.options.now = lambda: later
    with pytest.raises(VialticError) as error:
        rig.service.unlock_start(proposal.id, attestation, None)
    assert error.value.code == "proposal_expired"
    assert not rig.service.unlock_status(split.identity.device_id).in_progress


def test_stale_marker_does_not_lock_a_keyboard_the_user_unlocked(make_rig):
    rig = make_rig()
    binding = rig.service.list_devices().devices[0].identity.binding_id
    rig.backend.firmware(0).state["unlocked"] = True
    rig.store.set_unlock_marker(binding, "pr-someotherproposal")
    result = rig.apply(rig.plan((1, 0, 2, "KC_A")))
    assert result.outcome == "verified" and result.relocked is None
    assert rig.service.unlock_status(None).unlocked


def test_marker_is_dropped_once_the_firmware_is_locked(make_rig):
    rig = make_rig()
    binding = rig.service.list_devices().devices[0].identity.binding_id
    rig.store.set_unlock_marker(binding, "pr-replugged")
    assert rig.service.inspect_device().warnings == []
    assert rig.store.read_unlock_marker(binding) is None


def test_unlock_timeout_keeps_the_marker_until_a_replug_ends_the_unlock(make_rig):
    """Firmware ignores a lock while it waits, and polling could still complete that unlock."""
    rig = make_rig(faults=Faults(unlock_never=True), unlock_timeout=0.02)
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    with pytest.raises(VialticError):
        rig.apply(proposal)
    assert rig.store.read_unlock_marker(proposal.device.binding_id) == proposal.id
    rig.backend.firmware(0).power_cycle()
    assert rig.service.inspect_device().warnings == []
    assert rig.store.read_unlock_marker(proposal.device.binding_id) is None


def test_refused_after_commands_unlocked_locks_again(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    rig.service.unlock_start(proposal.id, rig.approve(proposal), None)
    for _ in range(5):
        rig.service.unlock_poll(None)
    assert rig.service.unlock_status(None).unlocked
    rig.external_set(0, 0, 0, 0x29)
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "proposal_stale"
    assert not rig.service.unlock_status(None).unlocked


def test_hid_failure_during_unlock_is_a_public_error_and_leaves_nothing(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))

    def failing(session, proposal):
        raise RuntimeError("failed to communicate with the device")

    rig.service._unlock_for = failing
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "device_communication_failed"
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)


def test_lost_permission_is_reported_as_permission(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "KC_A"))

    def denied(path):
        raise PermissionError(13, "denied")

    rig.backend.open = denied
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "permission_denied"


def test_lost_permission_stops_automatic_restore(make_rig):
    rig = make_rig(faults=Faults(disconnect_after_writes=1, reconnect_after_seconds=0.0), reconnect_window=30.0)
    proposal = _multi_plan(rig)
    original = rig.backend.open
    opened = []

    def denied_after_first(path):
        opened.append(path)
        if rig.backend.reconnected:
            raise PermissionError(13, "denied")
        return original(path)

    rig.backend.open = denied_after_first
    result = rig.apply(proposal)
    assert result.outcome == "unknown" and "permission" in result.message


def test_interrupt_between_backup_and_journal_leaves_nothing_to_recover(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    _interrupt_once(rig.store, "write_journal")
    with pytest.raises(VialticError) as error:
        rig.apply(proposal)
    assert error.value.code == "cancelled" and rig.write_packets() == 0
    assert not rig.store.has_unresolved_write(proposal.device.binding_id)
    assert rig.apply(proposal).outcome == "verified"


def test_corrupt_journal_does_not_break_reads_or_recovery(make_rig):
    rig = make_rig()
    proposal = _multi_plan(rig)
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)
    binding = proposal.device.binding_id
    rig.store.journal_path(binding).write_text("{corrupt")

    restarted = rig.restart()
    assert any("recover plan" in w for w in restarted.service.read_snapshot().warnings)
    restarted.service.diagnose()
    assert restarted.apply(restarted.service.create_recovery_proposal(None)).outcome == "verified"
    assert not restarted.store.has_unresolved_write(binding)


def test_corrupt_backup_says_how_to_continue_without_deleting_it(make_rig):
    rig = make_rig()
    proposal = _multi_plan(rig)
    crash = _crash_on_write(rig)
    with pytest.raises(crash):
        rig.apply(proposal)
    path = rig.store.pending_path(proposal.device.binding_id)
    path.write_text("{corrupt")
    with pytest.raises(VialticError) as error:
        rig.restart().service.create_recovery_proposal(None)
    assert error.value.code == "invalid_file" and str(path) in error.value.next_steps[0]
    assert path.exists()


def test_atomic_write_removes_temp_left_by_a_killed_process(tmp_path):
    from vialtic.storage import atomic_write

    target = tmp_path / "devices" / "x" / "pending.json"
    target.parent.mkdir(parents=True)
    leftover = target.parent / ".pending.json.abc123"
    leftover.write_text("full snapshot")
    atomic_write(target, b"{}")
    assert not leftover.exists() and target.read_bytes() == b"{}"


def test_proposal_lists_what_the_backup_cannot_hold(make_rig):
    rig = make_rig()
    spec = rig.backend.specs[0]
    rig.backend.specs = [type(spec)(**{**spec.__dict__, "settings": (1, 2, 7, 60000)})]
    firmware = rig.backend.firmware(0)
    firmware.spec = rig.backend.specs[0]
    firmware.state["settings"]["60000"] = 0
    snapshot = rig.service.read_snapshot()
    assert snapshot.coverage["settings"] == "partial_unknown_qsids"
    proposal = rig.plan((1, 0, 2, "KC_A"))
    assert any("60000" in w and "never writes" in w for w in proposal.warnings)
    assert "Everything Vialtic can read" in proposal.recovery and "full configuration" not in proposal.recovery


def test_crash_before_journal_while_unlocked_still_relocks_on_recovery(make_rig):
    rig = make_rig()
    proposal = rig.plan((1, 0, 2, "QK_BOOT"))
    binding = proposal.device.binding_id

    class Crash(BaseException):
        pass

    def crashing(journal):
        raise Crash

    rig.store.write_journal = crashing
    original_discard = rig.store.discard_pending
    rig.store.discard_pending = lambda binding_id: None  # a killed process runs no cleanup
    with pytest.raises(Crash):
        rig.apply(proposal)
    rig.store.discard_pending = original_discard
    assert rig.store.pending_path(binding).exists() and rig.store.read_journal(binding) is None

    restarted = rig.restart()
    assert any("lock" in c.name and c.status == "warning" for c in restarted.service.diagnose().checks)
    result = restarted.apply(restarted.service.create_recovery_proposal(None))
    assert result.outcome == "verified" and result.relocked is True
    assert not restarted.service.unlock_status(None).unlocked


def test_full_disk_at_commit_still_finishes_the_rotation(rig):
    proposal = rig.plan((1, 0, 2, "KC_A"))
    binding = proposal.device.binding_id
    original = rig.store.write_journal

    def failing(journal):
        if journal.status == "verified":
            raise OSError(28, "No space left on device")
        original(journal)

    rig.store.write_journal = failing
    result = rig.apply(proposal)
    assert result.outcome == "verified" and not rig.store.has_unresolved_write(binding)
    assert rig.store.previous_path(binding).exists()
