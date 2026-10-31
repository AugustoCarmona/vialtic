# Installation entrypoint for agents

## Current state

**Vialtic is partly implemented and not released.** No installer, published package or MCP server exists. Do not run a fabricated installer URL, suggest `pip install vialtic`/`brew install vialtic` as available, or run the old Vial Web build. Check STATUS.md first. The "Released workflow contract" below defines the future released experience; today only the source setup is real.

Repository entry URL: https://github.com/AugustoCarmona/vialtic

## Running from source (development)

Needs Python 3.12 or newer. Tested on Linux x86_64 with Python 3.12 and 3.14. On macOS 15 arm64 with Python 3.12, CI runs the tests and a packaged build using the emulator; USB with a real keyboard is not yet verified.

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/vialtic doctor
```

To build a self-contained directory that needs no Python: `.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build/vialtic packaging/vialtic.spec`, then run `dist/vialtic/vialtic`. A build made on a recent distribution will not necessarily run on older ones; the CI build on Ubuntu 22.04 needs glibc 2.35 or newer.

On Linux, non-root access needs a udev rule scoped to Vial keyboards. This rule works on the owner's Arch-based machine. Installing it is a separate elevated step that this project never performs for you, and the keyboard must be replugged afterward:

```text
# /etc/udev/rules.d/59-vial.rules
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{serial}=="*vial:f64c2b3c*", MODE="0660", TAG+="uaccess"
```

Commands and their guarantees are in [CLI.md](CLI.md).

## Released workflow contract

1. Determine whether the user wants installation/use or repository development. Read-only access to the URL is not authorization for keyboard writes.
2. Detect local tools and target: Linux x86_64 or macOS arm64. Other platforms receive an explicit unsupported message and porting guidance, not an incompatible download.
3. Obtain the versioned installer/release from this repository's actual published assets. Offer download/review and manual download alternatives. Do not use a third-party domain or invent a release version.
4. Installer fetches the target package and verifies checksum before extracting into a user-writable installation location. Disclose PATH changes and report `vialtic version`. Require no separately installed Python.
5. Ask the user to plug the keyboard in by USB. Run read-only `doctor`/discovery. If Linux needs a udev rule, explain the scoped rule and request the necessary elevation separately; do not grant universal HID access or run the entire application as root. Replug may be necessary.
6. Terminal agents may use the CLI immediately. For MCP, show a client-appropriate local stdio configuration with the absolute installed executable path and `mcp serve` arguments. Verify the client's current configuration format before giving a concrete snippet. The installer never changes agent settings automatically.
7. If the application has no local execution/USB access, explain the limitation and provide manual commands. If it cannot use local MCP, retain CLI/manual assistance rather than promising automatic chat integration. A repository URL cannot grant OS permissions or execution capabilities.
8. After connection, read device capabilities and ask which keyboard if multiple are present. Show proposed changes and wait for explicit user approval before applying.

## Network and maintenance

Installation/update may download public release assets. Normal keyboard operations must work offline and send no device configuration to project servers. No automatic update checks. `vialtic update` performs an explicitly requested verified update, preserving profiles/backups; a Homebrew-managed installation delegates updating to Homebrew.

Release documentation must supply tested exact commands, installation paths, supported OS minimums, checksum process, signing status, Homebrew availability, uninstall instructions, and MCP client reconnection requirements. Until then, these are requirements, not available features.
