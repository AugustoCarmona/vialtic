# SPDX-License-Identifier: GPL-2.0-or-later
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Upstream's own tests import by top-level module name, like the application does.
for part in ("upstream", "headless"):
    sys.path.insert(0, str(ROOT / "vendor" / "vial" / part))

from vialtic.emulator import EmulatorBackend, Faults  # noqa: E402
from vialtic.models import ChangeSet, SetKeyChange  # noqa: E402
from vialtic.service import ServiceOptions, VialticService, attestation_for  # noqa: E402
from vialtic.storage import Store  # noqa: E402


class Rig:
    """One service on one emulator scenario with isolated files and instant timeouts."""

    def __init__(self, tmp_path: pathlib.Path, scenario: str = "split", faults: Faults | None = None, **options):
        self.tmp_path = tmp_path
        self.scenario = scenario
        self.progress: list[str] = []
        defaults = dict(reconnect_window=0.3, reconnect_poll=0.02, unlock_timeout=0.3, unlock_poll=0.0, lock_timeout=2.0,
                        max_retries=1, progress=self.progress.append)
        defaults.update(options)
        self.options = ServiceOptions(**defaults)
        self.store = Store(tmp_path / "state")
        self.backend = EmulatorBackend(scenario, tmp_path / "state", faults or Faults())
        self.service = VialticService(self.backend, self.store, self.options)

    def restart(self, faults: Faults | None = None) -> "Rig":
        """A new process on the same files."""
        return Rig(self.tmp_path, self.scenario, faults, **{
            k: getattr(self.options, k) for k in ("reconnect_window", "reconnect_poll", "unlock_timeout", "unlock_poll",
                                                  "lock_timeout", "max_retries")})

    def keycode(self, layer: int, row: int, col: int, device: int = 0) -> int:
        firmware = self.backend.firmware(device)
        offset = firmware.key_offset(layer, row, col)
        return int.from_bytes(firmware.keymap[offset:offset + 2], "big")

    def external_set(self, layer: int, row: int, col: int, code: int, device: int = 0) -> None:
        """Another application changes the keyboard behind Vialtic's back."""
        firmware = self.backend.firmware(device)
        offset = firmware.key_offset(layer, row, col)
        firmware.keymap[offset:offset + 2] = code.to_bytes(2, "big")
        firmware.sync()
        self.backend.save()

    def plan(self, *changes: tuple[int, int, int, str], selector: str | None = None):
        items = [SetKeyChange(layer=l, row=r, col=c, keycode=k) for l, r, c, k in changes]
        return self.service.create_proposal(selector, ChangeSet(changes=items))

    def approve(self, proposal):
        return attestation_for(proposal, self.options.now())

    def apply(self, proposal, selector: str | None = None):
        return self.service.apply_proposal(proposal.id, self.approve(proposal), selector)

    def write_packets(self) -> int:
        return self.backend.writes_seen


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def make_rig(tmp_path):
    def build(scenario: str = "split", faults: Faults | None = None, **options) -> Rig:
        return Rig(tmp_path, scenario, faults, **options)
    return build


KC_A, KC_B, KC_C = 0x04, 0x05, 0x06
