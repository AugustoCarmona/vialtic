# Project status and handoff

Last updated: 2026-10-31.

## Actual state

M0, M1 and M2 gates are met. USB with a real keyboard on macOS is the pending item M0 allows and identifies. One approved keymap change, read-back and undo is hardware-verified on the owner's Corne. This work was pushed to `main` at the owner's request; no release, tag, agent configuration or system setting was created or changed.

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

- Writable operations are `set_key` and `set_encoder` only. Other families are read and backed up; M3 adds writing them. Undo refuses (`restore_unsupported`) if one of them differs. Recovery leaves them as they are and says so, because refusing would lock the keyboard out of all further plans.
- Hardware: the gate's one approved Corne change, read-back and undo passed on 2026-10-31. See "Evidence". The other guarantees in this milestone are emulator-tested only.

M3 to M7 have not started.

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
- Stale checks cover every family except lighting, which lighting keys on the keyboard can change at any moment. Lighting differences during undo are warnings.
- `snapshot` leaves out macro contents unless `--include-macros` is given. Backups keep them, owner-only.
- Unknown QMK settings produce a warning and do not block keymap writes, since this build cannot write settings. Revisit when M3 makes them writable (SPEC 7).
- Identical units: writing after a path change needs an explicit `--device`, automatic restore stops unless the path matches and the values are explained, and backups are per identity. Corne firmware falls in this category.
- Dependencies: Python 3.12 or newer, `hidapi` 0.15 (ships `hidraw` for Linux, replacing upstream's fork), Pillow, pydantic, `simpleeval`. Exact versions and hashes are in `requirements.lock`, compiled with `uv pip compile --universal`.

## Next action

Implement M3 from PLAN.md: the capability matrix and writing for the remaining families through the same pipeline, starting by extending `RESTORABLE_FAMILIES` and `WRITABLE_OPERATIONS` in `src/vialtic/vial_adapter.py`. Pushes, tags and releases need the owner's request.

## Unresolved engineering work

- Verify USB with a real keyboard on macOS, and fix the minimum macOS version from evidence.
- CI warns that `actions/checkout@v4`, `setup-python@v5` and `upload-artifact@v4` target the deprecated Node.js 20. They still work, forced onto Node.js 24; update them when convenient.
- Implement M3 to M7, including `profile`, `import`, `export`, `mcp serve` and `update`.
- Obtain macOS Apple Silicon hardware evidence when needed. Missing hardware is a verification limitation, not permission to claim passing results.
- Decide signing and notarization on evidence.
