# Vendored Vial GUI subset

Source: https://github.com/vial-kb/vial-gui at revision `aef8222a2d0429a183b2ed692d5f9efcfd383f08`, GPL-2.0-or-later per each file's header and `upstream/COPYING`.

## Layout

- `upstream/`: **unmodified** copies. `MANIFEST.sha256` lists `sha256  local path  upstream path` for each file; `tests/test_vendor.py` verifies the hashes, so a local edit fails the test suite.
- `headless/`: narrow replacements for the five modules that import PyQt5 or the browser glue. Everything else in `upstream/` runs unchanged against them.

`headless/` precedes `upstream/` on `sys.path` (see `src/vialtic/vial_adapter.py`). `macro` is a namespace package spanning both directories.

| Headless module | Replaces | Kept | Dropped |
| --- | --- | --- | --- |
| `util.py` | `src/main/python/util.py` | `hid_send`, `chunks`, `MSG_LEN`, serial magics | Qt translation, widgets, `KeycodeDisplay`, log setup, device search (now in `src/vialtic/device.py`) |
| `unlocker.py` | `src/main/python/unlocker.py` | `Unlocker.unlock` entry point and status check | Modal dialog; raises `UnlockRequired` instead of prompting |
| `hidproxy.py` | `src/main/python/hidproxy.py` | Linux `hidraw` / other `hid` selection | Emscripten branch |
| `macro/macro_action_ui.py` | `src/main/python/macro/macro_action_ui.py` | `tag_to_action` table | Widget classes |
| `editor/qmk_settings.py` | `src/main/python/editor/qmk_settings.py` | qsid table and codec classmethods | Qt editor; loads the vendored JSON directly |

## Runtime dependency changes from upstream

Upstream's `requirements.txt` is not used. `simpleeval` comes from PyPI rather than upstream's pinned archive, and `hidraw`/`hid` come from the PyPI `hidapi` distribution rather than upstream's fork. Exact versions are in `requirements.lock`. Upstream's own unit tests in `upstream/test/` run unchanged against these versions as part of `pytest`, except `test_gui.py`, which needs Qt and is not vendored.

## Updating

Replace files in `upstream/` from a new pinned revision, regenerate the manifest, re-run the tests, and record the revision here and in `docs/UPSTREAM.md`.
