# CLI contract

This documents the commands that exist today. Run `vialtic <command> --help` for all options. The planned command families in SPEC.md that are missing here (`profile`, `import`, `export`, `mcp serve`, `update`) are not implemented yet. Feature coverage is in [`CAPABILITIES.md`](CAPABILITIES.md).

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
| `plan --set L:R,C=KEYCODE ... \| --set-encoder L:I:D=KEYCODE ... \| --changes FILE` | no | Validates changes for any family and stores an immutable proposal. |
| `proposal show ID [--include-macros]` | no | Shows a stored proposal. |
| `apply ID --user-approved` | **yes** | Rechecks, backs up, writes, reads back, verifies. |
| `undo plan`, `recover plan` | no | Propose restoring the previous state or the state before an interrupted write. |
| `unlock status`, `unlock poll` | no configuration | Lock state and progress. |
| `unlock start ID --user-approved`, `lock` | no configuration | Physical unlock for an approved proposal that needs it; locking. |
| `emulator list`, `emulator reset --emulator NAME` | no | Development emulator scenarios and state. |

Every family in [`CAPABILITIES.md`](CAPABILITIES.md) is writable when the firmware exposes it. `device inspect` lists what a keyboard takes under `writable_operations`. A request for something it lacks is refused with `capability_unavailable`. Physical unlock is declared in the proposal for every macro write and for anything that writes `QK_BOOT`.

### Change files

`--set` and `--set-encoder` cover keys and encoders. Everything else goes through `--changes FILE`, or `-` for stdin. Files are limited to 4 MiB. In each entry, fields left out keep their current value.

```json
{"schema_version": 1, "changes": [
  {"type": "set_key", "layer": 1, "row": 0, "col": 2, "keycode": "LGUI(KC_3)"},
  {"type": "set_encoder", "layer": 0, "index": 0, "direction": 1, "keycode": "KC_VOLU"},
  {"type": "set_layout_option", "index": 1, "choice": 2},
  {"type": "set_tap_dance", "index": 0, "on_tap": "KC_A", "on_hold": "KC_LCTL", "on_double_tap": "KC_ESC", "on_tap_hold": "KC_NO", "tapping_term": 200},
  {"type": "set_combo", "index": 0, "keys": ["KC_A", "KC_B"], "output": "KC_ESC"},
  {"type": "set_key_override", "index": 0, "trigger": "KC_BSPC", "replacement": "KC_DEL", "layers": 65535, "trigger_mods": 2, "negative_mod_mask": 0, "suppressed_mods": 2, "options": 135},
  {"type": "set_alt_repeat_key", "index": 0, "keycode": "KC_A", "alt_keycode": "KC_B", "allowed_mods": 0, "options": 8},
  {"type": "set_qmk_setting", "qsid": 7, "value": 175},
  {"type": "set_qmk_setting", "qsid": 21, "bit": 7, "enabled": true},
  {"type": "set_macro", "index": 0, "actions": [["text", "hello"], ["tap", "LCTL(KC_A)"], ["delay", 100], ["down", "KC_LSFT"], ["up", "KC_LSFT"]]},
  {"type": "set_lighting", "system": "vialrgb", "save": true, "mode": 3, "speed": 90, "hue": 16, "saturation": 32, "value": 64}
]}
```

Rules worth knowing:

- **Layout options:** `index` is the position in `layout_option_labels` and `choice` an alternative within it. A plain label takes 0 or 1. Bits no option claims are kept.
- **Key override `options`:** bit 0 trigger down, 1 required mod down, 2 negative mod up, 3 one mod, 4 no re-register, 5 no unregister on other key, 7 enabled. Bit 6 and any other bit Vial does not name can be kept but not changed. `layers` has one bit per layer; 65535 means all.
- **Alternate repeat `options`:** bit 0 default to this key, 1 bidirectional, 2 ignore handedness, 3 enabled. Unnamed bits behave the same way.
- **Combos:** `keys` replaces all four inputs. A `KC_NO` before another input gets a warning.
- **QMK settings:** give `value`, or `bit` and `enabled` for one flag. Ranges and flags come from the pinned table. Bits the table does not define can be kept but not changed. Settings unknown to the table are refused, as are ones the firmware refuses to report.
- **Macros:** one macro is replaced at a time; the others keep their exact bytes, even malformed ones. The unused rest of the buffer is zeroed and read back, so a cleared macro's text does not stay in keyboard memory. A macro write therefore sends the whole buffer, however small the edit. Text may hold printable ASCII, tabs and newlines. `KC_NO` is refused because it would encode a zero byte, which ends a macro. Delays need Vial protocol 2 or newer, and 16-bit keycodes need protocol 5 or newer. A result over the buffer size is refused with `capacity_exceeded`, never truncated.
- **Lighting:** `save` is required. `true` sends the firmware's one save command after the values verify. It saves all lighting, not just this system, and with no other field it makes the current values permanent. `false` lasts until the keyboard loses power. A request cannot mix them. Backlight `effect` is 0 or 1.

### Macro contents

Macro text can hold anything a person typed, so contents stay out of default output: `plan`, `undo plan`, `recover plan`, `proposal show` and `snapshot` show which macros change and how many bytes, not what they hold. Errors and verification differences also leave contents out. The buffers are in the proposal, backup and any file you ask for, all mode 0600. **Before asking for approval of a proposal that changes macros, show the user `vialtic proposal show ID --include-macros`.** The before and after shown there are a parsed preview, which can hide malformed bytes; the raw buffer is what gets written.

Keycodes use Vial/QMK names (`KC_A`, `MO(1)`, `LT(2, KC_SPC)`, `LGUI(KC_3)`, `LSFT_T(KC_F)`) or a hex value for unnamed custom codes. Input is parsed by a strict grammar; anything else is refused, never mapped to `KC_NO` as upstream's own decoder does. Arguments that do not fit, such as `LT(1, LCTL(KC_A))` or `LSFT(MO(1))`, are refused rather than masked. Change files are strictly typed: `true` is not a number and `"0"` is not a layer.

## The approval contract

`apply` needs `--user-approved`, which states that the user explicitly approved this exact proposal. **The flag is an attestation by the caller. It cannot prove a person consented, and clients must uphold this contract:** show the proposal, wait for explicit approval, and only then pass the flag. New or changed operations need a new proposal. A proposal is single use, whatever the outcome of applying it, and expires after 30 minutes.

Before writing, `apply` takes an OS lock and rechecks identity, protocol versions and the keyboard's configuration against the proposal, refusing if anything changed (`proposal_stale`). Lighting is part of that check only when the proposal changes it, because lighting keys change it at any moment. A write also refuses to start if it would replace a `previous.json` this version cannot read (`invalid_file`); the file is kept, and moving it aside by hand is the way forward. If a physical unlock is needed, it repeats those checks and the expiry check after the keys are released, and locks again if it refuses. It then saves the configuration durably and only then opens the write gate. Once read-back passes, the write stands: a later interruption finishes the backup rotation rather than rolling back.

## Apply outcomes

| Outcome | Meaning | Exit |
| --- | --- | --- |
| `verified` | All writes landed and read back correctly. The prior state is now the one saved previous configuration. | 0 |
| `failed_restored` | The write failed; the saved state was restored and verified. | 10 |
| `partial` | The write failed and restoring did not complete. The keyboard may hold a mix. The saved state is kept. | 10 |
| `unknown` | The write failed and the keyboard could not be read. The saved state is kept. | 10 |

After `partial`, `unknown` or a crash, new plans are refused (`recovery_pending`) until `recover plan` and an approved `apply` resolve it. Nothing writes automatically on startup. Automatic restore runs only inside the failing `apply`, is disclosed in the proposal, and stops if identity is ambiguous, permissions fail, an unlock is needed or 30 seconds pass.

A replug locks the firmware, so a macro write or anything holding `QK_BOOT` that fails with a disconnect cannot be restored automatically: nobody is holding the unlock keys at that point. The outcome is `partial`, the saved state is kept, and `recover plan` produces a proposal whose `apply` asks for the unlock. If that unlock times out, the saved state is still kept; replug the keyboard and retry.

### Undo and recovery

- Undo restores every family that differs from the saved previous state, and lists them in its proposal. After it, the replaced configuration becomes the single previous state.
- **Lighting is the exception:** undo and recovery restore only the lighting systems the reversed write changed, repeating that write's save choice. A recovery saves only if the interrupted write got as far as sending its save. Any other difference is probably someone pressing a lighting key, and it is left alone and reported.
- A family is left out when the firmware no longer has the same entries as the saved state, which can happen when it is rebuilt with the same definition. Undo refuses (`restore_unsupported`). Recovery restores the rest and says so, because refusing would lock the keyboard out of all further plans.
- Undo and recovery refuse (`device_changed`) if the firmware crossed Vial protocol 2, 5 or 6 since the backup, because saved keycodes or macro bytes mean something else across those boundaries. For recovery the message says how to continue: Vialtic keeps the backup, and you move it aside by hand once you have checked the keyboard.

## Exit codes

| Code | Meaning | Error codes |
| --- | --- | --- |
| 0 | Success | |
| 1 | Internal error | `internal_error` |
| 2 | Usage | `usage_error` |
| 3 | Device selection | `no_device`, `device_not_found`, `device_selection_required`, `device_ambiguous` |
| 4 | Permission | `permission_denied` |
| 5 | Busy | `device_busy`, `lock_timeout` |
| 6 | Unsupported | `unsupported_protocol`, `unsupported_operation`, `capability_unavailable`, `malformed_device_data`, `restore_unsupported`, `write_blocked`, `hid_backend_unavailable` |
| 7 | Invalid request | `invalid_request`, `invalid_keycode`, `invalid_coordinate`, `invalid_layer_reference`, `no_changes`, `nothing_to_undo`, `nothing_to_recover`, `invalid_file`, `capacity_exceeded` |
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
| `devices/<binding>/previous.json` | The one automatic previous configuration. Holds macro contents, like any complete backup. Files written by 0.1.0 stay readable. |
| `devices/<binding>/pending.json` | Baseline while a write is unresolved. Removed once resolved. |
| `devices/<binding>/recovery.json` | Progress of an unresolved write. No key data. |
| `proposals/`, `renders/` | Temporary and bounded. |
| `locks/` | Advisory OS locks. |
| `emulator/<scenario>/` | The same tree for one emulator scenario, isolated from real keyboards. |

## Limitations

- Identical keyboards with only the shared Vial serial marker cannot be told apart. Corne firmware is an example. Identity reports `model_and_firmware` and backups are per identity. Writing needs an explicit `--device` if the USB path changed since planning, and is refused if another unit still sits at the original path. Automatic restore stops whenever the keyboard is not at the same path or holds values neither the backup nor the write explain, so after such a replug use `recover plan`.
- Other software such as Vial or VIA is not locked out. Close it before applying; changes it makes are caught by baseline checks and read-back.
- QMK settings unknown to the pinned Vial tables cannot be written, because their width is unknown. Their raw answers are kept with the backup, and undo reports if one moved, but cannot put it back. Nothing Vialtic sends touches them, so they do not block other writes.
- The firmware reports only current lighting values and has one save command for all lighting. Vialtic cannot read saved values, cannot tell if current values are saved, and cannot confirm a save reached permanent storage. So undoing a saved lighting change restores and saves the values read just before it; if those were themselves unsaved, they are now saved.
- Firmware may clamp values, such as a lighting brightness limit. Read-back then fails the apply and the prior values are restored.
- While a physical unlock is in progress, firmware ignores every command except unlock status and polling, and only ending the unlock or a replug clears it. Every other command, including `lock`, answers `unlock_in_progress`. Cancelling or timing out cannot stop it from the host, so replug the keyboard before retrying.
- A backup holds what was read from a keyboard identified by model and firmware. If its USB path changed, `undo plan` and `recover plan` refuse (`device_ambiguous`) until you confirm it is the same unit with `--device`, and they keep refusing while another keyboard with that identity sits at the original path. Unlock tracking is per USB path, with one marker per identity, so two identical keyboards unlocked by Vialtic at the same time are not both tracked.
- Backlight brightness is a level in firmware (`level = value * levels / 255`), and the number of levels is not reported. Any other value fails read-back and is reported with what the firmware kept. Asking for that reported value works.
- The firmware treats RGB light effect 0 as off and ignores colour changes while off, so a colour change on an RGB light that stays off fails read-back. It also ignores brightness while breathing and hue in the rainbow effects, so a colour change on an RGB light passes briefly through the static effect, and you may see it flicker.

## Emulator scenarios

`generic` (encoder, layout option, VialRGB), `split` (rotated thumb keys, RGB light), `multi` (two keyboards), `twins` (identical identity), `serial` (unique USB serial), `legacy` (Vial protocol 5, backlight), `future` (unsupported protocol), `full` (every family and every QMK setting), `dual` (backlight and RGB light), `minimal` (keys only), `vintage` (Vial protocol 1). Development fault injection uses `VIALTIC_EMULATOR_FAULTS`, a JSON object with the fields of `Faults` in `src/vialtic/emulator.py`. Emulator results do not establish physical compatibility.
