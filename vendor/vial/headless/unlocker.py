# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless replacement for vial-gui's src/main/python/unlocker.py.

Upstream opens a modal Qt dialog from inside protocol setters. Vialtic runs the
guided physical unlock before it writes, so reaching this point while the
keyboard is locked is refused instead of prompting.
"""


class UnlockRequired(RuntimeError):
    pass


class Unlocker:

    @classmethod
    def unlock(cls, keyboard):
        if keyboard.get_unlock_status() == 1:
            return True
        raise UnlockRequired("the keyboard must be physically unlocked for this write")
