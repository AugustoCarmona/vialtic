# SPDX-License-Identifier: GPL-2.0-or-later
"""Stable public error codes and their process exit codes. See docs/CLI.md."""
from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_DEVICE_SELECTION = 3
EXIT_PERMISSION = 4
EXIT_BUSY = 5
EXIT_UNSUPPORTED = 6
EXIT_INVALID = 7
EXIT_APPROVAL = 8
EXIT_STALE = 9
EXIT_APPLY_FAILED = 10
EXIT_RECOVERY_PENDING = 11
EXIT_INTERRUPTED = 12
EXIT_COMMUNICATION = 13
EXIT_BACKUP = 14

ERROR_EXIT_CODES: dict[str, int] = {
    "internal_error": EXIT_INTERNAL,
    "usage_error": EXIT_USAGE,
    "no_device": EXIT_DEVICE_SELECTION,
    "device_not_found": EXIT_DEVICE_SELECTION,
    "device_selection_required": EXIT_DEVICE_SELECTION,
    "device_ambiguous": EXIT_DEVICE_SELECTION,
    "permission_denied": EXIT_PERMISSION,
    "device_busy": EXIT_BUSY,
    "lock_timeout": EXIT_BUSY,
    "hid_backend_unavailable": EXIT_UNSUPPORTED,
    "unsupported_protocol": EXIT_UNSUPPORTED,
    "unsupported_operation": EXIT_UNSUPPORTED,
    "malformed_device_data": EXIT_UNSUPPORTED,
    "restore_unsupported": EXIT_UNSUPPORTED,
    "write_blocked": EXIT_UNSUPPORTED,
    "invalid_request": EXIT_INVALID,
    "invalid_keycode": EXIT_INVALID,
    "invalid_coordinate": EXIT_INVALID,
    "invalid_layer_reference": EXIT_INVALID,
    "no_changes": EXIT_INVALID,
    "nothing_to_undo": EXIT_INVALID,
    "nothing_to_recover": EXIT_INVALID,
    "invalid_file": EXIT_INVALID,
    "approval_required": EXIT_APPROVAL,
    "proposal_not_found": EXIT_STALE,
    "proposal_expired": EXIT_STALE,
    "proposal_stale": EXIT_STALE,
    "proposal_tampered": EXIT_STALE,
    "device_changed": EXIT_STALE,
    "unlock_required": EXIT_APPROVAL,
    "recovery_pending": EXIT_RECOVERY_PENDING,
    "cancelled": EXIT_INTERRUPTED,
    "unlock_timeout": EXIT_INTERRUPTED,
    "device_communication_failed": EXIT_COMMUNICATION,
    "backup_failed": EXIT_BACKUP,
    "storage_failed": EXIT_BACKUP,
}


class VialticError(Exception):
    """An expected failure with a stable code and actionable next steps."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None,
                 next_steps: list[str] | None = None):
        if code not in ERROR_EXIT_CODES:
            raise ValueError(f"unregistered error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.next_steps = next_steps or []

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES[self.code]
