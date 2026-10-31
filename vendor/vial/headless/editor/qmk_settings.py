# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless replacement for vial-gui's editor/qmk_settings.py.

Keeps the qsid table and codec classmethods verbatim, without the Qt editor.
The table loads from the vendored resource instead of the fbs application
context.
"""
import json
import pathlib
from collections import defaultdict

RESOURCE = pathlib.Path(__file__).resolve().parents[2] / "upstream" / "resources" / "qmk_settings.json"


class QmkSettings:

    qsid_fields = None
    settings_defs = None

    @classmethod
    def initialize(cls, path=RESOURCE):
        cls.qsid_fields = defaultdict(list)
        with open(path, "r") as inf:
            cls.settings_defs = json.load(inf)
        for tab in cls.settings_defs["tabs"]:
            for field in tab["fields"]:
                cls.qsid_fields[field["qsid"]].append(field)

    @classmethod
    def is_qsid_supported(cls, qsid):
        """ Return whether this qsid is supported by the settings editor """
        return qsid in cls.qsid_fields

    @classmethod
    def qsid_serialize(cls, qsid, data):
        """ Serialize from internal representation into binary that can be sent to the firmware """
        fields = cls.qsid_fields[qsid]
        if fields[0]["type"] == "boolean":
            assert isinstance(data, int)
            return data.to_bytes(fields[0].get("width", 1), byteorder="little")
        elif fields[0]["type"] == "integer":
            assert isinstance(data, int)
            assert len(fields) == 1
            return data.to_bytes(fields[0]["width"], byteorder="little")

    @classmethod
    def qsid_deserialize(cls, qsid, data):
        """ Deserialize from binary received from firmware into internal representation """
        fields = cls.qsid_fields[qsid]
        if fields[0]["type"] == "boolean":
            return int.from_bytes(data[0:fields[0].get("width", 1)], byteorder="little")
        elif fields[0]["type"] == "integer":
            assert len(fields) == 1
            return int.from_bytes(data[0:fields[0]["width"]], byteorder="little")
        else:
            raise RuntimeError("unsupported field")


QmkSettings.initialize()
