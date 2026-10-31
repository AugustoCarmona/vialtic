# CLI contract

This documents the commands that exist today. Run `vialtic <command> --help` for all options. The planned command families in SPEC.md that are missing here (`profile`, `import`, `export`, `mcp serve`, `update`) are not implemented yet.

## Conventions

- `--json` prints exactly one JSON envelope on stdout. Progress and diagnostics go to stderr.
- `--device <id>` selects a keyboard. Ids come from `devices list` and are only valid until the keyboard is replugged. With several connected keyboards, commands refuse to guess.
- `--emulator <scenario>` (or `VIALTIC_EMULATOR`) uses the development emulator. It is never used unless requested, and its results are marked `simulated`.

Success: `{"schema_version": 1, "ok": true, "result": {...}}`. Failure: `{"schema_version": 1, "ok": false, "error": {"code", "message", "details", "next_steps"}}`. An `apply` that ran but did not verify returns `ok: false` with a `result`, not an `error`. `vialtic schema [name]` prints the JSON schema of each contract; checked-in copies live in `tests/schemas/`.

## Commands

| Command | Writes to the keyboard | Purpose |
| --- | --- | --- |
| `version`, `schema [name]` | no | Version, supported protocols, contract schemas. |
| `doctor` | no | HID access, permissions, protocol versions, pending recovery. |
| `devices list`, `device inspect` | no | Discovery, identity and confidence, capabilities, limits, lock state. |
| `snapshot [--output FILE] [--include-macros]` | no | Complete configuration including lighting. Macro contents are left out unless requested. |
| `view [--layer N] [--format text\|png] [--output FILE] [--proposal ID] [--unlock]` | no | Physical key map from firmware geometry; before and after values for a proposal. |
| `keycodes [--query TEXT]` | no | Keycodes valid for this keyboard. |
| `plan --set L:R,C=KEYCODE ... \| --set-encoder L:I:D=KEYCODE ... \| --changes FILE` | no | Validates changes and stores an immutable proposal. |
| `proposal show ID` | no | Shows a stored proposal. |
| `apply ID --user-approved` | **yes** | Rechecks, backs up, writes, reads back, verifies. |
| `undo plan`, `recover plan` | no | Propose restoring the previous state or the state before an interrupted write. |
| `unlock status`, `unlock poll` | no configuration | Lock state and progress. |
| `unlock start ID --user-approved`, `lock` | no configuration | Physical unlock for an approved proposal that needs it; locking. |
| `emulator list`, `emulator reset --emulator NAME` | no | Development emulator scenarios and state. |

Keymap keys and encoders are the only writable features in this build. Every other family is read and backed up, and changes that would require writing one are refused (`restore_unsupported`).

### Change files

```json
{"schema_version": 1, "changes": [
  {"type": "set_key", "layer": 1, "row": 0, "col": 2, "keycode": "LGUI(KC_3)"},
  {"type": "set_encoder", "layer": 0, "index": 0, "direction": 1, "keycode": "KC_VOLU"}
]}
```

Keycodes use Vial/QMK names (`KC_A`, `MO(1)`, `LT(2, KC_SPC)`, `LGUI(KC_3)`, `LSFT_T(KC_F)`) or a hex value for unnamed custom codes. Input is parsed by a strict grammar; anything else is refused, never mapped to `KC_NO` as upstream's own decoder does.

## The approval contract

`apply` needs `--user-approved`, which states that the user explicitly approved this exact proposal. **The flag is an attestation by the caller. It cannot prove a person consented, and clients must uphold this contract:** show the proposal, wait for explicit approval, and only then pass the flag. New or changed operations need a new proposal. A proposal is single use and expires after 30 minutes.

Before writing, `apply` takes an OS lock and rechecks identity, protocol versions and the keyboard's configuration against the proposal, refusing if anything changed (`proposal_stale`). If a physical unlock is needed, it repeats those checks and the expiry check after the keys are released, and locks again if it refuses. It then saves the configuration durably and only then opens the write gate. Once read-back passes, the write stands: a later interruption finishes the backup rotation rather than rolling back.

## Apply outcomes

| Outcome | Meaning | Exit |
| --- | --- | --- |
| `verified` | All writes landed and read back correctly. The prior state is now the one saved previous configuration. | 0 |
| `failed_restored` | The write failed; the saved state was restored and verified. | 10 |
| `partial` | The write failed and restoring did not complete. The keyboard may hold a mix. The saved state is kept. | 10 |
| `unknown` | The write failed and the keyboard could not be read. The saved state is kept. | 10 |

After `partial`, `unknown` or a crash, new plans are refused (`recovery_pending`) until `recover plan` and an approved `apply` resolve it. Features this build cannot write are left as they are at that point, and the recovery proposal says so. Nothing writes automatically on startup. Automatic restore runs only inside the failing `apply`, is disclosed in the proposal, and stops if identity is ambiguous, permissions fail, an unlock is needed or 30 seconds pass.

## Exit codes

| Code | Meaning | Error codes |
| --- | --- | --- |
| 0 | Success | |
| 1 | Internal error | `internal_error` |
| 2 | Usage | `usage_error` |
| 3 | Device selection | `no_device`, `device_not_found`, `device_selection_required`, `device_ambiguous` |
| 4 | Permission | `permission_denied` |
| 5 | Busy | `device_busy`, `lock_timeout` |
| 6 | Unsupported | `unsupported_protocol`, `unsupported_operation`, `malformed_device_data`, `restore_unsupported`, `write_blocked`, `hid_backend_unavailable` |
| 7 | Invalid request | `invalid_request`, `invalid_keycode`, `invalid_coordinate`, `invalid_layer_reference`, `no_changes`, `nothing_to_undo`, `nothing_to_recover`, `invalid_file` |
| 8 | Approval | `approval_required`, `unlock_required` |
| 9 | Stale | `proposal_not_found`, `proposal_expired`, `proposal_stale`, `proposal_tampered`, `device_changed` |
| 10 | Apply did not verify | outcome `failed_restored`, `partial` or `unknown` |
| 11 | Recovery pending | `recovery_pending` |
| 12 | Interrupted | `cancelled`, `unlock_timeout` |
| 13 | Communication | `device_communication_failed` |
| 14 | Backup or storage | `backup_failed`, `storage_failed` |

## Local files

Linux: `$XDG_STATE_HOME/vialtic`, falling back to `~/.local/state/vialtic`. macOS: `~/Library/Application Support/Vialtic/state`. Files are owner-only.

| Path | Purpose |
| --- | --- |
| `devices/<binding>/previous.json` | The one automatic previous configuration. Holds macro contents, like any complete backup. |
| `devices/<binding>/pending.json` | Baseline while a write is unresolved. Removed once resolved. |
| `devices/<binding>/recovery.json` | Progress of an unresolved write. No key data. |
| `proposals/`, `renders/` | Temporary and bounded. |
| `locks/` | Advisory OS locks. |
| `emulator/<scenario>/` | The same tree for one emulator scenario, isolated from real keyboards. |

## Limitations

- Identical keyboards with only the shared Vial serial marker cannot be told apart. Corne firmware is an example. Identity reports `model_and_firmware` and backups are per identity. Writing needs an explicit `--device` if the USB path changed since planning, and is refused if another unit still sits at the original path. Automatic restore stops whenever the keyboard is not at the same path or holds values neither the backup nor the write explain, so after such a replug use `recover plan`.
- Other software such as Vial or VIA is not locked out. Close it before applying; changes it makes are caught by baseline checks and read-back.
- QMK settings unknown to the pinned Vial tables are reported, including in proposals, but not read or backed up. This does not block keymap writes, because this build cannot write settings at all; once settings become writable (M3), the backup rule must be revisited.
- Lighting state is read and backed up but not restored, and lighting differences are reported as warnings.
- Cancelling a physical unlock cannot always stop it in firmware. Replug the keyboard if keys stop responding.

## Emulator scenarios

`generic` (encoder, layout option, VialRGB), `split` (rotated thumb keys, RGB light), `multi` (two keyboards), `twins` (identical identity), `serial` (unique USB serial), `legacy` (Vial protocol 5, backlight), `future` (unsupported protocol). Development fault injection uses `VIALTIC_EMULATOR_FAULTS`, a JSON object with the fields of `Faults` in `src/vialtic/emulator.py`. Emulator results do not establish physical compatibility.
