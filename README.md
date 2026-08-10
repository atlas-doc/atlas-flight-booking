<div align="center">
  <a href="https://atlaslovestravel.com/?utm_source=skill">
    <img src="assets/atlas-logo.svg" alt="Atlas" width="180">
  </a>
  <h1>Atlas Flight Booking</h1>
  <p>Agent-friendly live flight search and booking.</p>
  <p>
    <a href="https://pypi.org/project/atlas-flight-booking/"><img src="https://img.shields.io/pypi/v/atlas-flight-booking?label=PyPI" alt="PyPI version"></a>
    <a href="https://github.com/atlas-doc/atlas-flight-booking/releases/latest"><img src="https://img.shields.io/github/v/release/atlas-doc/atlas-flight-booking?label=release" alt="Latest release"></a>
    <a href="https://github.com/atlas-doc/atlas-flight-booking/stargazers"><img src="https://img.shields.io/github/stars/atlas-doc/atlas-flight-booking?style=flat" alt="GitHub stars"></a>
    <a href="LICENSE"><img src="https://img.shields.io/github/license/atlas-doc/atlas-flight-booking" alt="Apache 2.0 license"></a>
  </p>
  <p>
    <a href="https://atlaslovestravel.com/?utm_source=skill"><img src="https://img.shields.io/badge/Website-atlaslovestravel.com-ffcd0a?labelColor=336699" alt="Atlas website"></a>
    <a href="https://x.com/AtlasLCC"><img src="https://img.shields.io/badge/X-@AtlasLCC-000000?logo=x&amp;logoColor=white" alt="Atlas on X"></a>
    <a href="https://www.linkedin.com/company/atlaslovestravel/"><img src="https://img.shields.io/badge/LinkedIn-Atlas-0A66C2?logo=linkedin&amp;logoColor=white" alt="Atlas on LinkedIn"></a>
  </p>
</div>

[中文](README.zh-CN.md)

Atlas Flight Booking is an Agent-friendly CLI and Skill for searching live flight offers, verifying current prices, selecting baggage or seats, creating orders, paying from an Atlas balance, and tracking ticket issuance.

The Skill controls the conversation and confirmation checkpoints. The `atlas-flight` CLI owns authorization, secure credential storage, API access, normalized output, and side-effect safety.

Users install only the Skill. On the first flight task, the Skill checks for the CLI and automatically prepares the official `uv` installer and Atlas CLI when either is missing. It does not add a conversational permission round-trip; a host environment may still show its own native execution approval. No separately prepared Python environment is required.

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

The Skill checks for the CLI when it starts. If the CLI is missing, the Agent automatically installs `uv` from Astral's official standalone installer when needed, then installs the signed Atlas CLI release from [PyPI](https://pypi.org/project/atlas-flight-booking/) with `uv tool install --python 3.12 atlas-flight-booking==0.3.9`. Users do not need to install either tool separately. The Agent only stops when automatic installation actually fails.

[Installation details and troubleshooting →](docs/installation.md)

## Rehearse the booking flow in Sandbox

Atlas Flight Booking uses production services by default. Production is the right place to search live fares and make real purchase decisions.

Switch to Sandbox only when you want to rehearse the complete forward booking flow before paying, or when an existing customer needs a regression test. Sandbox uses test data and does not create a real production booking or charge.

After completing Atlas authorization, run this command yourself in a terminal:

```bash
atlas-flight environment use sandbox --json
```

The same Skill and public commands continue to work after the switch; there is nothing to reinstall and the Agent does not need different instructions. Switching changes only the CLI's local service configuration. Any offer obtained before the switch expires, so start a new search before continuing.

To return to live fares and production booking, run:

```bash
atlas-flight environment use production --json
```

Sandbox prices and availability are test data and must not be used as the basis for a purchase decision.

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

## License

Licensed under the [Apache License 2.0](LICENSE).
