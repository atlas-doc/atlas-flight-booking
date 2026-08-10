---
name: atlas-flight-booking
description: Use when a user wants to authorize Atlas, search or compare flights, verify a current fare, choose baggage or seats, create and pay for an order, or check ticketing status.
---

# Atlas Flight Booking

Operate through the Atlas Flight Booking CLI only. Preserve every opaque ID exactly. Branch on response `code`, never `message`, and present only normalized CLI fields.

## Start

Run `atlas-flight --version`. If the command is unavailable, explain that Atlas Flight Booking requires its CLI and ask for permission to install it. After permission, run exactly `uv tool install --python 3.12 atlas-flight-booking==0.3.8`, then verify `atlas-flight --version`. If `uv` is unavailable, direct the user to `https://docs.astral.sh/uv/getting-started/installation/` and stop; do not install through another Python environment.

Then run `atlas-flight auth status --json`. If authorization is required, follow `references/cli-contract.md`. Explain that Atlas authorization is required before the interrupted task can continue and present the returned URL as a descriptive clickable link. Briefly explain what the user will do on the page: sign in and authorize with an existing ATRIP account, or choose **Create one**, finish registration, then sign in and authorize. Ask the user to return to the conversation and reply after authorization is complete. Stop the current turn without polling. After the user confirms completion, poll once for at most 120 seconds and resume the interrupted task only after `AUTHORIZED`.

## Search and booking

Collect missing search inputs, search, and list offers. When an offer has `bookable=false` or `price_status=reference`, describe the results to the user as real-time flight price search and comparison only. State that they do not support continued price verification or ticketing, and include a descriptive link to `https://resources.atriptech.com/api-wen-dang/api-reference/booking-apis/price-compare-search#price-compare-search` labeled “价格查询与比价说明” in Chinese or its natural equivalent in the user's language. Do not expose internal product labels. Otherwise, verify only an `offer_id` returned by the CLI. Tell the user when the verified price decreases. Obtain new explicit confirmation when the verified price increases.

Follow `references/booking-workflow.md` for optional services, order creation, payment, and ticketing. Read `references/passenger-input.md` before collecting passenger details. Optional-service unavailability never blocks verification, order creation, payment, or ticketing.

Before payment, present the CLI's current payment summary and show `data.order_url` only when it is present, then wait for the user's explicit approval of that summary. Use the returned payment confirmation ID exactly once. If payment or order creation is uncertain, query status when an order number is available and never repeat a side-effecting command.

## Mandatory checkpoints

- 🛑 **AUTHORIZATION:** After presenting the authorization link and the existing-account/new-account instructions, stop the turn. Poll only after the user replies that authorization is complete.
- 🛑 **PRICE INCREASE:** After presenting the old and new totals, stop. Confirm the increased price only after the user explicitly accepts it.
- 🛑 **SEAT FALLBACK:** Before selecting a seat, stop until the user chooses what to do if that seat becomes unavailable during order creation.
- 🛑 **PAYMENT:** After presenting the current masked payment summary and any returned order link, stop. Pay only after the user explicitly approves that exact summary.

## Safety

Do not inspect configuration, credentials, or internal routing. Do not call services directly. Do not expose passenger input or copy it into chat, logs, command arguments, or saved Skill files. A retryable read-only failure permits at most one identical retry; never retry order creation or payment.

## References

Read `references/cli-contract.md` before constructing commands. Read `references/booking-workflow.md` for the end-to-end flow, `references/passenger-input.md` for one-time passenger input, and `references/error-handling.md` for every non-success code.
