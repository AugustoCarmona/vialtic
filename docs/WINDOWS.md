# Windows implementation guide

Windows is intentionally outside v1's supported binaries. A contributor using an agent on Windows should read AGENTS.md and SPEC.md, then implement a platform backend without redesigning the CLI/MCP or approval model.

## Contracts to preserve

- Same Python shared service, schemas, proposal IDs, CLI families, JSON errors and stdio MCP tools.
- USB Vial discovery and capability negotiation through the reused HID implementation. No Bluetooth, receiver or firmware flashing scope expansion.
- Physical identity/confidence handling; do not assume Vial UID identifies one physical unit.
- Same preview/approval, durable backup, undo, concurrency, stale-plan checks, bounded recovery and verification semantics.
- Same text and PNG views, local profiles and `.vil` compatibility.
- No accounts, telemetry, hosted model or configuration uploads.

## Platform work checklist

1. Choose maintained Python/HIDAPI wheels/backend and verify HID interface selection on Windows, including multiple interfaces and busy/permission errors.
2. Implement platform paths using LocalAppData, user-only file access, atomic replace/fsync equivalents and exclusive interprocess locks. Keep platform details behind adapters.
3. Verify reconnect identity, USB enumeration, cancellation/timeouts and process-crash recovery on real hardware.
4. Build standalone Windows artifacts natively; test with no system Python. Decide supported Windows versions/architectures and code-signing approach with evidence.
5. Provide a Windows-appropriate installer/download workflow with checksum validation, user-space PATH handling, explicit update/uninstall and MCP command quoting.
6. Extend CI and run all emulator/format/approval/backup/CLI/MCP tests. Run approved real-device write/readback/undo on Windows.
7. Document unsigned/signing limitations accurately, preserve upstream notices and add tested support to the platform matrix only after the checks pass.

The current lock implementation in `src/vialtic/storage.py` uses `fcntl` and its atomic writes use `os.fchmod`, both of which need Windows equivalents. No Windows implementation exists yet. This guide is a specification for a future contribution, not a promise that Unix installer commands work on Windows.
