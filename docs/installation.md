# Installation

[中文](installation.zh-CN.md)

## Agent installation (recommended)

Install the Skill:

```bash
npx --yes skills add https://github.com/atlas-doc/atlas-flight-booking-skill --skill atlas-flight-booking
```

Whenever the Skill needs Atlas Flight Booking, it checks the `atlas-flight` version. If the CLI is missing or older than the Skill's minimum supported version, the Agent automatically installs `uv` from Astral's official standalone installer when needed, installs or upgrades the CLI, verifies the version, and resumes the original flight task. It does not downgrade newer versions or add a conversational permission round-trip; the host may still display its own native execution approval. Users do not normally install either tool themselves.

## Requirements

- Windows, macOS, or Linux;
- Node.js with `npx` for installing the Skill;
- Internet access so the Agent can obtain [uv](https://docs.astral.sh/uv/getting-started/installation/) and the signed CLI package.

The Agent installs `uv` when it is absent. `uv` then downloads and manages Python 3.12 when needed. A separately prepared Python environment is not required.

## Manual CLI recovery

This is an advanced recovery path for support and development. Use it only when the Agent-managed installation cannot complete.

```bash
uv tool install --force --python 3.12 atlas-flight-booking==0.3.12
```

Verify the installation:

```bash
atlas-flight --version
atlas-flight doctor --json
```

The version command should report `atlas-flight 0.3.12` or newer.

## Command not found after installation

Show the directory where `uv` installs executable files:

```bash
uv tool dir --bin
```

Ask `uv` to add that directory to the shell environment:

```bash
uv tool update-shell
```

Close and reopen the terminal before verifying `atlas-flight --version` again.

## Reinstall or repair

```bash
uv tool install --force --python 3.12 atlas-flight-booking==0.3.12
```

Reinstalling the CLI does not remove Atlas authorization stored in the operating system's secure credential facility.

## Uninstall

```bash
uv tool uninstall atlas-flight-booking
```

CLI installation and Atlas authorization are separate. Installing the CLI does not authorize an account; the Skill starts authorization only when a requested task requires it.
