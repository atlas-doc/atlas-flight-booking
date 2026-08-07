# Installation

[中文](installation.zh-CN.md)

## Agent installation (recommended)

Install the Skill:

```bash
npx --yes skills add https://github.com/atlas-doc/atlas-flight-booking --skill atlas-flight-booking
```

When the Skill first needs Atlas Flight Booking, it checks for `atlas-flight`. If the CLI is missing, the Agent explains why it is required and asks for permission. After approval, the Agent installs the CLI and verifies the installed version. Users do not normally need to install the CLI themselves.

## Requirements

- Windows, macOS, or Linux;
- Node.js with `npx` for installing the Skill;
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the Agent-managed CLI installation.

`uv` downloads and manages Python 3.12 when needed. A separately prepared Python environment is not required.

## Manual CLI recovery

This is an advanced recovery path for support and development. Use it only when the Agent-managed installation cannot complete.

```bash
uv tool install --python 3.12 git+https://github.com/atlas-doc/atlas-flight-booking.git@v0.3.6
```

Verify the installation:

```bash
atlas-flight --version
atlas-flight doctor --json
```

The version command should report `atlas-flight 0.3.6`.

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
uv tool install --force --python 3.12 git+https://github.com/atlas-doc/atlas-flight-booking.git@v0.3.6
```

Reinstalling the CLI does not remove Atlas authorization stored in the operating system's secure credential facility.

## Uninstall

```bash
uv tool uninstall atlas-flight-booking
```

CLI installation and Atlas authorization are separate. Installing the CLI does not authorize an account; the Skill starts authorization only when a requested task requires it.
