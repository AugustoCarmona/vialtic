# Vialtic

Vial-based agentic software for configuring your keyboard.

> [!IMPORTANT]
> To start modifying this repository, give its URL to your agent and ask it to read it:
> **https://github.com/AugustoCarmona/vialtic**
>
> The agent should read [`AGENTS.md`](AGENTS.md), follow its reading order, and use the specification and project status to collaborate with you. Agents with repository-reading and local execution capabilities can work directly; other clients may require you to provide files or enable local tools.

this idea was born on friday, october 30, after an extreme day of work. to clear my head a little, i had two pieces of toast as an afternoon snack and started configuring my new omarchy. it turns out my corne keyboard has trouble accessing the numbered workspaces, so i started configuring it through vial. but with codex open on my main screen, i realized it would be much easier to simply ask my agent. that is how this project was born. if you are using it, i hope you enjoy it. and if you are going to contribute, i hope you enjoy it too. a big hug. acp.

## Project status

**Early development, usable for a first test from source.** There is no release, installer or MCP server yet. Implemented so far: a command line tool that reads USB Vial keyboards, renders them as text or PNG, and changes every Vial configuration feature the firmware exposes through reviewable proposals with a backup, read-back verification, undo and failure recovery.

| Area | State |
| --- | --- |
| Discovery, full read-only snapshot, text and PNG views | Implemented. Read-only use verified on a real Corne on Linux x86_64. |
| Proposal, approval, write, read-back, undo, recovery for keymap keys | Implemented and tested on the emulator. One key change, read-back and undo verified on a real Corne on Linux x86_64. Failure recovery is emulator-tested only. |
| Encoders, layout options, tap dance, combos, key overrides, alternate repeat keys, QMK settings, macros, lighting | Implemented and tested on the emulator only, including a fault sweep. **Not yet verified on any real keyboard.** [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) has the details and [`docs/HARDWARE_TEST.md`](docs/HARDWARE_TEST.md) is the test plan. |
| Named profiles, `.vil` and Vialtic file import and export | Implemented and tested on the emulator only. Applying one creates an ordinary proposal. **Not yet verified on any real keyboard.** |
| MCP server for agents | Planned (M5). Agents with terminal access can use the CLI. |
| Installer, updates, Homebrew | Planned (M6). |
| macOS Apple Silicon | Tests and a packaged build pass in CI on macOS 15 arm64, using the emulator. Not yet tried with a real keyboard. |

Vialtic will let your existing agent inspect and configure USB-connected Vial keyboards: it shows proposed changes, waits for your approval, saves the previous configuration, applies changes and verifies the result. It runs locally without accounts, telemetry, a hosted model, API keys, or configuration uploads. Your chosen agent has its own data-processing behavior.

## Try it from source

Needs Python 3.12 or newer. See [`docs/INSTALL.md`](docs/INSTALL.md) for Linux permissions.

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .

.venv/bin/vialtic doctor                 # reads only
.venv/bin/vialtic view --layer 0         # reads only
.venv/bin/vialtic view --emulator split  # no keyboard needed
```

Changing a key takes a proposal, which writes nothing, followed by an explicit apply:

```sh
.venv/bin/vialtic plan --set '1:0,2=LGUI(KC_3)'
.venv/bin/vialtic apply <proposal-id> --user-approved
.venv/bin/vialtic undo plan
```

Other features go through a change file, described with commands, guarantees and limitations in [`docs/CLI.md`](docs/CLI.md).

## Start contributing

Give your agent the repository URL and ask:

> Read AGENTS.md and implement the next incomplete milestone in docs/PLAN.md, following docs/SPEC.md. Update docs/STATUS.md with the result and verification evidence.

Run the tests with `.venv/bin/python -m pytest`. They use the built-in emulator and need no keyboard.

## Documentation

- [Agent instructions](AGENTS.md)
- [Complete product and engineering specification](docs/SPEC.md)
- [Implementation plan and acceptance criteria](docs/PLAN.md)
- [Current state and handoff](docs/STATUS.md)
- [CLI contract](docs/CLI.md)
- [Capability matrix](docs/CAPABILITIES.md)
- [Hardware test plan](docs/HARDWARE_TEST.md)
- [Installation contract](docs/INSTALL.md)
- [Windows implementation guide](docs/WINDOWS.md)
- [Upstream reuse and attribution](docs/UPSTREAM.md)

## Freedom and attribution

New Vialtic code uses GPL-2.0-or-later. Preserve upstream notices and licenses for all reused material. Vialtic is based on [Vial](https://github.com/vial-kb/vial-gui); this checkout originated from [Vial Web](https://github.com/vial-kb/vial-web). See [COPYING](COPYING) and [upstream provenance](docs/UPSTREAM.md).

<p align="center">
  <img src="assets/mascot/turtle-tug-of-war.png" alt="Two turtles pulling on a lettuce leaf" width="640">
</p>
