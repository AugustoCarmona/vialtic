# SPDX-License-Identifier: GPL-2.0-or-later
"""Quality gate: a crash, an interrupt or a full disk at every state-file operation and every
packet, for every operation type and family, must never produce a false success, lose a backup,
rotate previous.json after a failure, write at startup or leave the lock state unreported.
"""
import contextlib
import errno
import json
import multiprocessing
import os
import pathlib
import shutil
import sys
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import pytest

from conftest import Rig
from vialtic import proposals
from vialtic.device import classify_packet
from vialtic.errors import VialticError
from vialtic.models import (
    ChangeSet, SetAltRepeatKeyChange, SetComboChange, SetEncoderChange, SetKeyChange, SetKeyOverrideChange,
    SetLayoutOptionChange, SetLightingChange, SetMacroChange, SetQmkSettingChange, SetTapDanceChange, Snapshot,
)
from vialtic.storage import read_model


class Killed(BaseException):
    """The process died. From here on nothing it tries, including cleanup, has any effect."""


OPERATIONS = {
    "key": SetKeyChange(layer=1, row=0, col=1, keycode="LGUI(KC_3)"),
    "key_boot": SetKeyChange(layer=1, row=0, col=1, keycode="QK_BOOT"),
    "encoder": SetEncoderChange(layer=0, index=0, direction=1, keycode="KC_VOLU"),
    "layout_option": SetLayoutOptionChange(index=1, choice=2),
    "tap_dance": SetTapDanceChange(index=1, on_tap="KC_A", tapping_term=150),
    "combo": SetComboChange(index=1, keys=["KC_A", "KC_B"], output="KC_C"),
    "key_override": SetKeyOverrideChange(index=1, trigger="KC_A", replacement="KC_B", options=0x80),
    "alt_repeat": SetAltRepeatKeyChange(index=1, keycode="KC_A", alt_keycode="KC_B", options=0x08),
    "setting": SetQmkSettingChange(qsid=7, value=175),
    "macro": SetMacroChange(index=1, actions=[["text", "hello world, this spans several packets"]]),
    "lighting_saved": SetLightingChange(system="vialrgb", save=True, mode=3),
    "lighting_transient": SetLightingChange(system="vialrgb", save=False, value=33),
}
ALL_LIGHTING = ("rgblight", "backlight", "vialrgb")
FLOWS = ("change", "undo", "recovery", "restore")
FAULTS = ("kill", "interrupt", "enospc")
# Prepares a history so previous.json exists and must survive.
EARLIER = SetKeyChange(layer=0, row=1, col=0, keycode="KC_Z")


class Injector:
    """Fires one fault at the Nth site, counting state-file operations and packets."""

    def __init__(self, target=None, fault=None):
        self.target, self.fault = target, fault
        self.count = 0
        self.active = False
        self.dead = False
        self.fired = None
        # "restore" flow: a write fails first, then sites are counted through the restore that follows.
        self.first_failure = False

    def site(self, label):
        if self.dead:
            raise Killed
        if not self.active:
            return
        self.count += 1
        if self.count == self.target:
            self.fired = label
            if self.fault == "kill":
                self.dead = True
                raise Killed
            if self.fault == "interrupt":
                raise KeyboardInterrupt
            raise OSError(errno.ENOSPC, "No space left on device")

    def packet(self, msg):
        if self.first_failure and classify_packet(msg) == "write":
            self.first_failure = False
            self.active = True
            raise OSError("device disconnected")
        self.site(f"packet {msg[:3].hex()}")


@contextlib.contextmanager
def state_file_sites(injector, root):
    """Hook the OS calls behind the state files. Everything else, such as pytest itself, passes through."""
    real = {name: getattr(os, name) for name in ("replace", "fsync", "unlink", "open", "mkdir", "fchmod")}
    real_mkstemp = tempfile.mkstemp
    root = str(root)

    def inside(path):
        return str(path).startswith(root)

    def replace(src, dst, **kwargs):
        if inside(dst):
            injector.site(f"replace {pathlib.Path(dst).name}")
        return real["replace"](src, dst, **kwargs)

    def fsync(fd):
        injector.site("fsync")  # durability is the OS's job here, and skipping it keeps the sweep fast

    def unlink(path, **kwargs):
        if inside(path):
            injector.site(f"unlink {pathlib.Path(path).name}")
        return real["unlink"](path, **kwargs)

    def open_(path, *args, **kwargs):
        if inside(path):
            injector.site(f"open {pathlib.Path(path).name}")
        return real["open"](path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        if inside(path):
            injector.site(f"mkdir {pathlib.Path(path).name}")
        return real["mkdir"](path, *args, **kwargs)

    def fchmod(fd, mode):
        injector.site("fchmod")
        return real["fchmod"](fd, mode)

    def mkstemp(*args, **kwargs):
        if inside(kwargs.get("dir", "")):
            injector.site("mkstemp")
        return real_mkstemp(*args, **kwargs)

    os.replace, os.fsync, os.unlink, os.open, os.mkdir, os.fchmod = replace, fsync, unlink, open_, mkdir, fchmod
    tempfile.mkstemp = mkstemp
    try:
        yield
    finally:
        for name, function in real.items():
            setattr(os, name, function)
        tempfile.mkstemp = real_mkstemp


def build_rig(directory, injector=None):
    rig = Rig(directory, "full", reconnect_window=0.05, reconnect_poll=0.0, lock_timeout=0.5)
    # The emulator's own file is not a state file: keep it out of the sweep.
    rig.backend.save = lambda: rig.backend.path.write_text(json.dumps(rig.backend.states))
    if injector is not None:
        rig.backend.packet_hook = injector.packet
    return rig


def plan_change_set(rig, changes):
    return rig.service.create_proposal(None, ChangeSet(changes=changes))


def plan_change(rig, change):
    return plan_change_set(rig, [change])


def prepare(directory, flow, change):
    """Everything that happens before the operation under test, free of faults. Returns the proposal id."""
    idle = Injector()
    with state_file_sites(idle, directory):
        rig = build_rig(directory)
        assert rig.apply(plan_change(rig, EARLIER)).outcome == "verified"
        if flow in ("change", "restore"):
            return plan_change(rig, change).id
        if flow == "undo":
            assert rig.apply(plan_change(rig, change)).outcome == "verified"
            return rig.service.create_undo_proposal(None).id
        # A killed write to recover from. The second write packet is late enough for the first to land.
        multi = plan_change_set(rig, [SetKeyChange(layer=0, row=0, col=0, keycode="KC_F1"), change])
        writes = []

        def kill_on_second_write(msg):
            if classify_packet(msg) == "write":
                writes.append(msg)
                if len(writes) == 2:
                    idle.dead = True
            idle.site("kill")

        rig.backend.packet_hook = kill_on_second_write
        with contextlib.suppress(Killed):
            rig.apply(multi)
        idle.dead = False
        return build_rig(directory).service.create_recovery_proposal(None).id


def read_bytes(path):
    return path.read_bytes() if path.exists() else None


def run_case(directory, flow, proposal_id, target, fault):
    injector = Injector(target=target, fault=fault)
    with state_file_sites(injector, directory):
        rig = build_rig(directory, injector)
        proposal = rig.service.get_proposal(proposal_id)
        binding = proposal.device.binding_id
        start = rig.service.read_snapshot().configuration
        previous_before = read_bytes(rig.store.previous_path(binding))
        pending_before = read_bytes(rig.store.pending_path(binding))
        attestation = rig.approve(proposal)
        started_unlocked = rig.service.unlock_status(None).unlocked
        outcome, relock_error = None, None
        injector.first_failure = flow == "restore"
        injector.active = flow != "restore"
        try:
            result = rig.service.apply_proposal(proposal.id, attestation)
            outcome, relock_error = result.outcome, result.relock_error
        except (VialticError, KeyboardInterrupt, OSError, Killed):
            pass
        count = injector.count
        injector.active = injector.dead = injector.first_failure = False
        rig.backend.packet_hook = None
        if target is not None:
            check_invariants(directory, flow, proposal, start, previous_before, pending_before, outcome, relock_error, started_unlocked)
    return injector, count, outcome


def check_invariants(directory, flow, proposal, start, previous_before, pending_before, outcome, relock_error, started_unlocked):
    binding = proposal.device.binding_id
    fresh = build_rig(directory)
    previous = fresh.store.previous_path(binding)
    pending = fresh.store.pending_path(binding)

    # Whatever is on disk must be whole.
    for path in (previous, pending):
        if path.exists():
            read_model(path, Snapshot)

    # Dying during a physical unlock leaves the firmware waiting: it is reported, and a replug ends it.
    firmware = fresh.backend.firmware(0)
    if firmware.state["unlock_in_progress"]:
        with pytest.raises(VialticError) as waiting:
            fresh.service.read_snapshot()
        assert waiting.value.code == "unlock_in_progress"
        assert fresh.service.unlock_status(None).in_progress
        firmware.power_cycle()
        fresh.backend.save()

    # Nothing writes on startup, whatever state the last process left.
    snapshot = fresh.service.read_snapshot()
    inspect = fresh.service.inspect_device()
    doctor = fresh.service.diagnose()
    assert fresh.write_packets() == 0, "a read or a restart sent a write"

    observed = snapshot.configuration
    at_target = not proposals.compare_state(start, proposal.operations, observed, lighting_systems=ALL_LIGHTING)
    at_start = not proposals.compare_state(start, [], observed, lighting_systems=ALL_LIGHTING)

    if outcome == "verified":
        assert at_target, "verified, but the keyboard differs"
    if outcome == "failed_restored":
        # What a failed apply restores is its baseline, and for a recovery that is the state it was heading to.
        assert at_target if flow == "recovery" else at_start, "restored, but the keyboard differs"

    if flow == "recovery":
        assert read_bytes(previous) == previous_before, "a recovery must not rotate previous.json"
        if not at_target:
            assert read_bytes(pending) == pending_before, "the backup must survive until recovery verifies"
    else:
        if outcome != "verified" and not at_target:
            assert read_bytes(previous) == previous_before, "previous.json rotated after a failure"
        if not at_start:
            backup = pending if pending.exists() else (previous if at_target else None)
            assert backup is not None and backup.exists(), "the keyboard changed and no backup holds the prior state"
            saved = read_model(backup, Snapshot).configuration
            assert not proposals.compare_state(saved, [], start, lighting_systems=ALL_LIGHTING), "the backup is not the prior state"

    # A keyboard left unlocked must be reported, somewhere a person will see it.
    if firmware.state["unlocked"] and not started_unlocked:
        reported = relock_error or inspect.warnings or any("lock" in c.name and c.status == "warning" for c in doctor.checks)
        assert reported, "left unlocked without saying so"

    # And there is always a way forward.
    if fresh.store.has_unresolved_write(binding):
        with pytest.raises(VialticError) as refused:
            plan_change(fresh, SetKeyChange(layer=0, row=1, col=1, keycode="KC_X"))
        assert refused.value.code == "recovery_pending"
        recovery = fresh.service.create_recovery_proposal(None)
        assert fresh.apply(recovery).outcome == "verified"
        assert not fresh.store.has_unresolved_write(binding)
        after = fresh.service.read_snapshot().configuration
        if flow == "recovery":
            assert not proposals.compare_state(start, proposal.operations, after, lighting_systems=ALL_LIGHTING)
        else:
            assert not proposals.compare_state(start, [], after, lighting_systems=ALL_LIGHTING), "recovery did not restore the prior state"
    else:
        assert at_start or at_target, "nothing to recover, yet the keyboard holds a mix"
    if firmware.state["unlocked"] and not started_unlocked:
        fresh.service.lock(None)
    assert not fresh.service.unlock_status(None).unlocked or started_unlocked
    assert plan_change(fresh, SetKeyChange(layer=0, row=1, col=1, keycode="KC_X")).operations


def sweep(args):
    """One worker: every site of one flow and operation in a fresh copy of the prepared state."""
    flow, name, template, proposal_id, sites = args
    failures = []
    fired = 0
    base = pathlib.Path(tempfile.mkdtemp(dir=template.parent))
    for site in sites:
        for fault in FAULTS:
            run = base / "run"
            shutil.copytree(template, run)
            try:
                injector, _, _ = run_case(run, flow, proposal_id, site, fault)
                fired += bool(injector.fired)
            except AssertionError as failure:
                failures.append(f"{flow}/{name}: {fault} at site {site}: {str(failure).splitlines()[0]}")
            except BaseException as failure:  # noqa: BLE001 - a crash in the checks is a finding too
                failures.append(f"{flow}/{name}: {fault} at site {site}: unexpected {failure!r}")
            shutil.rmtree(run)
    shutil.rmtree(base)
    return failures, fired, len(sites) * len(FAULTS)


def test_every_fault_at_every_site_for_every_family_and_flow(tmp_path):
    jobs = []
    for flow in FLOWS:
        for name, change in OPERATIONS.items():
            template = tmp_path / flow / name / "template"
            proposal_id = prepare(template, flow, change)
            clean = tmp_path / flow / name / "clean"
            shutil.copytree(template, clean)
            _, total, outcome = run_case(clean, flow, proposal_id, None, None)
            assert outcome == ("failed_restored" if flow == "restore" else "verified"), (flow, name, outcome)
            assert total > 10, "the sweep must reach the write path"
            jobs.append((flow, name, template, proposal_id, list(range(1, total + 1))))
    context = multiprocessing.get_context("fork" if sys.platform.startswith("linux") else "spawn")
    failures, fired, expected = [], 0, 0
    with ProcessPoolExecutor(max_workers=os.cpu_count(), mp_context=context) as pool:
        for found, count, attempts in pool.map(sweep, jobs):
            failures += found
            fired += count
            expected += attempts
    kinds = Counter(failure.split(": ", 2)[0].split("/")[0] + ": " + failure.split(": ", 2)[2] for failure in failures)
    summary = "\n".join(f"{count} x {kind}" for kind, count in kinds.most_common()) + "\n" + "\n".join(failures[:20])
    assert not failures, f"{len(failures)} of {expected} cases failed:\n{summary}"
    assert fired == expected, f"only {fired} of {expected} injected faults fired"
