# SPDX-License-Identifier: GPL-2.0-or-later
# PyInstaller build: pyinstaller --noconfirm --clean packaging/vialtic.spec
# Vendored Vial sources ship as files because they load through sys.path at runtime;
# their dependencies below are therefore declared explicitly.
import pathlib

from PyInstaller.utils.hooks import collect_submodules

root = pathlib.Path(SPECPATH).parent
vendor = root / "vendor" / "vial"


def tree(source, target):
    return [(str(p), str(pathlib.PurePosixPath(target) / p.parent.relative_to(source)))
            for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts and "test" not in p.relative_to(source).parts]


datas = tree(vendor / "upstream", "vialtic/_vendor/vial/upstream") + tree(vendor / "headless", "vialtic/_vendor/vial/headless")
datas += [(str(vendor / "README.md"), "vialtic/_vendor/vial"), (str(vendor / "MANIFEST.sha256"), "vialtic/_vendor/vial"),
          (str(root / "COPYING"), ".")]

analysis = Analysis(
    # Never src/ on the search path: it also holds the Vial Web tree, whose old simpleeval.py
    # would shadow the locked dependency. vialtic comes from the installed package.
    [str(root / "packaging" / "entry.py")],
    datas=datas,
    # hid and hidraw are both listed; hidproxy picks one per platform at runtime.
    hiddenimports=collect_submodules("vialtic") + ["simpleeval", "hid", "hidraw", "lzma", "ast", "operator", "PIL._imagingft"],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest"],
)
for name, path, _ in analysis.pure:
    if pathlib.Path(path or "").is_relative_to(root / "src") and not name.startswith("vialtic"):
        raise SystemExit(f"{name} was collected from the Vial Web tree: {path}")
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="vialtic", console=True)
coll = COLLECT(exe, analysis.binaries, analysis.datas, name="vialtic")
