# SPDX-License-Identifier: GPL-2.0-or-later
"""CLI contract: clean JSON on stdout, documented exit codes, explicit emulator activation."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from vialtic import cli, errors


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # macOS paths ignore XDG variables
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("VIALTIC_EMULATOR", raising=False)
    monkeypatch.delenv("VIALTIC_EMULATOR_FAULTS", raising=False)


def as_json(capsys, *argv):
    code, out, _ = run(capsys, *argv, "--json")
    return code, json.loads(out)


def test_version_and_schema_discovery(capsys):
    code, data = as_json(capsys, "version")
    assert code == 0 and data["schema_version"] == 1 and data["ok"]
    assert 6 in data["result"]["supported_vial_protocols"]
    code, out, _ = run(capsys, "schema")
    assert "proposal" in json.loads(out)["schemas"]
    code, out, _ = run(capsys, "schema", "change_set")
    assert json.loads(out)["properties"]["changes"]["maxItems"] == 4096


def test_usage_error_exit_code_and_json(capsys):
    code, data = as_json(capsys, "plan", "--emulator", "split")
    assert code == errors.EXIT_USAGE and not data["ok"] and data["error"]["code"] == "usage_error"
    code, out, err = run(capsys, "plan", "--emulator", "split", "--set", "not a spec")
    assert code == errors.EXIT_USAGE and out == "" and "Invalid --set" in err
    code, out, err = run(capsys, "frobnicate")
    assert code == errors.EXIT_USAGE and "frobnicate" in err


def test_emulator_needs_explicit_activation(capsys, monkeypatch):
    class NoDevices:
        simulated = False

        def enumerate(self):
            return []

        def open(self, path):
            raise OSError

    monkeypatch.setattr("vialtic.device.HidBackend", NoDevices)
    code, data = as_json(capsys, "devices", "list")
    assert code == 0 and data["result"]["devices"] == [] and data["result"]["simulated"] is False
    code, data = as_json(capsys, "snapshot")
    assert code == errors.EXIT_DEVICE_SELECTION and data["error"]["code"] == "no_device"
    # Scenario names never reach the hardware path, and nothing fell back to simulation.
    code, data = as_json(capsys, "snapshot", "--emulator", "split")
    assert code == 0 and data["result"]["device"]["simulated"] is True


def test_empty_emulator_value_never_means_real_usb(capsys, monkeypatch):
    def fail():
        raise AssertionError("the hardware backend must not be built")

    monkeypatch.setattr("vialtic.device.HidBackend", fail)
    code, data = as_json(capsys, "devices", "list", "--emulator", "")
    assert code == errors.EXIT_USAGE and data["error"]["code"] == "usage_error"


def test_full_journey_with_exit_codes(capsys, tmp_path):
    em = ("--emulator", "split")
    code, data = as_json(capsys, "devices", "list", *em)
    assert code == 0 and data["result"]["devices"][0]["status"] == "ready"

    code, data = as_json(capsys, "plan", *em, "--set", "1:0,2=LGUI(KC_3)")
    assert code == 0
    proposal_id = data["result"]["id"]
    assert data["result"]["operations"][0]["after"]["keycode"] == "LGUI(KC_3)"

    code, data = as_json(capsys, "proposal", "show", proposal_id, *em)
    assert code == 0 and data["result"]["id"] == proposal_id

    code, data = as_json(capsys, "apply", proposal_id, *em)
    assert code == errors.EXIT_APPROVAL and data["error"]["code"] == "approval_required"

    code, data = as_json(capsys, "apply", proposal_id, "--user-approved", *em)
    assert code == 0 and data["ok"] and data["result"]["outcome"] == "verified"

    code, data = as_json(capsys, "apply", proposal_id, "--user-approved", *em)
    assert code == errors.EXIT_STALE and data["error"]["code"] == "proposal_not_found"

    code, data = as_json(capsys, "undo", "plan", *em)
    assert code == 0
    code, data = as_json(capsys, "apply", data["result"]["id"], "--user-approved", *em)
    assert code == 0 and data["result"]["outcome"] == "verified"

    code, out, err = run(capsys, "view", *em, "--layer", "1")
    assert code == 0 and "layer 1" in out and "SIMULATED" in out

    target = tmp_path / "out.png"
    code, data = as_json(capsys, "view", *em, "--layer", "0", "--format", "png", "--output", str(target))
    assert code == 0 and target.exists() and data["result"]["artifact_path"] == str(target)


def test_plan_from_change_file_and_stdin_errors(capsys, tmp_path):
    em = ("--emulator", "generic")
    path = tmp_path / "changes.json"
    path.write_text(json.dumps({"schema_version": 1, "changes": [
        {"type": "set_key", "layer": 0, "row": 0, "col": 0, "keycode": "KC_Z"},
        {"type": "set_encoder", "layer": 0, "index": 0, "direction": 0, "keycode": "KC_VOLD"}]}))
    code, data = as_json(capsys, "plan", *em, "--changes", str(path))
    assert code == 0 and len(data["result"]["operations"]) == 2

    path.write_text(json.dumps({"schema_version": 1, "changes": [{"type": "raw_packet"}]}))
    code, data = as_json(capsys, "plan", *em, "--changes", str(path))
    assert code == errors.EXIT_INVALID and data["error"]["code"] == "invalid_request"

    path.write_text(json.dumps({"schema_version": 99, "changes": [
        {"type": "set_key", "layer": 0, "row": 0, "col": 0, "keycode": "KC_Z"}]}))
    code, data = as_json(capsys, "plan", *em, "--changes", str(path))
    assert code == errors.EXIT_INVALID

    code, data = as_json(capsys, "plan", *em, "--changes", str(tmp_path / "missing.json"))
    assert code == errors.EXIT_INVALID and data["error"]["code"] == "invalid_file"


def test_several_keyboards_exit_code_lists_candidates(capsys):
    code, data = as_json(capsys, "snapshot", "--emulator", "multi")
    assert code == errors.EXIT_DEVICE_SELECTION and data["error"]["code"] == "device_selection_required"
    assert len(data["error"]["details"]["candidates"]) == 2


def test_macro_contents_are_left_out_by_default(capsys, tmp_path):
    em = ("--emulator", "split")
    code, data = as_json(capsys, "snapshot", *em)
    macros = data["result"]["configuration"]["macros"]
    assert macros["redacted"] is True and macros["buffer_b64"] is None and macros["macros"] is None
    code, data = as_json(capsys, "snapshot", *em, "--include-macros")
    assert data["result"]["configuration"]["macros"]["buffer_b64"]

    target = tmp_path / "s.json"
    run(capsys, "snapshot", *em, "--output", str(target))
    assert json.loads(target.read_text())["configuration"]["macros"]["redacted"] is True
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_failed_apply_outcome_uses_apply_failed_exit_code(capsys, monkeypatch):
    em = ("--emulator", "split")
    code, data = as_json(capsys, "plan", *em, "--set", "1:0,2=KC_A", "--set", "1:0,3=KC_B")
    proposal_id = data["result"]["id"]
    monkeypatch.setenv("VIALTIC_EMULATOR_FAULTS", json.dumps({"disconnect_after_writes": 1}))
    code, out, err = run(capsys, "apply", proposal_id, "--user-approved", "--reconnect-window", "0.05", *em, "--json")
    data = json.loads(out)
    assert code == errors.EXIT_APPLY_FAILED and not data["ok"] and data["result"]["outcome"] == "unknown"
    assert "Write failed" in err, "progress belongs on stderr"

    monkeypatch.delenv("VIALTIC_EMULATOR_FAULTS")
    code, data = as_json(capsys, "plan", *em, "--set", "0:0,0=KC_Z")
    assert code == errors.EXIT_RECOVERY_PENDING and data["error"]["code"] == "recovery_pending"
    code, data = as_json(capsys, "recover", "plan", *em)
    assert code == 0 and data["result"]["proposal_type"] == "recovery"
    code, data = as_json(capsys, "apply", data["result"]["id"], "--user-approved", *em)
    assert code == 0 and data["result"]["outcome"] == "verified"


def test_human_output_for_every_read_command(capsys):
    em = ("--emulator", "generic")
    for argv in (("doctor",), ("devices", "list"), ("device", "inspect"), ("snapshot",), ("keycodes", "--query", "volu"),
                 ("unlock", "status"), ("version",)):
        code, out, _ = run(capsys, *argv, *em)
        assert code == 0 and out.strip(), argv
    code, out, _ = run(capsys, "doctor", *em)
    assert "emulator" in out


def test_emulator_list_and_reset(capsys):
    code, out, _ = run(capsys, "emulator", "list")
    assert "split" in out
    run(capsys, "plan", "--emulator", "split", "--set", "1:0,2=KC_A")
    code, out, _ = run(capsys, "emulator", "reset", "--emulator", "split")
    assert code == 0
    code, out, err = run(capsys, "emulator", "reset")
    assert code == errors.EXIT_USAGE


def test_module_entrypoint_keeps_stdout_clean():
    env = dict(os.environ, XDG_STATE_HOME=str(pathlib.Path(os.environ["XDG_STATE_HOME"])))
    output = subprocess.run([sys.executable, "-m", "vialtic", "devices", "list", "--emulator", "generic", "--json"],
                            capture_output=True, text=True, env=env, check=True)
    assert json.loads(output.stdout)["ok"] is True
