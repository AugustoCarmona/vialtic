# SPDX-License-Identifier: GPL-2.0-or-later
"""Command line front end. Human text by default, one JSON envelope on stdout with --json.

Progress and diagnostics go to stderr. The contract is documented in docs/CLI.md.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import sys
from typing import Any

from pydantic import BaseModel, ValidationError

from . import __version__
from .errors import EXIT_APPLY_FAILED, EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, VialticError
from .models import (
    ApplyResult, ChangeSet, DeviceList, DoctorReport, InspectResult, KeycodeList, PUBLIC_SCHEMAS, Proposal, SCHEMA_VERSION,
    SetEncoderChange, SetKeyChange, Snapshot, UnlockStatus, VersionInfo, ViewResult,
)
from .storage import atomic_write, state_root


class UsageError(VialticError):
    def __init__(self, message: str):
        super().__init__("usage_error", message)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise UsageError(message)


def _common(parser: argparse.ArgumentParser, device: bool = True) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON envelope on stdout")
    parser.add_argument("--emulator", metavar="SCENARIO", default=argparse.SUPPRESS,
                        help="use the development emulator instead of USB (also VIALTIC_EMULATOR)")
    if device:
        parser.add_argument("--device", metavar="ID", default=argparse.SUPPRESS,
                            help="device id from `devices list`; required when several keyboards are connected")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="vialtic", description="Inspect and configure USB Vial keyboards through reviewable proposals.")
    parser.add_argument("--version", action="version", version=f"vialtic {__version__}")
    _common(parser, device=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def leaf(subs, name, help_text, device=True):
        command = subs.add_parser(name, help=help_text, description=help_text)
        _common(command, device=device)
        return command

    leaf(sub, "version", "Show version and supported protocols.", device=False)
    schema = leaf(sub, "schema", "Print the JSON schema of a public contract.", device=False)
    schema.add_argument("name", nargs="?", choices=sorted(PUBLIC_SCHEMAS))
    leaf(sub, "doctor", "Diagnose USB access, permissions and protocol versions.", device=False)

    devices = sub.add_parser("devices", help="Discover keyboards.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(devices, "list", "List connected USB Vial keyboards.", device=False)
    device = sub.add_parser("device", help="Inspect one keyboard.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(device, "inspect", "Show identity, capabilities and limits.")

    snapshot = leaf(sub, "snapshot", "Read the complete configuration. Reads only.")
    snapshot.add_argument("--output", metavar="FILE", help="write the snapshot JSON to a file (mode 0600)")
    snapshot.add_argument("--include-macros", action="store_true",
                          help="include macro contents, which can hold typed text; left out by default")

    view = leaf(sub, "view", "Show keys as text or PNG using the keyboard's own geometry.")
    view.add_argument("--layer", type=int, action="append", metavar="N", help="layer to show; repeatable; default all")
    view.add_argument("--format", choices=["text", "png"], default="text")
    view.add_argument("--output", metavar="FILE", help="PNG path; default under the state directory")
    view.add_argument("--proposal", metavar="ID", help="show this proposal's before and after values")
    view.add_argument("--unlock", action="store_true", help="mark the keys to hold for a physical unlock")

    keycodes = leaf(sub, "keycodes", "List keycodes valid for this keyboard.")
    keycodes.add_argument("--query", help="filter by name, label or description")

    plan = leaf(sub, "plan", "Validate changes and store an immutable proposal. Writes nothing to the keyboard.")
    plan.add_argument("--set", dest="set_keys", action="append", metavar="L:R,C=KEYCODE", default=[],
                      help="assign a key, e.g. 1:0,2=LGUI(KC_3); repeatable")
    plan.add_argument("--set-encoder", dest="set_encoders", action="append", metavar="L:I:D=KEYCODE", default=[],
                      help="assign an encoder direction; repeatable")
    plan.add_argument("--changes", metavar="FILE",
                      help="read a change_set JSON file, or - for stdin; needed for every feature except keys and encoders "
                           "(see `vialtic schema change_set`)")

    proposal = sub.add_parser("proposal", help="Read stored proposals.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    show = leaf(proposal, "show", "Show a stored proposal.", device=False)
    show.add_argument("proposal_id")
    show.add_argument("--include-macros", action="store_true",
                      help="include macro contents, which can hold typed text; left out by default")

    apply_cmd = leaf(sub, "apply", "Apply one approved proposal, verify it and keep a backup.")
    apply_cmd.add_argument("proposal_id")
    apply_cmd.add_argument("--user-approved", action="store_true",
                           help="attest that the user explicitly approved this exact proposal; the flag cannot prove it")
    apply_cmd.add_argument("--reconnect-window", type=float, metavar="SECONDS", default=None, help=argparse.SUPPRESS)
    apply_cmd.add_argument("--unlock-timeout", type=float, metavar="SECONDS", default=None,
                           help="how long to wait for a physical unlock (default 60)")

    undo = sub.add_parser("undo", help="Restore the previous configuration.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(undo, "plan", "Propose restoring the one saved previous configuration.")
    recover = sub.add_parser("recover", help="Resolve an interrupted write.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(recover, "plan", "Propose restoring the state saved before an interrupted write.")

    unlock = sub.add_parser("unlock", help="Guided physical unlock.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(unlock, "status", "Show lock state and the keys to hold.")
    start = leaf(unlock, "start", "Begin unlocking for an approved proposal that requires it.")
    start.add_argument("proposal_id")
    start.add_argument("--user-approved", action="store_true", help="attest that the user approved this proposal")
    leaf(unlock, "poll", "Report unlock progress.")
    leaf(sub, "lock", "Lock the keyboard.")

    emulator = sub.add_parser("emulator", help="Development emulator state.").add_subparsers(dest="action", required=True, parser_class=_Parser)
    leaf(emulator, "list", "List emulator scenarios.", device=False)
    leaf(emulator, "reset", "Delete the selected scenario's simulated keyboards and files.", device=False)
    return parser


_KEY_SPEC = re.compile(r"^(\d+):(\d+),(\d+)=(.+)$")
_ENCODER_SPEC = re.compile(r"^(\d+):(\d+):([01])=(.+)$")


MAX_CHANGE_FILE = 4 * 1024 * 1024


def _read_bounded(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as source:
        return source.read(MAX_CHANGE_FILE + 1)


def _changes_from_args(args: argparse.Namespace) -> ChangeSet:
    if args.changes and (args.set_keys or args.set_encoders):
        raise UsageError("Use --changes or --set/--set-encoder, not both.")
    if args.changes:
        try:
            text = sys.stdin.read(MAX_CHANGE_FILE + 1) if args.changes == "-" else _read_bounded(args.changes)
            if len(text) > MAX_CHANGE_FILE:
                raise VialticError("invalid_file", f"The change set is over the {MAX_CHANGE_FILE} byte limit.")
            return ChangeSet.model_validate_json(text)
        except OSError as exc:
            raise VialticError("invalid_file", f"Could not read {args.changes}: {exc}") from exc
        except ValidationError as exc:
            raise VialticError("invalid_request", "The change set does not match the change_set schema.",
                               details={"errors": exc.errors(include_url=False, include_input=False)},
                               next_steps=["Run `vialtic schema change_set`."]) from exc
    changes: list[Any] = []
    for spec in args.set_keys:
        match = _KEY_SPEC.match(spec)
        if not match:
            raise UsageError(f"Invalid --set {spec!r}; expected LAYER:ROW,COL=KEYCODE.")
        changes.append(SetKeyChange(layer=int(match[1]), row=int(match[2]), col=int(match[3]), keycode=match[4]))
    for spec in args.set_encoders:
        match = _ENCODER_SPEC.match(spec)
        if not match:
            raise UsageError(f"Invalid --set-encoder {spec!r}; expected LAYER:INDEX:DIRECTION=KEYCODE.")
        changes.append(SetEncoderChange(layer=int(match[1]), index=int(match[2]), direction=int(match[3]), keycode=match[4]))
    if not changes:
        raise UsageError("Nothing to plan; pass --set, --set-encoder or --changes.")
    return ChangeSet(changes=changes)


def _redact_macros(snapshot: Snapshot) -> Snapshot:
    config = snapshot.configuration
    macros = config.macros.model_copy(update=dict(buffer_b64=None, macros=None, redacted=True))
    return snapshot.model_copy(update={"configuration": config.model_copy(update={"macros": macros}),
                                       "family_fingerprints": redact_fingerprints(snapshot.family_fingerprints)})


def redact_fingerprints(fingerprints: dict[str, str]) -> dict[str, str]:
    from .proposals import redact_fingerprints as redact

    return redact(fingerprints)


def _redacted(proposal: Proposal) -> Proposal:
    from .proposals import redact_proposal

    return redact_proposal(proposal)


def _progress(message: str) -> None:
    print(f"vialtic: {message}", file=sys.stderr, flush=True)


def make_service(args: argparse.Namespace):
    from .service import ServiceOptions, VialticService
    from .storage import Store

    options = ServiceOptions(progress=_progress)
    if getattr(args, "reconnect_window", None) is not None:
        options.reconnect_window = args.reconnect_window
    if getattr(args, "unlock_timeout", None) is not None:
        options.unlock_timeout = args.unlock_timeout
    scenario = _scenario(args)
    if scenario:
        from .emulator import EmulatorBackend, Faults

        directory = state_root() / "emulator" / scenario
        options.reconnect_poll = 0.1
        options.max_retries = 2
        return VialticService(EmulatorBackend(scenario, directory, Faults.from_env()), Store(directory), options)
    from .device import HidBackend

    return VialticService(HidBackend(), Store(state_root()), options)


def _scenario(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "emulator", None)
    if explicit == "":
        raise UsageError("--emulator needs a scenario name; it never silently means real USB.")
    return explicit or os.environ.get("VIALTIC_EMULATOR") or None


# Human output.

def _format_identity(identity) -> str:
    extra = " SIMULATED" if identity.simulated else ""
    return f"{identity.name}{extra} [{identity.device_id}] {identity.vendor_id}:{identity.product_id}"


def _format_devices(result: DeviceList) -> str:
    if not result.devices:
        lines = ["No USB Vial keyboards found."]
    else:
        lines = []
        for entry in result.devices:
            line = f"{entry.status:<20} {_format_identity(entry.identity)}"
            if entry.status == "ready":
                line += f" VIA {entry.identity.via_protocol} / Vial {entry.identity.vial_protocol}"
            lines.append(line)
            if entry.problem:
                lines.append(f"  {entry.problem.message}")
                lines.extend(f"  next: {step}" for step in entry.problem.next_steps)
    lines.extend(f"ignored              {item.name}: {item.reason}" for item in result.ignored)
    return "\n".join(lines)


def _format_inspect(result: InspectResult) -> str:
    caps = result.capabilities
    unlock_keys = ", ".join(f"{p.row},{p.col}" for p in result.unlock.positions) or "none reported"
    lines = [
        _format_identity(result.device),
        f"  VIA protocol {result.device.via_protocol}, Vial protocol {result.device.vial_protocol}, UID {result.device.vial_uid}",
        f"  Identity: {result.device.identity_note}",
        f"  Matrix {caps.rows}x{caps.cols}, {caps.key_count} keys, {caps.layers} layers, {caps.encoder_count} encoders",
        f"  Macros {caps.macro_count} ({caps.macro_memory_bytes} bytes), tap dance {caps.tap_dance_count}, "
        f"combos {caps.combo_count}, key overrides {caps.key_override_count}, alt repeat {caps.alt_repeat_key_count}",
        f"  Lighting {caps.lighting}, QMK settings {len(caps.qmk_settings)}"
        + (f" (+{len(caps.unknown_qmk_settings)} unknown)" if caps.unknown_qmk_settings else ""),
        f"  Writable on this keyboard: {', '.join(caps.writable_operations)}. All families are read and backed up.",
        f"  Unlocked: {'yes' if result.unlock.unlocked else 'no'}; unlock keys: {unlock_keys}",
        f"  Previous configuration saved: {'yes' if result.previous_available else 'no'}",
    ]
    if result.recovery_pending:
        lines.append("  An earlier write did not finish cleanly. Run `vialtic recover plan`.")
    lines.extend(f"  Warning: {w}" for w in result.warnings)
    return "\n".join(lines)


def _format_snapshot(result: Snapshot) -> str:
    config = result.configuration
    lines = [_format_identity(result.device), f"  Read at {result.created_at.isoformat()}",
             f"  Keymap entries {len(config.keymap)}, encoder entries {len(config.encoders)}, layout options {config.layout_options}",
             f"  Macro buffer {config.macros.used_bytes} of {config.macros.memory_bytes} bytes"
             + (" (contents left out; use --include-macros)" if config.macros.redacted else ""),
             f"  Tap dance {len(config.tap_dance)}, combos {len(config.combos)}, key overrides {len(config.key_overrides)}, "
             f"alt repeat {len(config.alt_repeat_keys)}, QMK settings {len(config.settings.values)}, lighting {config.lighting.kind}",
             "  Coverage: " + ", ".join(f"{k}={v}" for k, v in result.coverage.items())]
    lines.extend(f"  Warning: {w}" for w in result.warnings)
    lines.append("  Use --json for values or `vialtic view` for a key map.")
    return "\n".join(lines)


def _format_proposal(result: Proposal) -> str:
    from .proposals import format_proposal

    return (format_proposal(result) + "\n\nNothing has been written. After the user explicitly approves this exact "
            f"proposal, run:\n  vialtic apply {result.id} --user-approved")


def _format_apply(result: ApplyResult) -> str:
    lines = [f"Outcome: {result.outcome}", result.message, f"Keyboard: {_format_identity(result.device)}"]
    lines.extend(f"  [{op.status}] {op.summary}" for op in result.operations)
    if result.differences:
        lines.append(f"Differences reported: {len(result.differences)}")
        lines.extend(f"  {d.family} {d.target}: expected {d.expected}, observed {d.observed}" for d in result.differences[:20])
    if result.backup_path:
        lines.append(f"Backup: {result.backup_path}")
    if result.relock_error:
        lines.append(f"Lock: {result.relock_error}")
    elif result.relocked:
        lines.append("Keyboard locked again.")
    lines.extend(f"Warning: {w}" for w in result.warnings)
    if result.recovery_steps:
        lines.append("Recovery:")
        lines.extend(f"  {step}" for step in result.recovery_steps)
    if result.outcome == "verified":
        lines.append("Revert with `vialtic undo plan`.")
    return "\n".join(lines)


def _format_unlock(result: UnlockStatus) -> str:
    keys = ", ".join(f"{p.row},{p.col}" for p in result.positions) or "none reported"
    state = "unlocked" if result.unlocked else ("unlock in progress" if result.in_progress else "locked")
    counter = f", {result.counter} remaining" if result.counter is not None else ""
    return f"Keyboard is {state}{counter}. Keys to hold: {keys}"


def _format_doctor(result: DoctorReport) -> str:
    lines = [f"vialtic {result.version} on {result.platform}" + (" (emulator)" if result.simulated else "")]
    for check in result.checks:
        lines.append(f"[{check.status}] {check.name}: {check.message}")
        lines.extend(f"    next: {step}" for step in check.next_steps)
    return "\n".join(lines)


def _format_keycodes(result: KeycodeList) -> str:
    return "\n".join(f"{kc.keycode:<26} {kc.label:<18} {kc.description or ''}".rstrip() for kc in result.keycodes) or "No matching keycodes."


def _format_view(result: ViewResult) -> str:
    return result.text + (f"\nPNG: {result.artifact_path}" if result.artifact_path else "")


def _format_version(result: VersionInfo) -> str:
    return (f"vialtic {result.version}\nVial GUI revision {result.vial_gui_revision}\n"
            f"Supported protocols: VIA {result.supported_via_protocols}, Vial {result.supported_vial_protocols}\n"
            f"Python {result.python}, {result.platform}, {'packaged' if result.frozen else 'source'}")


FORMATTERS = {
    DeviceList: _format_devices, InspectResult: _format_inspect, Snapshot: _format_snapshot, Proposal: _format_proposal,
    ApplyResult: _format_apply, UnlockStatus: _format_unlock, DoctorReport: _format_doctor, KeycodeList: _format_keycodes,
    ViewResult: _format_view, VersionInfo: _format_version,
}


def emit(args: argparse.Namespace, result: BaseModel, ok: bool = True) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": ok, "result": result.model_dump(mode="json")}, indent=2))
    else:
        print(FORMATTERS[type(result)](result))


def emit_error(args: argparse.Namespace | None, error: VialticError) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "ok": False,
               "error": {"code": error.code, "message": error.message, "details": error.details,
                         "next_steps": error.next_steps}}
    if args is not None and getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
        return
    print(f"vialtic: error [{error.code}]: {error.message}", file=sys.stderr)
    for step in error.next_steps:
        print(f"  next: {step}", file=sys.stderr)


def _approval(service, proposal_id: str, flag: bool):
    from .service import attestation_for

    if not flag:
        return None
    return attestation_for(service.get_proposal(proposal_id), service.options.now())


def run(args: argparse.Namespace) -> int:
    command = args.command
    action = getattr(args, "action", None)
    selector = getattr(args, "device", None)

    if command == "schema":
        if args.name:
            print(json.dumps(PUBLIC_SCHEMAS[args.name].model_json_schema(), indent=2))
        else:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "schemas": sorted(PUBLIC_SCHEMAS)}, indent=2))
        return EXIT_OK

    if command == "emulator":
        from .emulator import SCENARIOS

        if action == "list":
            print(json.dumps({"schema_version": SCHEMA_VERSION, "scenarios": sorted(SCENARIOS)}, indent=2)
                  if getattr(args, "json", False) else "\n".join(sorted(SCENARIOS)))
            return EXIT_OK
        scenario = _scenario(args)
        if not scenario:
            raise UsageError("Choose the scenario to reset with --emulator.")
        if scenario not in SCENARIOS:
            raise VialticError("invalid_request", f"Unknown emulator scenario {scenario!r}.")
        shutil.rmtree(state_root() / "emulator" / scenario, ignore_errors=True)
        print(f"Reset emulator scenario {scenario}.")
        return EXIT_OK

    service = make_service(args)

    if command == "version":
        emit(args, service.version())
    elif command == "doctor":
        emit(args, service.diagnose())
    elif command == "devices":
        emit(args, service.list_devices())
    elif command == "device":
        emit(args, service.inspect_device(selector))
    elif command == "snapshot":
        snapshot = service.read_snapshot(selector)
        if not args.include_macros:
            snapshot = _redact_macros(snapshot)
        if args.output:
            atomic_write(pathlib.Path(args.output), snapshot.model_dump_json(indent=2).encode(), own_directory=False)
            _progress(f"Snapshot written to {args.output}")
        emit(args, snapshot)
    elif command == "view":
        emit(args, service.render(selector, args.layer, args.format, args.output, args.proposal, args.unlock))
    elif command == "keycodes":
        emit(args, service.keycodes(selector, args.query))
    elif command == "plan":
        emit(args, _redacted(service.create_proposal(selector, _changes_from_args(args))))
    elif command == "proposal":
        proposal = service.get_proposal(args.proposal_id)
        emit(args, proposal if args.include_macros else _redacted(proposal))
    elif command == "apply":
        signal.signal(signal.SIGTERM, _raise_interrupt)
        result = service.apply_proposal(args.proposal_id, _approval(service, args.proposal_id, args.user_approved), selector)
        emit(args, result, ok=result.outcome == "verified")
        return EXIT_OK if result.outcome == "verified" else EXIT_APPLY_FAILED
    elif command == "undo":
        emit(args, _redacted(service.create_undo_proposal(selector)))
    elif command == "recover":
        emit(args, _redacted(service.create_recovery_proposal(selector)))
    elif command == "unlock":
        if action == "status":
            emit(args, service.unlock_status(selector))
        elif action == "start":
            emit(args, service.unlock_start(args.proposal_id, _approval(service, args.proposal_id, args.user_approved), selector))
        else:
            emit(args, service.unlock_poll(selector))
    elif command == "lock":
        emit(args, service.lock(selector))
    return EXIT_OK


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = None
    json_requested = "--json" in (argv if argv is not None else sys.argv[1:])
    try:
        args = build_parser().parse_args(argv)
        return run(args)
    except VialticError as error:
        if args is None and json_requested:
            args = argparse.Namespace(json=True)
        emit_error(args, error)
        return error.exit_code
    except KeyboardInterrupt:
        emit_error(args, VialticError("cancelled", "Cancelled."))
        return VialticError("cancelled", "").exit_code
    except BrokenPipeError:
        return EXIT_USAGE
    except OSError as exc:
        emit_error(args, VialticError("storage_failed", f"A local file operation failed: {exc}"))
        return VialticError("storage_failed", "").exit_code
    except Exception as exc:
        emit_error(args, VialticError("internal_error", f"Unexpected {type(exc).__name__}: {exc}"))
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
