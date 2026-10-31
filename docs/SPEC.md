# Vialtic specification

Status: accepted product specification, implementation in progress (see STATUS.md). Date: 2026-10-30.
Owner: Augusto Carmona. Repository: https://github.com/AugustoCarmona/vialtic.

## 1. Purpose

Let a person ask their preferred local-capable agent to configure a USB-connected Vial keyboard in natural language. Vialtic supplies deterministic local tools, visual references, validation and reversible writes. It neither hosts nor implements an LLM. A user can use a cloud-backed or fully local agent; Vialtic never independently transmits keyboard configuration.

Example: “Make it easy to select numbered Omarchy workspaces on my Corne.” The agent inspects current assignments and asks about ambiguous intent, translates the request into Vial-supported key assignments, shows the exact proposal, waits for approval, applies it and verifies it. Vialtic does not inspect/edit Omarchy shortcuts or OS configuration. Host keyboard locale affects interpreted characters; show firmware keycodes accurately and do not claim to know OS mappings automatically.

## 2. Scope and platforms

- Python application, minimizing new code through reuse of Vial GUI internals.
- Linux x86_64 and macOS arm64 (Apple Silicon); target OS baselines must be fixed and documented by the packaging milestone using successful CI evidence.
- USB-connected keyboards with Vial firmware. Generic discovery and geometry; Corne for initial real tests. Neither Bluetooth nor wireless receiver support is a v1 requirement.
- CLI plus local stdio MCP server, sharing a service layer and data models.
- Terminal reference rendering and PNG image rendering for capable chat clients. No mandatory local browser, desktop window, or Qt event loop.
- All configuration functions exposed by the supported upstream Vial implementation, subject to device capabilities and protocol versions.
- Named profiles, JSON backups, `.vil` interchange, one-step previous-state restoration, bounded recovery, internal basic emulator.
- Local readable files, no encryption/password, no telemetry, accounts, API keys, background update checks or cloud backend.

Out of scope: OS settings/shortcuts, firmware compilation/flashing/reboot-to-bootloader actions, hardware firmware feature creation, passive keystroke monitoring, live pressed-key visualization, a built-in conversational model, remote USB access, subscriptions. Assigning a supported keycode is configuration; adding firmware capabilities is not.

“Any Vial keyboard” means no model allowlist or Corne-only assumptions, not a guarantee that unknown future protocol revisions can be written. Safely refuse unknown formats and report why.

## 3. User journeys

### 3.1 Install through an agent

User supplies the repository URL to a terminal agent or application agent and asks to install. README routes to INSTALL.md. A client with local execution downloads a release using the documented installer; a client lacking local tools provides exact manual steps. Agent configuration changes are presented as instructions/snippets, not performed by the installer. Explain any required MCP reconnection/restart without claiming the running client can automatically acquire new tools.

### 3.2 Inspect and propose

Discover USB Vial devices, report permissions/busy/unsupported errors separately, and ask which device if more than one is present. With one candidate, identify it visibly. Read capabilities and current state, translate intent into structured changes, validate, and generate a proposal containing before/after values, affected layers/features, warnings and a stable ID. Provide a compact terminal diff. Produce an image when the client can display it and the user requests/needs visual reference; textual summaries remain sufficient for approval. Resolve ambiguous keys/layers with the user instead of guessing silently.

### 3.3 Approve and apply

The agent presents the proposal and waits for explicit approval in the user's language. Approval authorizes only that immutable proposal on that selected device. New or changed operations require a new proposal and approval. No unconditional `--yes` write bypass is exposed as normal workflow. A CLI flag indicating agent-attested approval is permissible only against an existing immutable proposal; it is not proof of human consent and must be documented as such.

Immediately before writing, acquire exclusive per-device access, recheck identity, capabilities and baseline state, and durably save the previous configuration. Refuse stale proposals instead of overwriting changes made by another app. Apply validated operations and read back the result; report success only for verified outcomes. Release resources on completion. Vial writes are not atomic; describe partial results honestly.

### 3.4 Unlock

Preserve Vial's physical unlock mechanism. Report required positions with a text/image reference, start unlock only as part of an approved operation requiring it, poll with timeout/cancellation, then continue the same proposal if still valid. Never show a Qt unlock dialog or bypass firmware security. Lock a device unlocked by Vialtic after the operation when supported; report failures. Do not unexpectedly change a preexisting unlock state.

### 3.5 Undo and failure recovery

`undo` prepares a proposal restoring the one previous configuration, shows its diff and requires conversational approval. After successful manual undo, the prior current state becomes the new single previous state (allowing a reversible undo); never create an unbounded automatic history.

On interrupted/failed writes, preserve the pre-write backup and record progress metadata, then attempt restoration to that baseline within a bounded reconnect window (default 30 seconds). This restoration is disclosed in the original apply proposal and requires no second approval. Verify device identity and compatibility before any recovery write. If identity is ambiguous, permissions fail, unlock is required or timeout expires, stop, retain the backup and report partial/unknown state plus exact recovery commands. A process crash cannot trigger in-process recovery: on next invocation, detect pending recovery metadata and offer a recovery proposal; never silently write on startup.

Recovery metadata/journaling is not an additional configuration-history feature. Never replace the good backup with partial state, delete it after failure, or silently restore a different identical-model device.

## 4. Functional coverage

Inventory the pinned upstream implementation and maintain a capability matrix mapping read, validate, propose, apply, verify, backup and restore behavior for each feature:

| Feature | Required behavior |
| --- | --- |
| Keymap/layers | Read and assign keys, modifiers and supported composite keycodes; validate physical coordinates and layer references; respect firmware layer count. |
| Layout options | Discover, visualize and set firmware-provided physical layout alternatives. |
| Encoders | Assign supported clockwise/counterclockwise actions per layer; represent them in references. |
| Macros | Read/write supported macro formats, text, taps/presses/releases/delays; validate size and version; never execute macros on the host to test them. |
| Tap dance | All exposed entries and supported timing/action settings. |
| Combos | All exposed entries, actions and supported settings. |
| Key overrides | Read/write exposed override entries and flags. |
| Alternate repeat keys | Read/write exposed entries. |
| QMK settings | Exposed runtime configuration, including available tapping/timing settings; no compile-time feature promises. |
| Lighting | Supported backlight, RGB light and Vial RGB values/effects, with persistent save semantics where exposed. |
| Security | Unlock information/progress and lock as required for configuration. |

Do not manufacture an enabled capability when hardware lacks it. Unknown custom keycodes retain numeric values even if their names are unavailable. Never truncate values or silently drop unsupported profile fields. Unknown protocol versions must fail closed for writes while reporting available identification diagnostics.

Reset-to-default and firmware flashing are not needed to satisfy configuration coverage; do not expose destructive maintenance commands merely because they exist upstream. The feature matrix must explicitly classify nonconfiguration upstream functions.

## 5. Reuse-first architecture

The root checkout is Vial Web, not Vial GUI. Preserve its history. During implementation, vendor the smallest coherent reusable Vial Python tree under `vendor/vial/`, pinned with source revision and retained notices. Keep root web files during the initial extraction; move to a documented reference directory only if needed, without deletion/history replacement. Do not maintain a second full upstream application or fetch source at runtime.

Upstream `protocol/keyboard_comm.py` and feature modules already handle most device operations. Reuse keycode tables/codecs, KLE parsing, macro codecs, QMK tables and `.vil` serialization. Existing Qt imports cross protocol, utility, unlock and macro paths. Extract narrow pure-Python helpers and replace UI callbacks with headless status/callback interfaces; preserve behavior through focused tests. Do not reimplement the wire protocol to avoid these imports.

Proposed application tree (created during implementation):

```text
src/vialtic/
  cli.py                 # human output / stable JSON
  mcp_server.py          # stdio only, local process
  service.py             # common use cases and write gate
  models.py              # typed, versioned contracts
  device.py              # identity, discovery, locking
  vial_adapter.py        # reused upstream boundary
  proposals.py           # immutable plans and validation
  storage.py             # profiles, single backup, recovery metadata
  render.py              # physical-layout text and PNG
  emulator.py            # development backend only
vendor/vial/
tests/
docs/
packaging/
```

Prefer maintained Python MCP SDK, standard argparse (or a small CLI library if justified), typed validation, HIDAPI bindings, and a small raster renderer such as Pillow. Record exact supported Python/dependency versions after a tested extraction/packaging spike; old upstream Python/PyQt/PyInstaller pins are not approved dependencies. Avoid inheriting GUI-only dependencies. A static fallback PNG renderer from shared geometry is preferable to shipping a desktop UI solely to draw keys.

## 6. Identity, sessions and concurrency

Vial UID identifies a firmware/model, not necessarily an individual keyboard. Combine USB descriptors/serial, VID/PID, firmware UID, layout fingerprint and session path. USB paths may change on replug. If no unique physical serial exists, report that limitation and require user reselection when ambiguous. Do not automatically reconnect to the first matching VID/PID or UID.

Every mutating request references a selected device and proposal. Multiple candidates always trigger explicit choice when beginning configuration; no persistent default. Persisted backups may use a local binding identifier with confidence metadata, never a falsely globally unique UID. Serialize reads/writes that share a mutable upstream object; isolate per-device keycode contexts if upstream tables are global. Use an OS lock to prevent concurrent CLI and MCP writes. Other applications cannot be controlled; baseline checks detect external drift.

## 7. Data contracts and storage

All Vialtic JSON envelopes include `schema_version`. Public errors include stable `code`, human `message`, optional structured `details`, and actionable next steps. Set explicit limits for JSON sizes, decompressed firmware descriptions, macro buffers, indexes and protocol response sizes. Reject malformed or incompatible data before writing; never execute imported expressions, macros, arbitrary code or paths.

Snapshot envelope: device identity/confidence, firmware and VIA/Vial protocol versions, layout definition/fingerprint, capability inventory and limits, observed configuration values, timestamps and explicit coverage/unknown fields. A complete Vialtic snapshot includes lighting independently of `.vil`.

Proposal envelope: ID derived from canonical content, schema version, device binding, baseline fingerprint for all affected state/dependencies, ordered typed operations, before/after diff, warnings, verification and recovery coverage, creation/expiration (default 30 minutes). Changed or expired proposals require regeneration and approval. Arbitrary USB packets are never a public mutation API.

Apply result: proposal/device IDs, terminal outcome (`verified`, `failed_restored`, `partial`, `unknown`), per-operation results, verification differences, backup reference and recovery steps. Preserve raw numeric codes as well as labels to avoid lossy round trips. Distinguish saved/persistent values from transient lighting state.

Linux state defaults to `$XDG_STATE_HOME/vialtic` (fallback `~/.local/state/vialtic`); profiles under `$XDG_DATA_HOME/vialtic` (fallback `~/.local/share/vialtic`). macOS uses `~/Library/Application Support/Vialtic`. Use restrictive owner permissions, atomic writes and durable backup flush before mutation. No config contents in routine logs. Proposals and metadata are temporary and bounded, not a hidden history archive.

Exactly one automatic `previous.json` per bound physical keyboard. Explicit profiles are separate user-named JSON files and persist until deleted. A temporary pending snapshot may be needed for crash-safe rotation; never leave multiple historical automatic snapshots. If a complete backup cannot be created, block mutation rather than pretending undo is reliable. Changes to a feature that cannot be restored must be blocked with a specific explanation until a safe implementation exists.

## 8. CLI and MCP contracts

Finalize schemas in machine-readable files during the first contract milestone; the following command families and tool semantics are required. Reads do not imply approval to write.

| CLI family | MCP equivalent | Purpose |
| --- | --- | --- |
| `devices list`, `device inspect` | `list_devices`, `inspect_device` | Discovery, identity, capabilities, limits. |
| `snapshot`, `view` | `read_configuration`, `render_keyboard` | Read state, layer text/PNG, proposed changes. |
| `plan` | `create_proposal` | Validate typed changes, persist proposal and diff. |
| `apply <proposal-id>` | `apply_proposal` | Apply only after agent-attested user approval of that proposal. |
| `undo plan` | `create_undo_proposal` | Propose restoration of previous state. |
| `recover plan` | `create_recovery_proposal` | Explicit post-crash/interrupted recovery. |
| `profile list/save/show/delete/plan` | corresponding profile tools | Local profile management; application always creates a proposal. |
| `import`, `export` | `import_profile`, `export_configuration` | Vialtic JSON / `.vil`; import is local-only until proposed/applied. |
| `unlock status/start/poll`, `lock` | corresponding security tools | Guided physical unlock scoped to approved operation. |
| `doctor` | `diagnose_connection` | USB/permissions/version/busy errors; no config uploads. |
| `mcp serve` | — | Local stdio MCP server. |
| `update`, `version` | — | Explicit version/update management. |

Provide `--json` for machine CLI use, documented exit codes, schema/version/help discovery and explicit device selection. Long operations are bounded/cancellable and expose progress. No natural-language parser or agent-specific logic in the deterministic core. Agent guidance supplies instructions for intent translation, ambiguity resolution, preview/approval and undo.

MCP uses the maintained official Python SDK, advertises tool schemas and correct read/write annotations, and exposes guidance/resources for supported features and workflows. Approval may be provided through ordinary conversation; do not require optional MCP elicitation support. Return PNG content blocks plus text when the client supports them; CLI returns artifact paths plus text. If a client cannot display images, provide a text fallback. Local stdio avoids opening a listening network service. Hosting a remote URL is documentation distribution, not a remotely hosted MCP endpoint.

## 9. Rendering

Use firmware-provided KLE geometry and matrix mapping, not a Corne template. Preserve split gaps, key sizes/rotation, layout alternatives and encoders. Views select layer(s) explicitly, distinguish transparent vs disabled keys, show stable coordinates/key IDs and numeric/label fallback. Proposed views label before and after, not just color. Text remains readable without ANSI; PNG has sufficient contrast and a legend. Render unlock positions from the same geometry. No live keypress polling or browser requirement. Agents explain non-keymap changes in structured text alongside the reference.

## 10. Formats and compatibility

Reuse Vial `.vil` codec and preserve upstream version behavior. At reviewed revision, `.vil` is UTF-8 JSON bytes despite a misleading binary docstring. Existing serialization includes layout, encoders, layout options, macros, tap dance, combos, overrides, alternate repeat keys and settings; it does not serialize lighting. Do not call `.vil` a complete Vialtic backup or silently lose lighting on export. Export reports omissions; import validates version, keyboard/layout identity and capacities, and refuses incompatible application. Cross-device profiles are allowed only when compatibility is demonstrable; otherwise require explicit remapping, not a force bypass. Round-trip test using upstream fixtures/reference behavior. Unknown fields must be reported/preserved where safe, never executed.

## 11. Distribution and installation

Publish self-contained packaged Python builds on GitHub Releases for Linux x86_64 and macOS arm64. Build natively per target and test with no user Python installed. Investigate PyInstaller initially using current supported tooling, not old fbs scripts; choose one-file or bundled directory based on proven reliability, maintaining a single installer entrypoint.

Primary installation: versioned shell installer fetched using curl. It detects OS/architecture, fetches appropriate release, verifies published checksum before extraction, installs into user space, handles PATH, and prints version, permissions diagnostics and MCP connection instructions. No blanket root execution and no silent edits to agent settings. Explain Linux udev setup as a separate explicit elevated action; use a Vial-scoped rule and avoid global HID permissions. Installer changes and any PATH edits must be disclosed. Repeat installation should be predictable, not erase state/profiles.

Provide download-first/review instructions alongside a short curl form, manual release downloads, clean uninstall instructions preserving user state unless explicitly removed, and Homebrew as the secondary distribution route. Homebrew availability depends on an actually published tap/formula. Never document nonexistent releases or install URLs as working. Publish matching source and license materials. Define macOS signing/notarization approach and document any actual unsigned-build limitations before release; do not claim notarization without evidence. Linux glibc/native-HID dependencies need a documented tested baseline.

`vialtic update` is explicit, checks remote metadata only on invocation, downloads and verifies a release and replaces safely; it preserves local data and follows the installation channel (Homebrew installs are updated through Homebrew). Keyboard operations work offline. No startup update checks or background daemon.

## 12. Emulator and verification

Basic internal development emulator, not a full MCU/electronics emulator: preferably extend upstream DummyKeyboard to keep state for all supported configuration families, supply representative generic and split geometries, multiple devices, capability differences, identity ambiguity, lock/unlock and injected disconnection/partial-write failures. Isolate emulator data and require explicit activation; never silently fall back from hardware to simulated success.

Tests must cover reads, validation, no preapproval writes, stale proposals, locks, backup rotation, incomplete backups, undo, recovery and process restart, capability limits, unknown versions, `.vil` round trips, lighting backup, render geometry and CLI/MCP equivalence. Emulator tests do not establish physical compatibility.

Real-device acceptance uses owner's Corne for discovery, read-only snapshot, visual mapping, one approved reversible write, readback and undo. Features absent on Corne are labeled emulator-tested or require additional hardware evidence, never described as hardware-verified. macOS release requires native Apple Silicon install/USB evidence; absent hardware blocks that release claim, not unrelated development. CI runs platform-appropriate unit/integration/package checks without requiring a user's keyboard.

## 13. Repository and handoff

README starts with name/tagline, Important agent entry message and the owner's English origin note. Keep the origin note entirely lowercase, without quotation marks, preserving its wording, punctuation and `acp.` signature. AGENTS.md is vendor-neutral, routes installation vs development, identifies authoritative docs and requires updated status. Document contracts/platform interfaces sufficiently for a Windows agent to add a backend and packaging without redesigning the product. `.easter_egg/archivo.md` archives the conversation in its original Spanish; it is the explicit exception to English-only documentation and is not linked from README. It is hidden locally, tracked, and visible on GitHub after a user-authorized push.

Every code change must include a review and update of README.md from “Project status” onward and all affected documentation, including STATUS.md, to match the resulting implementation, behavior, limitations and verification evidence. Documentation synchronization is required to complete the change; it must not be deferred. Distinguish planned, implemented and hardware-verified behavior. Preserve the README introduction and origin note unless the owner requests changes.

## 14. Release completion

v1 is complete only after the shared core, CLI/MCP, all capability families, profiles/interchange, single undo state, recovery, both rendering modes, emulator, installers/update/Homebrew documentation, agent handoff, licenses and target platform checks pass the PLAN.md gates. Incremental milestones are development checkpoints, not scope reductions. Missing native/hardware verification is disclosed and tracked.
