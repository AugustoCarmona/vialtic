# SPDX-License-Identifier: GPL-2.0-or-later
# PyInstaller entry point. It lives outside src/ on purpose: PyInstaller puts the root of the
# entry script's package on its search path, and src/ also holds the Vial Web tree.
import sys

from vialtic.cli import main

sys.exit(main())
