# SPDX-License-Identifier: GPL-2.0-or-later
"""USB discovery, identity, per-device sessions and the transport write gate."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import struct
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Protocol

from . import vial_adapter as va
from .errors import VialticError
from .models import DeviceEntry, DeviceIdentity, ErrorInfo, IdentityConfidence, IgnoredInterface, Snapshot
from .storage import Store

RAW_HID_USAGE_PAGE = 0xFF60
RAW_HID_USAGE = 0x61
BUS_USB = 1

proto = va.proto


# Packet classification. Default deny: a command missing from these tables cannot
# be sent at all, so a read path cannot write even if upstream code tries.

READ_COMMANDS = {
    proto.CMD_VIA_GET_PROTOCOL_VERSION, proto.CMD_VIA_GET_KEYCODE, proto.CMD_VIA_LIGHTING_GET_VALUE,
    proto.CMD_VIA_MACRO_GET_COUNT, proto.CMD_VIA_MACRO_GET_BUFFER_SIZE, proto.CMD_VIA_MACRO_GET_BUFFER,
    proto.CMD_VIA_GET_LAYER_COUNT, proto.CMD_VIA_KEYMAP_GET_BUFFER,
}
WRITE_COMMANDS = {proto.CMD_VIA_SET_KEYCODE, proto.CMD_VIA_LIGHTING_SAVE, proto.CMD_VIA_MACRO_SET_BUFFER}
LIGHTING_WRITES = {
    proto.QMK_BACKLIGHT_BRIGHTNESS, proto.QMK_BACKLIGHT_EFFECT, proto.QMK_RGBLIGHT_BRIGHTNESS, proto.QMK_RGBLIGHT_EFFECT,
    proto.QMK_RGBLIGHT_EFFECT_SPEED, proto.QMK_RGBLIGHT_COLOR, proto.VIALRGB_SET_MODE,
}
VIAL_READ = {
    proto.CMD_VIAL_GET_KEYBOARD_ID, proto.CMD_VIAL_GET_SIZE, proto.CMD_VIAL_GET_DEFINITION, proto.CMD_VIAL_GET_ENCODER,
    proto.CMD_VIAL_GET_UNLOCK_STATUS, proto.CMD_VIAL_QMK_SETTINGS_QUERY, proto.CMD_VIAL_QMK_SETTINGS_GET,
}
VIAL_WRITE = {proto.CMD_VIAL_SET_ENCODER, proto.CMD_VIAL_QMK_SETTINGS_SET}
VIAL_SECURITY = {proto.CMD_VIAL_UNLOCK_START, proto.CMD_VIAL_UNLOCK_POLL, proto.CMD_VIAL_LOCK}
DYNAMIC_READ = {
    proto.DYNAMIC_VIAL_GET_NUMBER_OF_ENTRIES, proto.DYNAMIC_VIAL_TAP_DANCE_GET, proto.DYNAMIC_VIAL_COMBO_GET,
    proto.DYNAMIC_VIAL_KEY_OVERRIDE_GET, proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_GET,
}
DYNAMIC_WRITE = {
    proto.DYNAMIC_VIAL_TAP_DANCE_SET, proto.DYNAMIC_VIAL_COMBO_SET, proto.DYNAMIC_VIAL_KEY_OVERRIDE_SET,
    proto.DYNAMIC_VIAL_ALT_REPEAT_KEY_SET,
}


def classify_packet(msg: bytes) -> str:
    """Return read, write, security or deny for an outgoing packet."""
    cmd = msg[0] if msg else None
    sub = msg[1] if len(msg) > 1 else None
    if cmd == proto.CMD_VIA_GET_KEYBOARD_VALUE:
        # Live switch state is out of scope (no keypress monitoring).
        return "read" if sub == proto.VIA_LAYOUT_OPTIONS else "deny"
    if cmd == proto.CMD_VIA_SET_KEYBOARD_VALUE:
        # The same command also carries firmware-defined values; only layout options are configuration.
        return "write" if sub == proto.VIA_LAYOUT_OPTIONS else "deny"
    if cmd == proto.CMD_VIA_LIGHTING_SET_VALUE:
        return "write" if sub in LIGHTING_WRITES else "deny"
    if cmd in READ_COMMANDS:
        return "read"
    if cmd in WRITE_COMMANDS:
        return "write"
    if cmd != proto.CMD_VIA_VIAL_PREFIX:
        return "deny"
    if sub in VIAL_READ:
        return "read"
    if sub in VIAL_WRITE:
        return "write"
    if sub in VIAL_SECURITY:
        return "security"
    if sub == proto.CMD_VIAL_DYNAMIC_ENTRY_OP and len(msg) > 2:
        if msg[2] in DYNAMIC_READ:
            return "read"
        if msg[2] in DYNAMIC_WRITE:
            return "write"
    return "deny"


# Every write sets an absolute value, so repeating one is harmless; upstream sends some with no retries.
WRITE_RETRIES = 20
# Far more than the largest legitimate reads, so firmware that answers nonsense cannot loop forever.
MAX_READS_PER_SESSION = 100_000


class GuardedTransport:
    """usb_send replacement for upstream Keyboard. Writes need an explicitly opened gate."""

    def __init__(self, send: Callable[..., bytes] = va.hid_send, max_retries: int | None = None):
        self._send = send
        self._max_retries = max_retries
        self._writes_open = False
        self._security_open = False
        self.packets: Counter[str] = Counter()

    def __call__(self, dev: Any, msg: bytes, retries: int = 1) -> bytes:
        kind = classify_packet(msg)
        if kind == "deny" or (kind == "write" and not self._writes_open) or \
                (kind == "security" and not self._security_open):
            raise VialticError("write_blocked", f"Refused to send {kind} packet 0x{msg[:3].hex()}: gate closed.",
                               details={"packet_prefix": msg[:3].hex(), "class": kind})
        if kind == "write":
            retries = max(retries, WRITE_RETRIES)
        if self._max_retries is not None:
            retries = min(retries, self._max_retries)
        self.packets[kind] += 1
        if kind == "read" and self.packets[kind] > MAX_READS_PER_SESSION:
            raise VialticError("malformed_device_data", "The keyboard keeps answering without finishing a read, so Vialtic stopped.",
                               next_steps=["Replug the keyboard and retry; report this firmware if it persists."])
        return self._send(dev, msg, retries=retries)

    @contextlib.contextmanager
    def writes(self) -> Iterator[None]:
        self._writes_open = True
        try:
            yield
        finally:
            self._writes_open = False

    @contextlib.contextmanager
    def security(self) -> Iterator[None]:
        self._security_open = True
        try:
            yield
        finally:
            self._security_open = False


# Backends.

class Backend(Protocol):
    simulated: bool

    def enumerate(self) -> list[dict[str, Any]]: ...

    def open(self, path: Any) -> Any: ...


class HidBackend:
    """Real USB HID through the PyPI hidapi bindings, same module choice as upstream."""

    simulated = False

    def __init__(self) -> None:
        self._hid: Any = None

    def _module(self) -> Any:
        if self._hid is None:
            try:
                from hidproxy import hid
            except ImportError as exc:
                raise VialticError("hid_backend_unavailable", f"The HID library could not be loaded: {exc}",
                                   next_steps=["Reinstall Vialtic; the release bundles its HID library."]) from exc
            self._hid = hid
        return self._hid

    def enumerate(self) -> list[dict[str, Any]]:
        return self._module().enumerate()

    def open(self, path: Any) -> Any:
        dev = self._module().device()
        dev.open_path(path)
        return dev


# Identity.

def _path_text(path: Any) -> str:
    return path.decode("utf-8", "surrogateescape") if isinstance(path, bytes) else str(path)


def device_id_for(desc: dict[str, Any]) -> str:
    seed = f"{desc['vendor_id']}:{desc['product_id']}:{_path_text(desc['path'])}"
    return "dev-" + hashlib.sha256(seed.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _display_name(desc: dict[str, Any]) -> str:
    name = f"{desc.get('manufacturer_string') or ''} {desc.get('product_string') or ''}".strip()
    return va.printable(name) or f"{desc['vendor_id']:04x}:{desc['product_id']:04x}"


def base_identity(desc: dict[str, Any], simulated: bool) -> DeviceIdentity:
    return DeviceIdentity(
        device_id=device_id_for(desc), name=_display_name(desc), vendor_id=f"0x{desc['vendor_id']:04x}",
        product_id=f"0x{desc['product_id']:04x}", serial_number=desc.get("serial_number") or "",
        path=_path_text(desc["path"]), simulated=simulated)


def identity_confidence(serial: str) -> tuple[IdentityConfidence, str]:
    remainder = serial.replace(va.VIAL_SERIAL_NUMBER_MAGIC, "").strip(" -_:")
    if remainder:
        return "usb_serial", "The USB serial carries a value beyond the Vial marker, so it may identify this unit."
    return "model_and_firmware", ("The USB serial is only the shared Vial marker and the Vial UID identifies the "
                                  "firmware, so identical keyboards cannot be told apart.")


def complete_identity(base: DeviceIdentity, kb: va.HeadlessKeyboard) -> DeviceIdentity:
    fingerprint = sha256_json(kb.definition)
    uid = f"0x{kb.keyboard_id:016x}"
    binding = sha256_json([base.vendor_id, base.product_id, base.serial_number, uid, fingerprint])[:24]
    confidence, note = identity_confidence(base.serial_number)
    return base.model_copy(update=dict(
        vial_uid=uid, via_protocol=kb.via_protocol, vial_protocol=kb.vial_protocol, layout_fingerprint=fingerprint,
        binding_id=binding, identity_confidence=confidence, identity_note=note))


def _hid_interface_problem(desc: dict[str, Any]) -> str | None:
    if desc.get("bus_type", BUS_USB) not in (BUS_USB, None, 0):
        return "not a USB connection (wireless is out of scope)"
    if desc.get("usage_page") != RAW_HID_USAGE_PAGE or desc.get("usage") != RAW_HID_USAGE:
        return "not the raw HID interface"
    return None


def problem_info(error: VialticError) -> ErrorInfo:
    return ErrorInfo(code=error.code, message=error.message, details=error.details, next_steps=error.next_steps)


UDEV_HINT = ("On Linux install a Vial-scoped udev rule (see docs/INSTALL.md), reload udev and replug the keyboard. "
             "This needs a separate elevated step; do not run Vialtic as root.")


def open_error(path: Any, exc: OSError) -> VialticError:
    """Separate permission problems from a device that is present but held or gone."""
    text = _path_text(path)
    denied = exc.errno in (errno.EACCES, errno.EPERM)
    if sys.platform.startswith("linux") and os.path.exists(text) and not os.access(text, os.R_OK | os.W_OK):
        denied = True
    if denied:
        return VialticError("permission_denied", f"No permission to open {text}.", next_steps=[UDEV_HINT])
    return VialticError("device_busy", f"Could not open {text}: {exc}",
                        next_steps=["Close other keyboard software such as Vial or VIA, then retry.",
                                    "Replug the keyboard if the problem persists."])


class Session:
    """An open keyboard. Holds the path lock and the process-wide keycode context."""

    def __init__(self, handle: Any, kb: va.HeadlessKeyboard, transport: GuardedTransport, identity: DeviceIdentity):
        self.handle = handle
        self.kb = kb
        self.transport = transport
        self.identity = identity

    def refresh(self) -> None:
        """Re-read device state, discarding upstream's in-memory copy."""
        guarded_read(self.kb.reload)
        self.identity = complete_identity(self.identity, self.kb)
        va.activate(self.kb)

    def snapshot(self, now: datetime | None = None) -> Snapshot:
        config = guarded_read(lambda: va.read_configuration(self.kb))
        coverage = va.coverage_of(self.kb, config)
        warnings = []
        if config.settings.unknown_qsids:
            warnings.append(f"QMK settings {config.settings.unknown_qsids} are exposed by the firmware but unknown to "
                            "the pinned Vial tables. Their raw answers are kept with the backup, but Vialtic never writes "
                            "them, so it cannot restore them if something else changes them.")
        if config.settings.unreadable_qsids:
            warnings.append(f"The firmware refused to report QMK settings {config.settings.unreadable_qsids}, so they are "
                            "not backed up and cannot be changed.")
        return Snapshot(
            created_at=now or datetime.now(timezone.utc), device=self.identity,
            capabilities=va.capabilities_of(self.kb), layout=va.layout_of(self.kb), configuration=config,
            family_fingerprints=family_fingerprints(config), coverage=coverage, warnings=warnings)


def family_fingerprints(config) -> dict[str, str]:
    data = config.model_dump(mode="json")
    # Opaque answers of unknown width may carry bytes that are not part of the value.
    data["settings"].pop("unknown_raw", None)
    return {family: sha256_json(data[family]) for family in va.ALL_FAMILIES}


def guarded_read(call: Callable[[], Any]) -> Any:
    """Translate failures from upstream's read path into public errors."""
    try:
        return call()
    except VialticError:
        raise
    except va.ProtocolError as exc:
        raise VialticError("unsupported_protocol", "This keyboard reports a protocol version Vialtic cannot use.") from exc
    except RuntimeError as exc:
        text = str(exc)
        code = "device_communication_failed" if "communicate" in text else "malformed_device_data"
        raise VialticError(code, f"Reading the keyboard failed: {text}",
                           next_steps=["Close other keyboard software, replug the keyboard and retry."]) from exc
    except (struct.error, KeyError, ValueError, IndexError, TypeError, AttributeError, OSError) as exc:
        raise VialticError("malformed_device_data", f"The keyboard returned unexpected data: {exc!r}",
                           next_steps=["Replug the keyboard and retry; report this firmware if it persists."]) from exc


class DeviceManager:
    """Discovery and sessions. Works identically for hardware and the emulator backend."""

    def __init__(self, backend: Backend, store: Store, max_retries: int | None = None, lock_timeout: float = 10.0):
        self.backend = backend
        self.store = store
        self.max_retries = max_retries
        self.lock_timeout = lock_timeout

    def interfaces(self) -> tuple[list[dict[str, Any]], list[IgnoredInterface]]:
        candidates, ignored = [], []
        for desc in self.backend.enumerate():
            serial = desc.get("serial_number") or ""
            vendor, product = f"0x{desc['vendor_id']:04x}", f"0x{desc['product_id']:04x}"
            if va.VIBL_SERIAL_NUMBER_MAGIC in serial:
                ignored.append(IgnoredInterface(vendor_id=vendor, product_id=product, name=_display_name(desc),
                                                reason="Vial bootloader; firmware flashing is out of scope"))
            elif va.VIAL_SERIAL_NUMBER_MAGIC in serial:
                problem = _hid_interface_problem(desc)
                if problem is None:
                    candidates.append(desc)
                elif "wireless" in problem:
                    ignored.append(IgnoredInterface(vendor_id=vendor, product_id=product, name=_display_name(desc),
                                                    reason=problem))
            elif desc.get("usage_page") == RAW_HID_USAGE_PAGE and desc.get("usage") == RAW_HID_USAGE:
                ignored.append(IgnoredInterface(vendor_id=vendor, product_id=product, name=_display_name(desc),
                                                reason="raw HID interface without the Vial serial marker (no Vial firmware)"))
        return candidates, ignored

    @contextlib.contextmanager
    def _opened(self, desc: dict[str, Any]) -> Iterator[tuple[va.HeadlessKeyboard, GuardedTransport, Any]]:
        path = desc["path"]
        with self.store.path_lock(_path_text(path), self.lock_timeout):
            try:
                handle = self.backend.open(path)
            except OSError as exc:
                raise open_error(path, exc) from exc
            try:
                transport = GuardedTransport(max_retries=self.max_retries)
                yield va.HeadlessKeyboard(handle, usb_send=transport), transport, handle
            finally:
                with contextlib.suppress(Exception):
                    handle.close()

    def probe(self, desc: dict[str, Any]) -> DeviceEntry:
        """Identify one candidate using read-only packets."""
        base = base_identity(desc, self.backend.simulated)
        try:
            with va.CONTEXT_LOCK, self._opened(desc) as (kb, _, _):
                try:
                    guarded_read(kb.reload_layout)
                except VialticError as exc:
                    if exc.code == "unsupported_protocol":
                        return DeviceEntry(identity=_partial_identity(base, kb), status="unsupported_protocol",
                                           problem=problem_info(_unsupported(kb)))
                    raise
                return DeviceEntry(identity=complete_identity(base, kb), status="ready")
        except VialticError as exc:
            status = {"permission_denied": "permission_denied", "device_busy": "busy", "lock_timeout": "busy",
                      "unlock_in_progress": "busy"}.get(exc.code, "error")
            return DeviceEntry(identity=base, status=status, problem=problem_info(exc))

    def list_devices(self) -> tuple[list[DeviceEntry], list[IgnoredInterface], dict[str, dict[str, Any]]]:
        candidates, ignored = self.interfaces()
        entries = [self.probe(desc) for desc in candidates]
        by_id = {device_id_for(desc): desc for desc in candidates}
        return entries, ignored, by_id

    def select(self, selector: str | None) -> tuple[DeviceEntry, dict[str, Any], list[DeviceEntry]]:
        """Resolve a user selection. Never picks silently among several candidates."""
        entries, _, by_id = self.list_devices()
        if selector:
            for entry in entries:
                if selector in (entry.identity.device_id, entry.identity.path):
                    return self._usable(entry), by_id[entry.identity.device_id], entries
            raise VialticError("device_not_found", f"No connected keyboard matches {selector!r}.",
                               details={"candidates": [e.identity.model_dump() for e in entries]},
                               next_steps=["Run `vialtic devices list`; device ids change when a keyboard is replugged."])
        if not entries:
            raise VialticError("no_device", "No USB Vial keyboard was found.",
                               next_steps=["Connect the keyboard by USB.", "Run `vialtic doctor` for diagnostics."])
        if len(entries) > 1:
            raise VialticError("device_selection_required", f"{len(entries)} keyboards are connected. Choose one.",
                               details={"candidates": [e.model_dump(mode="json") for e in entries]},
                               next_steps=["Ask the user which keyboard to use, then pass --device <device_id>."])
        return self._usable(entries[0]), by_id[entries[0].identity.device_id], entries

    def select_interface(self, selector: str | None) -> dict[str, Any]:
        """Selection that talks to no keyboard, for the commands that must work during an unlock."""
        candidates, _ = self.interfaces()
        if selector:
            for desc in candidates:
                if selector in (device_id_for(desc), _path_text(desc["path"])):
                    return desc
            raise VialticError("device_not_found", f"No connected keyboard matches {selector!r}.",
                               next_steps=["Run `vialtic devices list`; device ids change when a keyboard is replugged."])
        if not candidates:
            raise VialticError("no_device", "No USB Vial keyboard was found.", next_steps=["Connect the keyboard by USB."])
        if len(candidates) > 1:
            raise VialticError("device_selection_required", f"{len(candidates)} keyboards are connected. Choose one.",
                               next_steps=["Ask the user which keyboard to use, then pass --device <device_id>."])
        return candidates[0]

    @contextlib.contextmanager
    def security_session(self, desc: dict[str, Any]) -> Iterator[Session]:
        """Opens a keyboard without reading its configuration, which an unlock in progress does not allow."""
        with va.CONTEXT_LOCK, self._opened(desc) as (kb, transport, handle):
            try:
                data = kb.usb_send(handle, struct.pack("BB", proto.CMD_VIA_VIAL_PREFIX, proto.CMD_VIAL_GET_KEYBOARD_ID), retries=20)
                kb.vial_protocol, kb.keyboard_id = struct.unpack("<IQ", data[0:12])
            except RuntimeError as exc:
                raise VialticError("device_communication_failed", f"The keyboard stopped responding: {exc}") from exc
            yield Session(handle, kb, transport, base_identity(desc, self.backend.simulated))

    @staticmethod
    def _usable(entry: DeviceEntry) -> DeviceEntry:
        if entry.problem is not None:
            problem = entry.problem
            raise VialticError(problem.code, problem.message, details=problem.details, next_steps=problem.next_steps)
        return entry

    @contextlib.contextmanager
    def session(self, desc: dict[str, Any]) -> Iterator[Session]:
        """Open and fully read a keyboard. Reads only, until a caller opens a transport gate."""
        base = base_identity(desc, self.backend.simulated)
        with va.CONTEXT_LOCK, self._opened(desc) as (kb, transport, handle):
            try:
                guarded_read(kb.reload)
            except VialticError as exc:
                if exc.code == "unsupported_protocol":
                    raise _unsupported(kb) from exc
                raise
            yield Session(handle, kb, transport, complete_identity(base, kb))


def _partial_identity(base: DeviceIdentity, kb: va.HeadlessKeyboard) -> DeviceIdentity:
    uid = f"0x{kb.keyboard_id:016x}" if kb.keyboard_id not in (-1, None) else None
    return base.model_copy(update=dict(vial_uid=uid, via_protocol=kb.via_protocol, vial_protocol=kb.vial_protocol))


def _unsupported(kb: va.HeadlessKeyboard) -> VialticError:
    return VialticError(
        "unsupported_protocol",
        f"This keyboard reports VIA protocol {kb.via_protocol} and Vial protocol {kb.vial_protocol}, which this "
        "Vialtic build does not support. It will not be written to.",
        details={"via_protocol": kb.via_protocol, "vial_protocol": kb.vial_protocol,
                 "supported_via_protocols": va.SUPPORTED_VIA_PROTOCOL,
                 "supported_vial_protocols": va.SUPPORTED_VIAL_PROTOCOL},
        next_steps=["Update Vialtic to a version that supports this firmware."])
