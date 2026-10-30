# Installation entrypoint for agents

## Current state

**Vialtic is specified but not implemented or released.** Do not run a fabricated installer URL, suggest `pip install vialtic`/`brew install vialtic` as available, or run the old Vial Web build to install the future product. Check STATUS.md first. This guide defines the required released installation experience.

Repository entry URL: https://github.com/AugustoCarmona/vialtic

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
