# Atlas Flight Booking

[中文](README.zh-CN.md)

Atlas Flight Booking is an Agent-friendly CLI and Skill for searching live flight offers, verifying current prices, selecting baggage or seats, creating orders, paying from an Atlas balance, and tracking ticket issuance.

The Skill controls the conversation and confirmation checkpoints. The `atlas-flight` CLI owns authorization, secure credential storage, API access, normalized output, and side-effect safety.

Users install only the Skill. On first use, the Skill checks for the CLI and, when it is missing, explains why it is required and asks for permission before the Agent installs it. The machine only needs [uv](https://docs.astral.sh/uv/getting-started/installation/) available; no separately prepared Python environment is required.

## Supported workflow

- browser authorization with a bounded poll;
- live flight search and normalized offer comparison;
- fare and availability verification, including price-change handling;
- optional baggage and seat selection;
- one-time passenger input through stdin or an existing local JSON file;
- order creation with a masked payment summary and Atlas order link;
- single-use balance payment after explicit confirmation;
- ticketing polling for up to 120 seconds and later order queries.

Optional baggage or seat unavailability does not block the main booking flow. This release does not implement refunds, cancellations, changes, credit-card payment, or other after-sales operations.

## Install the Skill

```bash
npx --yes skills add https://github.com/atlas-doc/atlas-flight-booking --skill atlas-flight-booking
```

The Skill checks for the CLI when it starts. If the CLI is missing, it explains the requirement and asks for permission before installing anything.

## Start a flight search

An Agent using the Skill will collect missing inputs and operate the CLI. The equivalent direct command is:

```bash
atlas-flight search \
  --origin KUL \
  --destination SIN \
  --depart 2026-08-20 \
  --adults 1 \
  --json
```

All subcommands return one stable JSON envelope. Agents branch on the response `code`, preserve opaque IDs exactly, and never inspect credentials or internal routing.

## Safety boundaries

- Authorization links are shown with context and require the user to complete authorization in the browser.
- A verified price increase requires a new explicit confirmation.
- Passenger details are one-time input and are excluded from persisted booking state and normalized errors.
- The current masked payment summary and Atlas order link are shown before payment.
- A payment confirmation ID is single-use; uncertain order creation or payment is never repeated.
- Credentials and private workflow data use the operating system's secure credential facility, with no plaintext fallback.

## Develop and verify offline

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen mypy src/atlas_cli
bash tests/skill/validate-skill.sh skills/atlas-flight-booking
uv run --frozen python -m scripts.scan_secrets .
uv build
```

These checks use mocks and fixtures; they do not prove online booking behavior. Online acceptance must be performed manually with approved accounts and data.

## License

Licensed under the [Apache License 2.0](LICENSE).
