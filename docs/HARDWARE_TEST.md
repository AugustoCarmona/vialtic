# Hardware test plan

For the owner, on a real Corne, from source on Linux. It verifies milestones M3 and M4 on hardware. Everything marked implemented in `STATUS.md` has only run on the emulator until this is done. **Only you can run this plan:** it opens real USB and you are the one approving writes.

Run one section at a time, in order. Record results in the table at the end, then put them in `STATUS.md`.

## Before you start

- Close Vial, VIA and anything else that talks to the keyboard. Other software can interleave with Vialtic's reads.
- Keep a second keyboard or a laptop keyboard at hand, in case a change makes the Corne awkward to type on.
- Plug in only the Corne. With two keyboards connected, Vialtic asks you to pick one.

Set up, from the repository root:

```sh
git fetch origin && git switch feat/m4-profiles
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m pytest -q      # takes a few minutes; uses the emulator and needs no keyboard
export VT="$PWD"
v() { "$VT/.venv/bin/vialtic" "$@"; }
py() { "$VT/.venv/bin/python" "$@"; }
unset VIALTIC_EMULATOR
```

**Keep your own copy of the existing backup.** There is exactly one automatic previous state, so the first write in this plan replaces the `previous.json` that 0.1.0 wrote. Macro contents are inside, so keep it owner-only:

```sh
mkdir -m 700 ~/vialtic-hw-test
cp -rp ~/.local/state/vialtic/devices ~/vialtic-hw-test/devices-before
```

### If something goes wrong, at any step

| What you see | What it means | What to do |
| --- | --- | --- |
| `failed_restored` | The write failed, and the prior values were restored and verified. | Note the message and differences, then stop that section. |
| `partial` or `unknown` | The write failed and the restore did not finish. The saved state is kept. | Replug if needed. Run `v recover plan`, read it, then `v apply <id> --user-approved`. If it needs an unlock, hold the keys it lists. |
| `recovery_pending` | An earlier write is unresolved. New plans are refused until it is. | Same as above. |
| Recovery keeps failing | The result lists the file to move aside. | Run `v view` first to see the keyboard's real state. Vialtic never deletes that file. Stop and report it. |
| `unlock_in_progress` | An unlock was started and not finished. The firmware ignores other commands, and some keys may not work. | Replug the keyboard. Then retry. |
| `unlock_timeout` | You did not hold the keys in time. Nothing was written. | Replug, then apply again. It may need a new plan if the keyboard's state changed. |
| `device_ambiguous` | The USB path changed since the plan or backup. Vialtic cannot tell identical units apart. | If it is the same physical keyboard, run `v devices list` and pass `--device <id>`. |
| `proposal_stale` | The keyboard's state changed since planning. Nothing was written. | Plan again. |
| A key stops doing what it should | | Use the other keyboard: `v undo plan`, then apply it. |

**Any traceback, any `internal_error`, or any `verified` that looks wrong: stop, keep the terminal output, and report it.** Do not delete anything under `~/.local/state/vialtic`.

## Phase 1: read only

These send no writes. Save the output.

| Step | Command | Expected |
| --- | --- | --- |
| 1 | `v doctor` | HID ok, your Corne ready, VIA 9 and Vial 6. |
| 2 | `v devices list` | One keyboard, `ready`. |
| 3 | `v device inspect` | Identity note about identical units. `Writable on this keyboard:` lists what the rest of this plan can test. |
| 4 | `v snapshot` | Coverage line. Nothing errors. Macro contents left out. |
| 5 | `v view` | Your real keymap, with the right geometry. |
| 6 | `v view --unlock` | Keys marked `!` are the ones you hold to unlock. Note them. |
| 7 | `v unlock status` | `Keyboard is locked`, and the same keys. |

**Check that 0.1.0's backup still reads.** This plans only, and you should not apply it:

```sh
v undo plan
```

Expected: a proposal whose only change is the one you made last time, `KC_NO -> KC_F13`-style, or `no_changes` if the keyboard already matches, or `device_ambiguous` if the USB path changed, in which case repeat with `--device`. A refusal with `invalid_file` is a failure of this test: stop and report it. Then confirm the file was not touched:

```sh
diff -r ~/.local/state/vialtic/devices ~/vialtic-hw-test/devices-before && echo unchanged
```

Expected: `unchanged`. Proposal files live elsewhere.

**Record a fingerprint of your macros and the rest of the configuration.** It prints hashes only, never contents. You will repeat this after each test and compare:

```sh
fingerprint() {
  v snapshot --include-macros --json | py -c '
import json, sys, hashlib
r = json.load(sys.stdin)["result"]; c = r["configuration"]
for f in ("keymap","encoders","layout_options","tap_dance","combos","key_overrides","alt_repeat_keys","settings"):
    print(f"{f:16}", hashlib.sha256(json.dumps(c[f], sort_keys=True).encode()).hexdigest()[:16])
print("macro buffer    ", hashlib.sha256((c["macros"]["buffer_b64"] or "").encode()).hexdigest()[:16], c["macros"]["used_bytes"], "of", c["macros"]["memory_bytes"], "bytes")'
}
fingerprint | tee ~/vialtic-hw-test/fingerprint-before.txt
```

Run it twice. Expected: both runs are identical.

## Phase 2: choose harmless targets

Do not assume which slots are free: your keyboard already uses some. This lists them from what it reads, printing indexes only:

```sh
v snapshot --include-macros --json | py -c '
import json, sys, base64
c = json.load(sys.stdin)["result"]["configuration"]
keys = " ".join(e["keycode"] for e in c["keymap"])
free_td = [i for i, e in enumerate(c["tap_dance"]) if all(k == "KC_NO" for k in e[:4]) and f"TD({i})" not in keys]
free_ko = [i for i, e in enumerate(c["key_overrides"]) if e["trigger"] == "KC_NO" and e["replacement"] == "KC_NO" and not e["options"] & 0x80]
buffer = base64.b64decode(c["macros"]["buffer_b64"] or "")
parts = buffer.split(bytes(1))[:c["macros"]["count"]]
free_m = [i for i, p in enumerate(parts) if not p and f"M{i}" not in keys.split()]
print("free tap dance:", free_td); print("free key overrides:", free_ko); print("free macros:", free_m, "of", c["macros"]["count"])
last = max(k["layer"] for k in c["keymap"])
print("unused keys on the last layer:", ["%d:%d,%d" % (e["layer"], e["row"], e["col"]) for e in c["keymap"] if e["layer"] == last and e["keycode"] in ("KC_NO", "KC_TRNS")][:6])'
```

Write down one value of each, and use them below in place of `TD`, `KO`, `M` and `L:R,C`. Choose a key you do not rely on. If a list is empty, skip that test and record it as not tested. A free entry nothing references cannot change how the keyboard types, so these tests are safe while typing normally.

Create a working directory for change files:

```sh
cd ~/vialtic-hw-test
```

## Phase 3: one family at a time

Each test uses the same cycle, and **each one is undone before the next**, because only one previous state exists.

1. `v plan ...`. Expected: `Nothing has been written`, the before and after you asked for, and a proposal id.
2. `v view --layer <n> --proposal <id>`, or `v view --format png --proposal <id> --output plan.png`. Check that it shows what you expect.
3. `v apply <id> --user-approved`. By passing that flag you state that you approved this exact proposal. Expected: `Outcome: verified`.
4. Check the result as described in the test.
5. `v undo plan`. Expected: a proposal reversing exactly that change. Lighting differences, if any, are reported rather than restored.
6. `v apply <id> --user-approved`. Expected: `verified`.
7. `fingerprint | diff - fingerprint-before.txt`. **Expected: no difference.** That proves your macros and everything else are back. If you see a difference, stop.

### 3.1 Key, composite keycode

Target: an unused key. Cycle with:

```sh
v plan --set 'L:R,C=LGUI(KC_F13)'
```

Check in step 4: `v view --layer L` shows it, and pressing it on the host sends Super+F13. Check with `wev` or similar. Replug the keyboard, and `v view --layer L` still shows it, which verifies persistence. If the path changed, step 5 may need `--device`.

### 3.2 Tap dance

Skip if `set_tap_dance` is not in `device inspect`. Target: a free entry.

```sh
cat > td.json <<'J'
{"schema_version": 1, "changes": [{"type": "set_tap_dance", "index": TD, "on_tap": "KC_F13", "on_hold": "KC_F14", "tapping_term": 180}]}
J
v plan --changes td.json
```

Check in step 4: `v snapshot --json | py -c 'import json,sys;print(json.load(sys.stdin)["result"]["configuration"]["tap_dance"][TD])'`. Optional, to feel it: in a separate cycle, also assign `TD(TD)` to your unused key and test it.

### 3.3 Key override

Skip if `set_key_override` is not listed. Target: a free entry. `options` 0 leaves it disabled, so it cannot affect typing:

```sh
cat > ko.json <<'J'
{"schema_version": 1, "changes": [{"type": "set_key_override", "index": KO, "trigger": "KC_F13", "replacement": "KC_F14", "layers": 65535, "options": 0}]}
J
v plan --changes ko.json
```

Check in step 4 the same way, with `key_overrides` and `KO`.

### 3.4 Macro, and your other macros staying intact

Skip if `set_macro` is not listed. Target: a free macro. This needs the physical unlock. **Hold the keys listed when `apply` asks, for up to 60 seconds, for both the apply and the undo.** Expected: progress on stderr, `Keyboard locked again` at the end.

```sh
cat > macro.json <<'J'
{"schema_version": 1, "changes": [{"type": "set_macro", "index": M, "actions": [["text", "vialtic test"]]}]}
J
v plan --changes macro.json
```

Expected in the plan: `Physical unlock required`, only macro `M` listed, and no contents shown. Then:

```sh
v proposal show <id> --include-macros
```

Expected: only macro `M`, before and after. This is what an agent has to show you before asking for approval.

Check in step 4: `fingerprint` shows a different macro buffer hash, and `v snapshot --include-macros --json` shows the text in macro `M` and your other macros unchanged. Step 7 is the proof that your real macros are intact: the buffer hash must match the one from Phase 1, byte for byte. Optional: assign `M<M>` to your unused key and test it with a text editor focused.

### 3.5 Physical unlock in an entry

Optional. Skip if no free tap dance. Writes `QK_BOOT` into an entry no key references, so nothing can trigger it. **Do not assign `TD(TD)` to a key during this test:** that key would put the keyboard into bootloader mode.

```sh
cat > boot.json <<'J'
{"schema_version": 1, "changes": [{"type": "set_tap_dance", "index": TD, "on_tap": "QK_BOOT"}]}
J
v plan --changes boot.json
```

Expected: `Physical unlock required`, and a warning about bootloader mode. Hold the keys. Check in step 4: the entry holds `QK_BOOT`, which proves the firmware accepted it only because the board was unlocked. Undo also asks for the unlock.

### 3.6 Refusals

These write nothing. Expected: each answers with the code shown, and `fingerprint` is unchanged afterward.

| Command | Expected |
| --- | --- |
| `v apply <id>` on a fresh plan, without the flag | `approval_required` |
| Plan change A on one unused key, plan change B on another, apply B, then apply A | A answers `proposal_stale`, because B changed the keymap. Undo B afterward. |
| A change file for a family absent on this keyboard, for example `{"schema_version": 1, "changes": [{"type": "set_combo", "index": 0, "output": "KC_A"}]}` | `capability_unavailable` |
| `v plan --set '99:0,0=KC_A'` | `invalid_coordinate` |
| `v plan --set '0:0,0=LT(1, LCTL(KC_A))'` | `invalid_keycode` |

### 3.7 Interrupted unlock

Plan the macro test again. Run `v apply <id> --user-approved --unlock-timeout 5` without holding the keys. Expected: `unlock_timeout`, nothing written, and a next step saying to replug. Then run `v snapshot`. Expected: `unlock_in_progress`, explaining the state. Replug. `v snapshot` works and the fingerprint is unchanged. Discard that proposal; it can be planned again if needed.

## Phase 4: profiles and files

No step here writes until you apply.

```sh
v profile save hw-test
v profile list
v profile show hw-test
v profile plan hw-test
```

Expected: stored at `~/.local/share/vialtic/profiles/hw-test.json`, mode 600. `show` leaves macro contents out. `plan` answers `no_changes`.

**Apply a profile.** Make a harmless change with `v plan --set 'L:R,C=KC_F13'` and apply it. Then:

```sh
v profile plan hw-test
```

Expected: a proposal of type `profile` changing only that key back, and warnings about what it leaves out, if any. Apply it. `fingerprint | diff - fingerprint-before.txt` shows no difference. Then `v undo plan` and apply puts `KC_F13` back, and `v undo plan` with apply once more removes it. Check the fingerprint again.

**Files.**

```sh
v export --output full.json
v export --output full.vil --format vil
v export --output with-macros.vil --format vil --include-macros
ls -l full.json full.vil with-macros.vil
```

Expected: all mode 600. `full.vil` reports that lighting is left out and that macros are left out unless asked. `grep -c macro full.vil` prints 0. Keep `with-macros.vil` private, and delete it when finished.

**A `.vil` from real Vial.** Save your layout in the Vial application to `vial.vil`, then close Vial:

```sh
v import vial.vil --name from-vial
v profile plan from-vial
```

Expected: stored as `compatible`, and `no_changes` if nothing moved. A refusal naming a macro means a macro holds bytes the strict encoder refuses, which is expected for non-ASCII text. Note it. Loading `with-macros.vil` back into Vial should show your configuration, which verifies compatibility in the other direction. Close Vial again afterward, and check the fingerprint.

**A refusal.** Edit a copy of `full.vil` and change `uid` to `1`:

```sh
v import edited.vil --name wrong
```

Expected: `profile_incompatible`, with no way to force it, and no `wrong` in `v profile list`.

**Clean up.**

```sh
v profile delete hw-test
v profile delete from-vial
shred -u with-macros.vil
```

## Phase 5: final check

```sh
fingerprint | diff - fingerprint-before.txt && echo same
v device inspect
```

Expected: `same`, locked, no pending recovery, no warnings. `~/.local/state/vialtic/devices/<binding>/previous.json` now holds the state from before your last undo, which is normal. Your 0.1.0 copy stays in `~/vialtic-hw-test/devices-before`; it holds your macros, so delete it when you are satisfied. Replug and run `v view` and the fingerprint once more, which verifies everything persisted.

## What your Corne cannot verify

It reports none of these, so they stay emulator-tested only, whatever this plan shows. Confirm with Phase 1, and if `device inspect` lists any, tell me: they could then be tested too.

| Feature | Stays unverified because |
| --- | --- |
| Encoders | Corne reports none. |
| Combos | Corne reports none. |
| Alternate repeat keys | Corne reports none. |
| QMK settings, including the status byte, widths and flag bits | Corne reports none. |
| Lighting: RGB light, backlight, VialRGB, saves, transient values | Corne reports none. Firmware behavior here comes from reading vial-qmk source, not from a device. |
| Layout options | Corne's definition has no options. |
| Identical second unit | Needs another Corne. |
| A real failure during a write: disconnect, restore, recovery | Not exercised here. Pulling the cable during a write risks nothing that recovery does not cover, but it is not part of this plan. Do it only if you want that evidence, on a plain key change. |
| macOS | Needs Apple Silicon hardware. |
| Packaged binary writes | This plan runs from source. Repeating 3.1 with `dist/vialtic/vialtic` would cover it, once you build it with `.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build/vialtic packaging/vialtic.spec`. |

## Results

| Section | Pass, fail or not tested | Notes |
| --- | --- | --- |
| Phase 1, reads | | |
| Phase 1, 0.1.0 backup still valid | | |
| 3.1 Key, read-back, host key press, replug, undo | | |
| 3.2 Tap dance | | |
| 3.3 Key override | | |
| 3.4 Macro, other macros intact | | |
| 3.5 `QK_BOOT` in an entry, unlock | | |
| 3.6 Refusals | | |
| 3.7 Interrupted unlock | | |
| Phase 4, profile apply, undo | | |
| Phase 4, export and import | | |
| Phase 4, `.vil` from real Vial, both directions | | |
| Phase 5, final state after replug | | |
