# Project status and handoff

Last updated: 2026-10-31.

## Actual state

- Replaced the README footer illustration with the owner-provided `Turtle Tug-of-War Over Lettuce.png`, stored as `assets/mascot/turtle-tug-of-war.png`, with descriptive alt text. Verified the copied PNG matches the supplied file, the README asset reference resolves, and `git diff --check` passes. The owner requested committing and pushing this replacement together with the pending documentation changes. Implementation remains paused; no runtime or hardware behavior changed.

- Added the owner-provided walking turtle mascot from `vialtic-web-kit.zip` to the end of README.md, after Freedom and attribution. Verified PNG integrity, README asset reference and `git diff --check`; no runtime or hardware behavior changed.

- Documentation maintenance rule added to AGENTS.md and SPEC.md: every code change must include reviewing and updating README.md from “Project status” onward and all affected documentation before the change is considered complete. Documentation-only verification: `git diff --check` passed.

- Product interview complete and accepted decisions recorded in SPEC.md.
- Agent entrypoint, plan, install contract, Windows guide and upstream review written.
- This checkout originates from Vial Web; its application source/build scripts remain unchanged. The inherited `.github/workflows/main.yml` web-build CI was removed at the owner’s request because it does not validate Vialtic. Python CI will be introduced during implementation. It is not a working Vialtic application.
- Python Vial GUI source inspected separately to locate reuse/headless seams; no application code vendored yet.
- No Vialtic runtime dependencies installed, builds run, tests implemented, installer published, or GitHub repository created by this work. The owner subsequently requested committing and pushing the documentation to the existing empty personal repository.
- Local directory: `~/Projects/vialtic`. Personal remote `origin`: `https://github.com/AugustoCarmona/vialtic.git`. Existing original remote remains `upstream`.
- New-code license decision: GPL-2.0-or-later. COPYING retains upstream GPL v2 text.

## Milestones

M0–M7 in PLAN.md are all pending. Documentation planning is complete; product implementation has not started.

## Evidence collected

- Inspected pinned Vial Web `src/build.sh`: bundles Vial GUI Python and Qt into browser build.
- Inspected Vial GUI protocol modules, imports, DummyKeyboard, `.vil` save/restore, unlock UI and requirements at revision recorded in UPSTREAM.md.
- Confirmed `.vil` is JSON bytes and omits lighting; protocol reuse requires separating Qt-dependent imports.
- Documentation checks passed: relative Markdown links resolve; `git diff --check` is clean; transcript contains all 48 user turns through the final interview answer; the initial specification changed only README.md among tracked upstream files; the inherited web CI was subsequently removed; COPYING is byte-identical to the reviewed upstream text. No runtime or hardware compatibility is claimed from these checks.

## Next action for a development agent

Implementation is paused at the owner’s request while specifications are refined. The README origin note is now entirely lowercase without quotation marks; SPEC.md and AGENTS.md reflect this requirement. Verification: the paragraph matches its previous wording converted to lowercase, and `git diff --check` passes. Continue specification review with the owner before starting M0.

Read AGENTS.md, SPEC.md, PLAN.md and UPSTREAM.md. Implement M0: establish a small headless adapter around pinned Vial GUI logic with modern packaging feasibility checks. Do not rewrite the protocol, implement a GUI, or treat old web build scripts as the Python product build. Update this file with actual commands/outcomes and the next incomplete task.

## Unresolved engineering work (not more product-interview questions)

- Choose tested Python/dependency versions and target OS minimums.
- Measure headless extraction scope and validate HID backend packaging.
- Define machine-readable schemas/tool signatures/exit-code map per accepted semantic contracts.
- Implement all runtime behavior and emulator.
- Obtain native macOS Apple Silicon and Corne hardware verification when needed. Missing hardware is a verification limitation, not permission to claim passing results.
- Implement release installer/update/Homebrew path and decide signing/notarization on evidence.

The owner explicitly requested the initial documentation push. The destination was checked and had no advertised refs before publication. Future releases and pushes still require the corresponding user instruction.
