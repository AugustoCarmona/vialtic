# Upstream reuse and provenance

## Reviewed sources

| Project | Revision reviewed on 2026-10-30 | Role |
| --- | --- | --- |
| https://github.com/vial-kb/vial-web | `3749f8dabc6d186f9b17cb75eed64e6bf0e23e16` | Original checkout/history; browser packaging infrastructure. |
| https://github.com/vial-kb/vial-gui | `aef8222a2d0429a183b2ed692d5f9efcfd383f08` | Python application, primary source for reuse. |

The root web build preloads Vial GUI Python files into an Emscripten build with Qt/CPython. It is not the simplest runtime for a local CLI or stdio MCP server. No upstream build has been executed as part of specification work.

## Concrete reuse map

Paths below are relative to `vial-gui/src/main/python/`:

- `protocol/keyboard_comm.py`: discovery-adjacent state loading, keyboard definition/KLE integration, keymap/encoders, lighting, settings, serialization, physical unlock and locking.
- `protocol/{base_protocol,dynamic,combo,tap_dance,key_override,alt_repeat_key,macro,constants}.py`: existing protocol/feature implementation.
- `vial_device.py`, `hidproxy.py`: device and HID boundary; evaluate native paths without browser compatibility code.
- `keycodes/`, `keymap/`, `keymaps.py`, `kle_serial.py`: keycode versions/labels and generic physical layout geometry.
- `protocol/dummy_keyboard.py`: existing dummy backend, useful as emulator starting point but not sufficient for failure recovery testing without extension.
- `protocol/keyboard_comm.py:save_layout/restore_layout`: `.vil` codec. Returns UTF-8 JSON bytes, not a proprietary binary format. Lighting is absent from its saved fields.
- `src/main/resources/base/qmk_settings.json` and related code: settings metadata; retain resources in packaged builds as needed.

## Known headless seams

`keyboard_comm.py` imports `Unlocker` and `util`; `util.py` imports PyQt5 widgets/core/gui. `protocol/macro.py` imports unlock UI and `macro_action_ui`. QMK settings serialization is referenced through `editor/qmk_settings.py`. Separate pure data/codec/transport functions from widgets rather than rewriting their logic or shipping dialogs in a CLI. Validate actual transitive dependencies during M0.

Upstream `requirements.txt` contains old fbs/PyInstaller/PyQt pins. Do not copy it as Vialtic's supported runtime environment. Resolve tested maintained equivalents and eliminate GUI-only requirements where possible.

## Licensing and history

The repository retains the Vial Web history and its existing source. Its original remote is named `upstream`. At the owner’s subsequent request, the personal `origin` is configured as `https://github.com/AugustoCarmona/vialtic.git` for the initial documentation push. Future pushes require user authorization.

Root COPYING is the unmodified GPL v2 text copied from the reviewed Vial GUI revision. New Vialtic code uses `SPDX-License-Identifier: GPL-2.0-or-later`. Preserve existing per-file notices and dependency-specific licenses; do not relabel third-party files wholesale. Record vendored source revisions, local modifications and upstream paths; ship required notices/source with releases. No claim is made that every file in a public repository has one universal license.

The reusable Python subset is vendored in `vendor/vial/` from the Vial GUI revision above. `vendor/vial/upstream/` holds unmodified copies verified against `vendor/vial/MANIFEST.sha256` by the test suite, and `vendor/vial/headless/` holds narrow Qt-free replacements; `vendor/vial/README.md` lists every seam and what it keeps. Normal builds use neither nested clones nor network fetches. Web references stay in place until a documented reorganization; moving them is optional.

Note that the root `src/simpleeval.py` belongs to the Vial Web build. Vialtic uses the locked PyPI `simpleeval`, and a test checks that the old copy is not the one loaded.

## Measured reuse decisions (M0)

- **Headless seam:** five replacement modules totaling about 160 lines are enough to run upstream's `protocol/`, `keycodes/`, `macro_action`, `kle_serial` and `any_keycode` unmodified, with no PyQt5 loaded. No offscreen Qt adapter was needed, so the agreed architecture stands. Upstream's own `test_keyboard`, `test_keycode` and `test_macro` (19 tests) pass unchanged against it.
- **Wire protocol:** not rewritten. `HeadlessKeyboard` subclasses upstream's `Keyboard` and overrides only definition loading, adding size limits and refusing unknown protocol versions before parsing, and the layer count check. A test pins that list of overrides.
- **Emulator:** implemented below `Keyboard`, as firmware answering the wire protocol, rather than by extending `DummyKeyboard`. `DummyKeyboard` overrides every send path, so it would not exercise packet codecs, the write gate, or transport failures, which are what recovery testing needs. Default capacities follow `DummyKeyboard`, and it remains vendored because upstream's macro tests use it.
- **Keycode fidelity:** upstream's decoder maps unparseable input to `KC_NO`, so requests go through a stricter grammar first. Reads rely on `Keycode.serialize`/`deserialize`, and all 65,536 codes round-trip without loss on Vial protocols 5 and 6 (one-off exhaustive check; the suite keeps a sampled version).
- **Not yet vendored:** `keymap/` and `keymaps.py` (host layout overrides for labels), since Vialtic does not assume the host's locale.

## Primary references

- https://github.com/vial-kb/vial-gui
- https://github.com/vial-kb/vial-web
- https://get.vial.today/docs/porting-to-via.html
- https://get.vial.today/manual/linux-udev.html
- https://get.vial.today/docs/security.html

These sources inform implementation; the accepted product behavior is in SPEC.md.
