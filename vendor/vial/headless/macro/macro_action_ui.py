# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless replacement for vial-gui's macro/macro_action_ui.py.

protocol/macro.py only needs the tag table; the widget classes are dropped.
"""
from macro.macro_action import ActionText, ActionDown, ActionUp, ActionTap, ActionDelay

tag_to_action = {
    "down": ActionDown,
    "up": ActionUp,
    "tap": ActionTap,
    "text": ActionText,
    "delay": ActionDelay,
}
