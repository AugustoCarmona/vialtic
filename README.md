# Vialtic

Vial-based agentic software for configuring your keyboard.

> [!IMPORTANT]
> To start modifying this repository, give its URL to your agent and ask it to read it:
> **https://github.com/AugustoCarmona/vialtic**
>
> The agent should read [`AGENTS.md`](AGENTS.md), follow its reading order, and use the specification and project status to collaborate with you. Agents with repository-reading and local execution capabilities can work directly; other clients may require you to provide files or enable local tools.

this idea was born on friday, october 30, after an extreme day of work. to clear my head a little, i had two pieces of toast as an afternoon snack and started configuring my new omarchy. it turns out my corne keyboard has trouble accessing the numbered workspaces, so i started configuring it through vial. but with codex open on my main screen, i realized it would be much easier to simply ask my agent. that is how this project was born. if you are using it, i hope you enjoy it. and if you are going to contribute, i hope you enjoy it too. a big hug. acp.

## Project status

**Specification complete; Vialtic implementation has not started.** No Vialtic installer, executable, release, or MCP server is available yet. The existing source and build scripts belong to upstream Vial Web and do not implement the product described here.

Vialtic will let your existing agent inspect and configure USB-connected Vial keyboards. It will show proposed changes, wait for your approval, save the previous configuration, apply changes, and verify the result. Terminal agents receive text views; clients that support images can show a keyboard image in the conversation.

The first release targets Linux x86_64 and macOS Apple Silicon. Vialtic runs locally without accounts, telemetry, a hosted model, API keys, or configuration uploads. Your chosen agent has its own data-processing behavior.

## Start contributing

Give your agent the repository URL and ask:

> Read AGENTS.md and implement the next incomplete milestone in docs/PLAN.md, following docs/SPEC.md. Update docs/STATUS.md with the result and verification evidence.

For installation after releases exist, the same repository will direct agents to [`docs/INSTALL.md`](docs/INSTALL.md). Downloading the source alone does not install Vialtic. There is no one-click installation claim for clients without local execution access.

## Documentation

- [Agent instructions](AGENTS.md)
- [Complete product and engineering specification](docs/SPEC.md)
- [Implementation plan and acceptance criteria](docs/PLAN.md)
- [Current state and handoff](docs/STATUS.md)
- [Installation contract](docs/INSTALL.md)
- [Windows implementation guide](docs/WINDOWS.md)
- [Upstream reuse and attribution](docs/UPSTREAM.md)

## Freedom and attribution

New Vialtic code will use GPL-2.0-or-later. Preserve upstream notices and licenses for all reused material. Vialtic is based on [Vial](https://github.com/vial-kb/vial-gui); this checkout originated from [Vial Web](https://github.com/vial-kb/vial-web). See [COPYING](COPYING) and [upstream provenance](docs/UPSTREAM.md).

<p align="center">
  <img src="assets/mascot/turtle-tug-of-war.png" alt="Two turtles pulling on a lettuce leaf" width="640">
</p>
