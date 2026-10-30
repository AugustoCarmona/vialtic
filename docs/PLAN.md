# Implementation plan

All milestones are pending. Update STATUS.md with evidence as work proceeds. Do not implement the entire project without validating the reuse boundary first.

## M0 — Reuse and packaging feasibility

- Fetch pinned Vial GUI source at the revision in UPSTREAM.md; inventory provenance and feature modules.
- Demonstrate headless import, device enumeration and state serialization by adapting existing Python logic. Extract utility, unlock and settings/macro codec seams that currently import Qt.
- Reuse KLE/keycode handling. Adapt DummyKeyboard for minimal stateful development use.
- Add modern pyproject metadata and lock tested dependency versions; keep upstream license headers.
- Produce a minimal packaged CLI on Linux x86_64 and macOS arm64 through target-native CI, with a recorded OS/Python baseline and HID dependency strategy. Native USB verification may remain pending but must be identified.
- Gate: no GUI needed for read-only core and no protocol rewrite. Document actual dependency graph and any justified narrow adapter decision before proceeding.

## M1 — Contracts, discovery and read-only views

- Implement shared typed models, schema fixtures, JSON errors/exit codes and version/help discovery.
- Generic USB discovery, capability/limit inventory, identity confidence, explicit selection among multiple devices, permissions/busy diagnostics.
- Read full snapshot including lighting; implement physically accurate text/PNG rendering and keycode labels with numeric fallback.
- Basic emulator with generic and split layouts; explicit simulated mode.
- Gate: read-only CLI and emulator can inspect/view without any device writes; geometry is not Corne-specific; unit tests exercise malformed data and unknown protocols.

## M2 — Proposal, approval, write and recovery core

- Immutable plans, stable IDs, typed operations, before/after diffs, expiry and baseline checks.
- Per-device locks; durable complete pre-write backup; partial-write metadata; operation readback.
- Approval attestations scoped to proposal/device, guided physical unlock, relock and cancellation.
- One automatic previous state; undo proposal; bounded reconnect/restore; restart recovery path.
- Emulator fault injection for disconnect, timeout, external drift, ambiguous identity and failed restore.
- Gate: tests prove zero writes before approval, no stale/unsupported writes, backup survives failures and incomplete state is never reported as success. Perform one approved Corne change/readback/undo when available.

## M3 — Complete Vial configuration coverage

- Build a checked feature matrix against pinned upstream modules.
- Implement all exposed configuration families using the same proposal pipeline: keymap/layers, layout options, encoders, macros, tap dance, combos, key overrides, alternate repeat keys, QMK settings, backlight/RGB/Vial RGB.
- Validate bounds, cross-layer references, buffer capacity, protocol variants and unknown custom codes.
- Gate: every supported feature has read/validate/propose/apply/verify/backup/restore tests; unsupported firmware features return explicit capability errors. Persistent lighting is covered outside `.vil`.

## M4 — Profiles and interchange

- Local named profile CRUD; Vialtic schema-versioned JSON import/export.
- Upstream-compatible `.vil` round trips, omission reporting, compatibility checks and proposal-only restoration.
- Storage permission/atomicity checks, one-backup rotation semantics and migration/version refusal tests.
- Gate: existing compatible Vial files can be imported/exported without silent loss, and foreign/malformed/oversized profiles cannot cause writes.

## M5 — MCP and agent workflows

- Local stdio MCP uses the shared service, official Python SDK and structured schemas.
- Add image/text tool results, feature guidance/resources and English CLI messages; agent responses may be in user language.
- Approval is ordinary conversation; no dependence on client-specific elicitation or rendering APIs.
- Document workflows for terminal agents and local-tool-enabled application clients. Verify actual client capabilities before naming integration as supported.
- Gate: CLI/MCP parity for discovery, plan, approved apply, undo and profiles; clean JSON-RPC stdout; text fallback for image-incompatible clients. Client without local execution receives honest manual installation guidance.

## M6 — Install, update and release

- Publishable native packages, matching source/license notices, checksums and a documented integrity/authenticity model.
- Agent installation guide, curl installer, user-space/PATH behavior, Linux scoped udev guidance, manual downloads, uninstall and explicit update command.
- Homebrew tap/formula plan and tested installation when publishing is authorized. Do not claim a nonexistent tap works.
- macOS arm64 signing/notarization decision and accurate user instructions; Linux baseline/native library compatibility evidence.
- Gate: clean target installations with no Python runtime prerequisite, offline configuration operation, explicit update and preserved local state. No user configuration/telemetry egress. No automatic agent-settings modifications.

## M7 — Final acceptance and contributor handoff

- Run complete CLI and MCP story on owner's Corne: discovery, selection, read, view, proposal, approval, write, readback, undo.
- Native Apple Silicon package and USB evidence; Linux x86_64 evidence. Record every unverified hardware-specific feature.
- Review capability matrix, Windows guide, install docs and agent entrypoint for another contributor starting with only the repo URL.
- Gate: fresh agent can find authoritative context and next task, all v1 gates pass, and any publishing/push occurs only when requested.
