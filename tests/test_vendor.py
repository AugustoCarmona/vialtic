# SPDX-License-Identifier: GPL-2.0-or-later
"""M0 gate: vendored source is unmodified and the read-only core needs no GUI."""
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "vial"


def manifest_entries():
    for line in (VENDOR / "MANIFEST.sha256").read_text().splitlines():
        if line and not line.startswith("#"):
            digest, local, upstream = line.split()
            yield digest, local, upstream


def test_vendored_files_match_manifest():
    entries = list(manifest_entries())
    assert entries
    for digest, local, _ in entries:
        assert hashlib.sha256((VENDOR / "upstream" / local).read_bytes()).hexdigest() == digest, local


def test_every_upstream_file_is_listed():
    listed = {local for _, local, _ in manifest_entries()}
    present = {str(p.relative_to(VENDOR / "upstream")) for p in (VENDOR / "upstream").rglob("*")
               if p.is_file() and "__pycache__" not in p.parts}
    assert present == listed


def test_vendored_copying_matches_repository():
    assert (VENDOR / "upstream" / "COPYING").read_bytes() == (ROOT / "COPYING").read_bytes()


def test_core_imports_without_qt_or_a_display():
    code = ("import sys, json, vialtic.cli, vialtic.service, vialtic.render, vialtic.emulator;"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('PyQt5','PyQt6','PySide2','PySide6','fbs_runtime','vialglue'))))")
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={"PATH": ""})
    assert json.loads(output.stdout) == []


def test_simpleeval_comes_from_the_locked_dependency_not_the_web_tree():
    code = "import vialtic.vial_adapter, simpleeval; print(simpleeval.__file__)"
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert str(ROOT / "src" / "simpleeval.py") != output.stdout.strip()


def test_upstream_keyboard_class_is_the_protocol_implementation():
    from vialtic import vial_adapter as va

    assert issubclass(va.HeadlessKeyboard, sys.modules["protocol.keyboard_comm"].Keyboard)
    # Only definition limits are overridden; reads and writes stay upstream's.
    overridden = {k for k in vars(va.HeadlessKeyboard) if not k.startswith("__")}
    assert overridden == {"reload_layout", "_fetch_definition", "reload_layers"}


def test_package_build_cannot_collect_the_web_tree():
    spec = (ROOT / "packaging" / "vialtic.spec").read_text()
    assert "pathex=" not in spec, "src/ on the search path shadows locked dependencies with the Vial Web copies"
    assert "collected from the Vial Web tree" in spec
