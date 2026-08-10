---
name: atlas-flight-booking
description: Use when a user wants to authorize Atlas, search or compare flights, verify a current fare, choose baggage or seats, create and pay for an order, or check ticketing status.
---

# Atlas Flight Booking

Operate through the Atlas Flight Booking CLI only. Preserve every opaque ID exactly. Branch on response `code`, never `message`, and present only normalized CLI fields.

## Start

Treat `0.3.11` as the minimum supported CLI version. Run `atlas-flight --version` and parse the stable `atlas-flight X.Y.Z` output. If the command is unavailable, its version output is invalid, or its version is older than `0.3.11`, bootstrap or upgrade the required tooling immediately and continue the interrupted flight task. Do not reinstall when the installed version is `0.3.11` or newer, and never downgrade a newer CLI. Do not ask conversational permission to install or upgrade it: the user's request to use this Skill authorizes installation of its required CLI. Allow any native execution-approval prompt from the host to appear, but do not add a separate permission question.

If `uv` is unavailable, detect the operating system and run the applicable official standalone installer:

- macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`; use `wget -qO- https://astral.sh/uv/install.sh | sh` only when `curl` is unavailable.
- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.

Use the newly installed executable in the current session, including `$HOME/.local/bin/uv` on macOS/Linux or `$HOME\.local\bin\uv.exe` on Windows when `uv` is not yet on `PATH`. Run exactly `uv tool install --force --python 3.12 atlas-flight-booking==0.3.11`, using that resolved executable path when the CLI is missing, invalid, or older than the minimum supported version. If `atlas-flight` is not yet on `PATH`, resolve the tool binary directory with `uv tool dir --bin` and invoke `atlas-flight` from there; do not ask the user to restart the terminal. Verify that `atlas-flight --version` now reports `0.3.11` or newer and continue. Only stop when the automatic installation or upgrade actually fails; then give one concise failure explanation and the official `https://docs.astral.sh/uv/getting-started/installation/` link. Do not fall back to another Python environment or package manager.

Then run `atlas-flight auth status --json`. Retain `data.ticketing_activation_url` and `data.ticketing_blocker` only when returned; never invent or derive either field. If authorization is required, follow `references/cli-contract.md`. Explain that Atlas authorization is required before the interrupted task can continue and present the returned URL as a descriptive clickable link. Briefly explain what the user will do on the page: sign in and authorize with an existing ATRIP account, or choose **Create one**, finish registration, then sign in and authorize. Ask the user to return to the conversation and reply after authorization is complete. Stop the current turn without polling. After the user confirms completion, poll once for at most 120 seconds and resume the interrupted task only after `AUTHORIZED`.

## Search and booking

Collect missing search inputs, search, and list offers. When the retained `data.ticketing_blocker` is `TOP_UP_REQUIRED`, explain in friendly language that the account can continue searching real-time flights and prices, but its balance top-up is not yet complete, so price verification, order creation, and ticketing are not yet available. Present `data.ticketing_activation_url` through a descriptive “ATRIP 工作台” link so the user can complete the top-up. Do not describe these results as the separate price-comparison service or include its documentation link. After the user says the top-up is complete, check authorization status and run a new search; never reuse the earlier offer.

Otherwise, when an offer has `bookable=false` or `price_status=reference`, describe the results to the user as real-time flight price search and comparison only. State that they do not support continued price verification or ticketing, and include a descriptive link to `https://resources.atriptech.com/api-wen-dang/api-reference/booking-apis/price-compare-search#price-compare-search` labeled “价格查询与比价说明” in Chinese or its natural equivalent in the user's language. When authorization returned `ticketing_available=false`, `data.ticketing_blocker=TICKETING_ACTIVATION_REQUIRED`, and `data.ticketing_activation_url`, also explain that the user can open the returned URL through a descriptive “ATRIP 工作台” link, complete the unfinished activation steps shown there, then return so the Agent can check status and run a new search. Do not guess whether the unfinished step is email verification, subscription, or access approval. Do not imply that a comparison-only offer can later be purchased or reuse its ID after activation. Do not expose internal product labels. Otherwise, verify only an `offer_id` returned by the CLI. Tell the user when the verified price decreases. Obtain new explicit confirmation when the verified price increases.

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
