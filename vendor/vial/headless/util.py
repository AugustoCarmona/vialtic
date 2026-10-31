# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless replacement for vial-gui's src/main/python/util.py.

Keeps the transport and helper functions used by the protocol modules verbatim
and drops everything that needs PyQt5 (translation, logging paths, widgets,
keycode display). See vendor/vial/README.md.
"""
import time

# For Vial keyboard
VIAL_SERIAL_NUMBER_MAGIC = "vial:f64c2b3c"

# For bootloader
VIBL_SERIAL_NUMBER_MAGIC = "vibl:d4f8159c"

MSG_LEN = 32


def hid_send(dev, msg, retries=1):
    if len(msg) > MSG_LEN:
        raise RuntimeError("message must be less than 32 bytes")
    msg += b"\x00" * (MSG_LEN - len(msg))

    data = b""
    first = True

    while retries > 0:
        retries -= 1
        if not first:
            time.sleep(0.5)
        first = False
        try:
            # add 00 at start for hidapi report id
            if dev.write(b"\x00" + msg) != MSG_LEN + 1:
                continue

            data = bytes(dev.read(MSG_LEN, timeout_ms=500))
            if not data:
                continue
        except OSError:
            continue
        break

    if not data:
        raise RuntimeError("failed to communicate with the device")
    return data


def chunks(data, sz):
    for i in range(0, len(data), sz):
        yield data[i:i+sz]
