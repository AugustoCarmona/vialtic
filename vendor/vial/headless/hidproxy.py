# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless replacement for vial-gui's src/main/python/hidproxy.py.

Same platform selection as upstream, without the browser (emscripten) branch.
Both modules come from the PyPI hidapi distribution.
"""
import sys

if sys.platform.startswith("linux"):
    import hidraw as hid
else:
    import hid

__all__ = ["hid"]
