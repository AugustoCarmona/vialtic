# Capability matrix

Maps every configuration family in SPEC section 4 to the pinned Vial GUI revision (`aef8222a2d0429a183b2ed692d5f9efcfd383f08`) and to Vialtic's behavior. Paths are under `vendor/vial/upstream/`. "Emulator" means tested against the wire-level emulator in `src/vialtic/emulator.py`; it says nothing about physical keyboards. Hardware evidence is tracked in `docs/STATUS.md` and is the owner's to produce.

## Configuration families

| Family | Upstream read | Upstream write | Vialtic operation | Capability source | Validation | Physical unlock | Verification | Backup and restore |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Keymap | `keyboard_comm.reload_keymap` | `set_key` | `set_key` | definition, layer count | coordinates, strict keycode grammar, layer and macro references | `QK_BOOT` | read-back by raw code | per key, raw code |
| Encoders | `reload_keymap` | `set_encoder` | `set_encoder` | definition | index, direction, keycode | `QK_BOOT` | read-back by raw code | per direction |
| Layout options | `reload_keymap` | `set_layout_options` | `set_layout_option` (merged per request) | `layouts.labels` | option index, choice range, unknown bits kept | no | read-back of the packed value | packed value |
| Tap dance | `tap_dance.reload_tap_dance` | `tap_dance_set` | `set_tap_dance` | dynamic entry count | index, keycodes, 16-bit term | `QK_BOOT` in any field | read-back per entry | per entry |
| Combos | `combo.reload_combo` | `combo_set` | `set_combo` | dynamic entry count | index, four inputs plus output, warning on gaps | `QK_BOOT` in any field (upstream: output only) | read-back per entry | per entry |
| Key overrides | `key_override.reload_key_override` | `key_override_set` | `set_key_override` | dynamic entry count | index, keycodes, masks, layer mask against layer count, option bits | `QK_BOOT` in any field (upstream: replacement only) | read-back per entry, raw option byte | per entry, raw option byte |
| Alternate repeat keys | `alt_repeat_key.reload_alt_repeat_key` | `alt_repeat_key_set` | `set_alt_repeat_key` | dynamic entry count | index, keycodes, mods, option bits | `QK_BOOT` in either keycode | read-back per entry, raw option byte | per entry, raw option byte |
| QMK settings | `keyboard_comm.reload_settings` | `qmk_settings_set` | `set_qmk_setting` | firmware qsid query plus `resources/qmk_settings.json` | known qsid only, width, integer range, boolean bits | no | status byte, then read-back | per qsid |
| Macros | `macro.reload_macros_late` | `macro.set_macro` | `set_macro` | macro count and buffer size | action tags, ASCII text, keycodes, protocol version, capacity, byte round trip, other macros untouched | always | read-back of the whole buffer | raw buffer, never the parsed form |
| Lighting | `keyboard_comm.reload_rgb`, `reload_persistent_rgb` | `set_qmk_rgblight_*`, `set_qmk_backlight_*`, `set_vialrgb_*`, `save_rgb` | `set_lighting` | definition `lighting` field | byte ranges, VialRGB effect list, maximum brightness | no | read-back of current values; saved values are not readable | current values, plus whether the write being reversed saved them |
| Security | `get_unlock_status`, `get_unlock_keys`, `unlock_start`, `unlock_poll`, `lock` | same | `unlock start/poll/status`, `lock` | always | proposal scope | not applicable | status read | not applicable |

## Upstream traps and how Vialtic handles them

- **Setters update memory before sending, and several send with no retries.** Vialtic never trusts upstream's in-memory state after a write: it reloads from the keyboard and compares. `GuardedTransport` raises every write packet to the same retry count, because all of them set absolute values.
- **The firmware silently replaces `QK_BOOT` with `KC_NO` while locked.** Read-back catches it, and any operation whose new value contains `QK_BOOT`, including an entry that only keeps an existing one, declares an unlock in its proposal.
- **`KeyOverrideOptions` and `AltRepeatKeyOptions` drop bits they do not name.** Vialtic reads the option byte again without that filter and writes it back untouched.
- **`macro_deserialize_v2` drops malformed bytes and `ActionText` turns bytes >= 0x80 into two UTF-8 bytes.** Backups and restores use the raw buffer. Editing one macro splices bytes into the raw buffer, so the other macros cannot change.
- **`restore_macros` truncates to the buffer size without telling anyone.** Vialtic refuses anything that does not fit. The only bytes it ever leaves out are terminators upstream's cleanup adds beyond the end of a buffer that has too few of them, which never existed on the keyboard.
- **`qmk_settings_set` returns a status byte that callers ignore.** Vialtic treats nonzero as a failed write.
- **`qmk_settings` reads drop values whose status is nonzero.** Those qsids are reported as unreadable, their coverage as partial, and writes to them are refused.
- **`set_layout_options` does nothing when the keyboard has no options.** Vialtic refuses earlier with a capability error.

## Upstream functions that are not configuration

| Upstream function | Classification | Vialtic behavior |
| --- | --- | --- |
| `Keyboard.reset` (packet 0x0B, jump to bootloader) | firmware flashing path, out of scope | Transport denies the packet. |
| `Keyboard.qmk_settings_reset` | destructive maintenance, not required | Transport denies the packet. |
| `CMD_VIA_SET_KEYBOARD_VALUE` other than `VIA_LAYOUT_OPTIONS` | not configuration | Transport denies the packet. |
| EEPROM reset and other VIA commands absent from the tables | destructive or unknown | Transport denies by default. |
| `Keyboard.matrix_poll` (`VIA_SWITCH_MATRIX_STATE`) | live keypress monitoring, out of scope | Transport denies the packet. |
| `Keyboard.get_uid` | identification | Read allowed; `reload_layout` already reads the same value. |
| `CMD_VIA_GET_KEYCODE` | read | Allowed; the buffer read is what is used. |
| `Keyboard.restore_layout` and `restore_*` | writes immediately, skips proposals, maps unreadable keycodes to `KC_NO`, drops extra entries | Never called. Files become typed operations. |
| `protocol/dummy_keyboard.py` | test double | Used only by upstream's tests. |
| Unlock dialog, editors, Qt widgets, matrix tester, firmware flasher | GUI | Not vendored. |
| Bootloader (`vibl`) devices | firmware flashing | Listed as ignored, never opened. |

## Knowable and unknowable

- **Saved lighting.** The firmware reports current lighting values only, and has one save command for all lighting. Vialtic cannot read the saved values, cannot tell whether current values are saved, and cannot confirm a save reached EEPROM. It reports a saved change as "values verified, save command acknowledged".
- **Firmware-limited lighting values.** A firmware may clamp values, for example to a brightness limit. Read-back reports that as a failed write and the prior values are restored.
- **QMK settings unknown to the pinned table.** Their width is unknown, so they can never be written. See "Unknown QMK settings" in STATUS.md for the decision.
- **Firewalled or ignored writes.** Most writes return no status. Read-back is the only evidence, so anything the firmware ignores surfaces as a verification difference, never as success.
