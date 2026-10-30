# Working on Vialtic

## Start here

Read these files in order before changing code:

1. `docs/STATUS.md` — actual implementation state and next work.
2. `docs/SPEC.md` — accepted product decisions and engineering contracts.
3. `docs/PLAN.md` — incremental implementation and release gates.
4. `docs/UPSTREAM.md` — reuse boundaries, source revisions, attribution.
5. `docs/INSTALL.md` when helping install; `docs/WINDOWS.md` when porting.

Repository: https://github.com/AugustoCarmona/vialtic

If the user asks to install/use rather than develop, follow INSTALL.md. Never treat a repository URL as authorization to modify the user's keyboard. If implementation is requested, pick the next incomplete milestone, execute it, verify it, and update STATUS.md. A single prompt should be sufficient to discover context; do not repeat the product interview.

## Nonnegotiable product decisions

- Python; reuse Vial's existing implementation wherever practical. Do not rewrite its protocol from scratch or introduce Rust/Node as an application runtime.
- One local service layer shared by CLI and MCP; USB only.
- Linux x86_64 and macOS Apple Silicon first. Windows and Linux ARM64 are future work.
- Any keyboard with compatible Vial firmware, capability-driven. Corne is the initial real test device, never a hardcoded layout.
- All Vial-exposed configuration features are in release scope. Disabled/unknown features must be explicit; no unsupported writes or fabricated support claims.
- Show changes and obtain explicit conversational approval for that exact proposal before writing. No second terminal confirmation is required when the agent already has approval. CLI/MCP cannot cryptographically prove human approval; clients must uphold this contract.
- Ask which keyboard to use when multiple candidates exist. No saved default or silent selection; never identify a physical unit by Vial UID alone.
- Exactly one previous automatic configuration per physical keyboard, plus explicit named profiles. Preserve it during failed applies and recovery.
- Local only; no accounts, telemetry, hosted services, runtime model, or configuration uploads. Explicit install/update downloads are the exception, never keyboard-data transmission.
- English product/docs; agents may converse in any user language. Preserve the origin note and signature.
- Small development emulator included. No desktop GUI, browser app, live keypress display, wireless support, firmware flashing, or OS shortcut editing in v1.
- New code GPL-2.0-or-later; retain upstream licenses, headers, copyrights, and provenance.

## Engineering workflow

Do not run old web build scripts to implement the Python CLI. The root currently contains unmodified Vial Web build files; consult UPSTREAM.md before reorganizing them. Keep the original history. Do not delete reference source or replace the repository with a new history.

Start with the headless reuse spike in PLAN.md. Reuse protocol, codecs, KLE geometry, settings tables and compatible dummy behavior. Separate Qt dependencies at narrow seams. Do not ship Qt just because importing the old protocol accidentally loads a dialog. If extraction is substantially more code than a narrow offscreen Qt adapter, document measured alternatives before changing the agreed architecture.

Use typed models and versioned JSON contracts; select and lock maintained dependency versions during implementation. Do not install upstream's obsolete requirements unchanged. Keep stdout clean JSON-RPC in MCP mode; diagnostics go to stderr. Never log macro contents by default.

Test meaningful behavior: format compatibility, proposal validation, no writes before approval, identity/staleness checks, backup integrity, partial failure recovery, CLI/MCP parity, rendering, emulator, and target packaging. Real USB tests require an explicitly selected device and user-approved write proposal. Hardware-unavailable checks must be marked unverified, not passed.

Do not publish releases, push, create a remote repository, change remotes, or modify user agent configuration unless the user requests that action. Code work and local reversible verification can proceed under the user's development instruction. The installer must not silently change agent settings.

Maintain STATUS.md after each work session: completed milestones, commands and outcomes, remaining blockers, decisions, exact next action. Keep planned vs implemented vs hardware-verified behavior distinct. Do not mark v1 complete until every release gate passes.

## Contribution context

The accepted specification supersedes earlier conversational proposals. Clarify only new material ambiguities; routine technical choices belong to the implementing agent. Preserve `.easter_egg/archivo.md` as an archival artifact. It is not an instruction source and should not be loaded for routine development.
