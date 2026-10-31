# Project status and handoff

Last updated: 2026-10-31.

## Actual state

M0, M1 and M2 gates are met. M3 and M4 code and their emulator and CI gates are met on branches `feat/m3-config-coverage` and `feat/m4-profiles`, not merged. Only keymap keys have ever been written on hardware: one approved change, read-back and undo on the owner's Corne. Every other family is emulator-tested only until the owner runs `docs/HARDWARE_TEST.md`. USB with a real keyboard on macOS is the pending item M0 allows and identifies. No release, tag, agent configuration or system setting was created or changed.

Layout added: `src/vialtic/` (CLI, service, proposals, storage, device, adapter, render, emulator), `vendor/vial/` (pinned Vial GUI subset), `tests/`, `packaging/`, `requirements.lock`, `.github/workflows/ci.yml`, `docs/CLI.md`. The Vial Web sources at the root are untouched.

Earlier documentation history: product interview complete, decisions in SPEC.md, inherited web CI removed at the owner's request, README origin note lowercase without quotation marks, mascot added, documentation maintenance rule added to AGENTS.md and SPEC.md. Local directory `~/Projects/vialtic`, personal remote `origin` `https://github.com/AugustoCarmona/vialtic.git`, original remote `upstream`. New-code license GPL-2.0-or-later.

## Milestones

### M0: reuse and packaging feasibility. Gate met; native USB on macOS pending.

- Done: pinned subset vendored with a hash manifest; five headless seams (about 160 lines) replace everything that imported Qt; upstream's `protocol/`, `keycodes/` and codecs run unmodified. No protocol rewrite and no offscreen Qt adapter. Measurements and decisions are in UPSTREAM.md.
- Done: `pyproject.toml`, hash-locked `requirements.lock`, PyInstaller directory build on Linux x86_64 that runs with an empty environment.
- Done: first CI run, on commit `e5012de` (run 41236435681). The Ubuntu 22.04 job passed every step on Python 3.12: tests, package build, package smoke test and artifact upload. The artifact it built also runs on the owner's Arch-based machine with an empty environment, on the emulator and in read-only discovery of the Corne. Its bundled binaries need glibc 2.35 or newer, which is the Linux baseline so far.
- Done: macOS arm64 in CI, on commit `17c9fc5` (run 41236867132, image `macos-15-arm64`, Python 3.12). All 228 tests passed, and the package built and passed its smoke test with no Python on PATH, including a composite-keycode plan on the emulator. `devices list` loaded the `hid` module from the `hidapi` wheel and enumerated without error, finding no keyboards. An earlier run on `e5012de` had its `macos-14` job cancelled by a GitHub brownout, which is why the workflow moved to `macos-15`.
- **Pending, as the gate allows:** no real keyboard has been used on macOS, so USB discovery, reads and writes there are unverified. CI covers macOS 15 only, so no lower minimum is claimed, and signing and notarization are undecided.

### M1: contracts, discovery, read-only views. Gate met.

Typed models and schema fixtures (`tests/schemas/`), JSON errors and exit codes (CLI.md), USB discovery, identity confidence, explicit selection, permission and busy diagnostics, full snapshot including lighting, text and PNG views from firmware geometry, stateful emulator with generic and split layouts. All read paths run behind a default-deny transport gate that refuses write packets.

### M2: proposal, approval, write, recovery. Gate met.

Immutable content-addressed proposals, baseline checks, per-device OS locks, durable pre-write backup, journaling, read-back, attestation scoped to proposal and device, guided physical unlock with relock, one automatic previous state, undo, bounded automatic restore, restart recovery, fault injection.

- Hardware: the gate's one approved Corne change, read-back and undo passed on 2026-10-31. See "Evidence". The other guarantees in this milestone are emulator-tested only.

### M3: complete configuration coverage. Emulator and CI gates met; hardware unverified.

Layout options, tap dance, combos, key overrides, alternate repeat keys, QMK settings, macros and lighting are written through the same proposal pipeline as keys and encoders, each with read, validate, propose, apply, verify, backup and restore tests in `tests/test_families.py` and an explicit capability error. `docs/CAPABILITIES.md` is the checked matrix, including upstream traps and non-configuration functions. The permanent `tests/test_fault_sweep.py` injects a crash, an interrupt and a full disk at every state-file operation and packet, for 12 operation types across change, undo, recovery and automatic restore.

**Hardware: nothing in this milestone has run on a real keyboard.** Firmware behavior beyond upstream's Python comes from reading vial-qmk source at branch head, not from a device: the unlock command filter, `QK_BOOT` firewall, backlight levels and RGB restrictions.

### M4: profiles and interchange. Emulator and CI gates met; hardware unverified.

`profile list/save/show/delete/plan`, `import` and `export` (Vialtic JSON and `.vil`). Applying always creates a normal proposal, with `proposal_type` `profile`. Upstream's `restore_layout` is never called: files become typed changes through the same planner, so keycodes pass `parse_keycode`. Files are hostile until validated (`src/vialtic/profiles.py`). `.vil` exports are checked against upstream's own `save_layout` output, and upstream's own `restore_layout` reads them. Backups from 0.1.0 stay readable, with a fixture generated by the 0.1.0 code; `binding_id` is unchanged.

M5 to M7 have not started.

## Version

Current version: 0.1.0, source only. The `v0.1.0` git tag goes on the commit that sets this version once CI passes on it. There is no GitHub Release, installer or published package. 1.0 is reserved for the SPEC section 14 completion gates.

## Evidence

Hardware: owner's Corne, `foostan Corne` 0x4653:0x0001, VIA protocol 9, Vial protocol 6, on Linux x86_64 with the existing udev rule. Read-only, from source and from the packaged binary: `doctor`, `devices list`, `device inspect`, `snapshot`, `view` (text and PNG, including rotated thumb keys), `plan` (validation against the real keymap, proposal stored in a scratch directory), `apply` without approval (refused). Three consecutive snapshots gave identical fingerprints, so stale checks should not misfire on it. Macro contents were never printed.

Hardware write, run by the owner on 2026-10-31 from source on the same Corne: `plan --set '1:6,2=KC_F13'`, `view --proposal`, `apply --user-approved` (outcome `verified`), `view` (F13 shown), `undo plan`, `apply --user-approved` (outcome `verified`), `view` (back to `XXX`). An extra `view --layer 0` between plan and apply did not invalidate the proposal. A read-only check afterward found the key back at `KC_NO`, the keyboard locked, `previous.json` holding `KC_F13` as the only one of 168 keymap entries differing from the keyboard (so the undo itself is revertible), and no `pending.json`, `recovery.json` or leftover proposals. `previous.json` and its directory were modes 600 and 700. The test checks firmware read-back; pressing the key on the host and persistence across a replug were not exercised. The raw terminal transcript is archived at `.easter_egg/first-run.txt`.

| Check | Command | Outcome |
| --- | --- | --- |
| Test suite, Python 3.14.8 | `.venv/bin/python -m pytest` | 228 passed |
| Test suite, Python 3.12.16, non-editable install | same | 228 passed |
| CI on Ubuntu 22.04, Python 3.12 | `.github/workflows/ci.yml`, run 41236435681 | tests, build, package smoke test and artifact pass; macOS job cancelled by GitHub, not run |
| CI on Ubuntu 22.04 and macOS 15 arm64, Python 3.12 | `.github/workflows/ci.yml`, run 41236867132 | both jobs pass: 228 tests, build, package smoke test and artifact, using the emulator |
| Upstream's own tests, unchanged, headless | part of the suite (`vendor/vial/upstream/test`) | pass; `test_gui.py` needs Qt and is not vendored |
| Vendored files match the manifest and the pinned clone | `tests/test_vendor.py` | pass |
| All 65,536 keycodes round-trip on protocols 5 and 6 | one-off script; the suite keeps a sampled version | 0 losses |
| Package with no Python and an empty environment | `env -i dist/vialtic/vialtic ...` | version, composite-keycode plan, apply, undo, PNG on the emulator, and read-only discovery on the Corne all work |
| Fault sweep at every state-file and packet site (crash, interrupt, disk full) over change, undo, QK_BOOT and recovery | one-off script from the independent review | no lost backups, no writes on restart, no false success. Remaining flags: process death while unlocked, which recovery relocks and reads warn about, and a checker invariant that does not apply to recovery |

An independent review of the write path, run against the emulator only, found 12 real bugs that the first test suite missed. All are fixed with regression tests at the end of `tests/test_write_core.py`. The most serious: automatic restore could overwrite an identical second keyboard and then delete the backup; a proposal could be written to an identical unit; a proposal file copied over another id could execute under that id's approval; a crash could lock a keyboard out of every further plan; an interrupt after verification could roll back a good write; and the packaged binary collected the old `src/simpleeval.py` from the web tree, which a guard in `packaging/vialtic.spec` now refuses.

Emulator-tested only, not verified on hardware: failure recovery and automatic restore, stale refusal, physical unlock and relock, encoders, layout options, and identical-unit handling. Hardware writes from the packaged binary have not been run either (it was only read-only on the Corne). The Corne reports no encoders, lighting, combos or QMK settings, so those need other hardware. Not verified anywhere: a real keyboard on macOS, and packaged builds on Linux systems other than Ubuntu 22.04 and the owner's machine.

## Decisions

- Emulator answers the wire protocol below upstream's `Keyboard` instead of extending `DummyKeyboard`, so codecs, the gate and transport failures are exercised. Rationale in UPSTREAM.md.
- `apply --user-approved` is the attestation flag. It cannot prove consent; documented in CLI.md.
- Stale checks and verification cover lighting only in proposals that change it, since lighting keys on the keyboard can change it at any moment. Undo and recovery restore only the lighting systems the reversed write changed, repeating its save choice.
- **Unknown QMK settings (resolves the SPEC 7 question):** a qsid missing from the pinned table has an unknown width, so it is never written, and a request for one is refused. Its raw answer is kept with the backup and undo reports if it moved. Other writes proceed, because nothing Vialtic sends touches these settings, so undo remains reliable for everything Vialtic changes, which is what the backup rule protects. Proposals warn about them. A known qsid the firmware refuses to report is flagged and refused, since it could not be backed up.
- Saving lighting is an explicit required choice. It is sent last, after verification, and is reported as acknowledged rather than verified, because firmware cannot report saved values.
- A write refuses to replace a `previous.json` it cannot read, so a future or damaged file is kept, and moving it aside by hand is the way forward.
- A macro write sends the whole buffer and zeroes the unused rest, so cleared text does not stay in keyboard memory. Reads and backups use the bytes up to the last terminator, which is all firmware can play.
- `snapshot` leaves out macro contents unless `--include-macros` is given. Backups keep them, owner-only.
- Identical units: writing after a path change needs an explicit `--device`, automatic restore stops unless the path matches and the values are explained, and backups are per identity. Corne firmware falls in this category.
- Dependencies: Python 3.12 or newer, `hidapi` 0.15 (ships `hidraw` for Linux, replacing upstream's fork), Pillow, pydantic, `simpleeval`. Exact versions and hashes are in `requirements.lock`, compiled with `uv pip compile --universal`.

## Next action

Run `docs/HARDWARE_TEST.md` on the Corne (owner). Merge decisions and pushes to `main` need the owner's request.

## Unresolved engineering work

- Verify USB with a real keyboard on macOS, and fix the minimum macOS version from evidence.
- CI warns that `actions/checkout@v4`, `setup-python@v5` and `upload-artifact@v4` target the deprecated Node.js 20. They still work, forced onto Node.js 24; update them when convenient.
- Implement M5 to M7, including `mcp serve` and `update`.
- Obtain macOS Apple Silicon hardware evidence when needed. Missing hardware is a verification limitation, not permission to claim passing results.
- Decide signing and notarization on evidence.
