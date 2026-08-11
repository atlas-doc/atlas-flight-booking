---
name: atlas-flight-booking
description: Use when a user asks what Atlas Flight Booking can do, wants to authorize Atlas, search or compare flights using exact or flexible dates and time preferences, verify a current fare, choose baggage or seats, create and pay for an order, or check ticketing status.
---

# Atlas Flight Booking

Operate through the Atlas Flight Booking CLI only. Preserve every opaque ID exactly. Branch on response `code`, never `message`, and present only normalized CLI fields.

## Capability questions

When the user only asks what this Skill can do or how to use it, answer directly in the user's language. Do not check or install the CLI, check authorization, or call any service. Keep the answer concise and use the following structure or its natural equivalent:

“Atlas Flight Booking 支持使用自然语言查询和预订航班。你可以提供准确日期，也可以描述日期范围、时间偏好或价格要求；我会搜索并比较符合条件的航班。

例如：

- ‘查询 9 月 4 日上海到东京的航班。’
- ‘比较 9 月 1 日至 7 日上海到东京的最低价格。’
- ‘找未来两周东京到大阪最便宜的上午直飞航班。’

选定航班后，还可以继续核价、选择行李和座位、创建订单、余额支付并查询出票状态。涉及涨价、座位替代或支付时，会先获得你的确认。”

Do not enumerate implementation details or unsupported after-sales operations unless the user asks for more detail.

## Start

Treat `0.3.12` as the minimum supported CLI version. Run `atlas-flight --version` and parse the stable `atlas-flight X.Y.Z` output. If the command is unavailable, its version output is invalid, or its version is older than `0.3.12`, bootstrap or upgrade the required tooling immediately and continue the interrupted flight task. Do not reinstall when the installed version is `0.3.12` or newer, and never downgrade a newer CLI. Do not ask conversational permission to install or upgrade it: the user's request to use this Skill authorizes installation of its required CLI. Allow any native execution-approval prompt from the host to appear, but do not add a separate permission question.

If `uv` is unavailable, detect the operating system and run the applicable official standalone installer:

- macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`; use `wget -qO- https://astral.sh/uv/install.sh | sh` only when `curl` is unavailable.
- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.

Use the newly installed executable in the current session, including `$HOME/.local/bin/uv` on macOS/Linux or `$HOME\.local\bin\uv.exe` on Windows when `uv` is not yet on `PATH`. Run exactly `uv tool install --force --python 3.12 atlas-flight-booking==0.3.12`, using that resolved executable path when the CLI is missing, invalid, or older than the minimum supported version. If `atlas-flight` is not yet on `PATH`, resolve the tool binary directory with `uv tool dir --bin` and invoke `atlas-flight` from there; do not ask the user to restart the terminal. Verify that `atlas-flight --version` now reports `0.3.12` or newer and continue. Only stop when the automatic installation or upgrade actually fails; then give one concise failure explanation and the official `https://docs.astral.sh/uv/getting-started/installation/` link. Do not fall back to another Python environment or package manager.

Then run `atlas-flight auth status --json`. Retain `data.ticketing_activation_url` and `data.ticketing_blocker` only when returned; never invent or derive either field. If authorization is required, follow `references/cli-contract.md`. Explain that Atlas authorization is required before the interrupted task can continue and present the returned URL as a descriptive clickable link. Briefly explain what the user will do on the page: sign in and authorize with an existing ATRIP account, or choose **Create one**, finish registration, then sign in and authorize. Ask the user to return to the conversation and reply after authorization is complete. Stop the current turn without polling. After the user confirms completion, poll once for at most 120 seconds and resume the interrupted task only after `AUTHORIZED`.

## Search and booking

Collect missing search inputs, search, and list offers. When the retained `data.ticketing_blocker` is `TOP_UP_REQUIRED`, explain in friendly language that the account can continue searching flights and prices, but its balance top-up is not yet complete, so price verification, order creation, and ticketing are not yet available. Do not describe this availability as “real-time.” When `data.ticketing_activation_url` is returned, use this Chinese wording: “当前账户可以继续查询航班和价格，但充值状态尚未生效，因此暂时不能继续核价、创建订单或出票。你可以前往 [ATRIP 工作台]({ticketing_activation_url})查看充值状态；状态更新后，我会重新检查；如果你已选择某个报价，我会先核价确认最新价格。” Use the returned URL for `{ticketing_activation_url}`. Retain a selected offer whose `price_status=current`. After the user says the top-up is effective, check authorization status. If `data.ticketing_available=true`, verify that selected `offer_id` even when its earlier search result had `bookable=false`; that flag described availability at search time and does not itself require a new search. Treat the earlier amount only as the previous price. Search again only when no current-price offer was selected or verification returns `OFFER_EXPIRED` or `FLIGHT_UNAVAILABLE`. Never reuse an offer whose `price_status=reference`. Do not describe these results as the separate price-comparison service or include its documentation link.

### Flexible search

Resolve relative or fuzzy dates against the current date and the user's timezone. Present the interpreted absolute dates with the results. For one exact departure date, run one new search. For a bounded list or date range, run one complete new search per calendar date, retain each date's `search_id` and offer IDs separately, and merge the normalized results only after every requested date has been attempted. Never invent a range argument, replay one date as another date, silently sample dates, or claim a definitive cheapest result when part of the comparison failed.

Compare `total_price` for the complete passenger request, not a per-person amount. Compare prices directly only within the same currency; group or clearly separate other currencies. Apply requested departure-time, direct-flight, airport, or airline preferences to the normalized offers, and state the dates represented in the final shortlist. Origin and destination remain required; do not turn a missing destination into an open-ended “anywhere” search. Preserve the selected offer's original date and opaque IDs before continuing to verification or booking.

Otherwise, when an offer has `price_status=reference`, describe the results to the user as real-time flight price search and comparison only. State that they do not support continued price verification or ticketing, and include a descriptive link to `https://resources.atriptech.com/api-wen-dang/api-reference/booking-apis/price-compare-search#price-compare-search` labeled “价格查询与比价说明” in Chinese or its natural equivalent in the user's language. When authorization returned `ticketing_available=false`, `data.ticketing_blocker=TICKETING_ACTIVATION_REQUIRED`, and `data.ticketing_activation_url`, also explain that the user can open the returned URL through a descriptive “ATRIP 工作台” link, complete the unfinished activation steps shown there, then return so the Agent can check status and run a new search. Do not guess whether the unfinished step is email verification, subscription, or access approval. Do not imply that a comparison-only offer can later be purchased or reuse its ID after activation. Do not expose internal product labels. Otherwise, verify only an `offer_id` returned by the CLI. Tell the user when the verified price decreases. Obtain new explicit confirmation when the verified price increases.

Follow `references/booking-workflow.md` for optional services, order creation, payment, and ticketing. Read `references/passenger-input.md` before collecting passenger details. Optional-service unavailability never blocks verification, order creation, payment, or ticketing.

Before payment, present the CLI's current payment summary and show `data.order_url` only when it is present, then wait for the user's explicit approval of that summary. Use the returned payment confirmation ID exactly once. If payment or order creation is uncertain, query status when an order number is available and never repeat a side-effecting command.

On `PAYMENT_BALANCE_CHECK_REQUIRED`, explain that payment could not be confirmed and that the ATRIP account balance may be insufficient. Ask the user to check the balance, show `data.order_url` only when returned, and never submit payment again. Use only `order status` for any later status check.

## Mandatory checkpoints

- 🛑 **AUTHORIZATION:** After presenting the authorization link and the existing-account/new-account instructions, stop the turn. Poll only after the user replies that authorization is complete.
- 🛑 **PRICE INCREASE:** After presenting the old and new totals, stop. Confirm the increased price only after the user explicitly accepts it.
- 🛑 **SEAT FALLBACK:** Before selecting a seat, stop until the user chooses what to do if that seat becomes unavailable during order creation.
- 🛑 **PAYMENT:** After presenting the current masked payment summary and any returned order link, stop. Pay only after the user explicitly approves that exact summary.

## Safety

Do not inspect configuration, credentials, or internal routing. Do not call services directly. Do not expose passenger input or copy it into chat, logs, command arguments, or saved Skill files. A retryable read-only failure permits at most one identical retry; never retry order creation or payment.

## References

Read `references/cli-contract.md` before constructing commands. Read `references/booking-workflow.md` for the end-to-end flow, `references/passenger-input.md` for one-time passenger input, and `references/error-handling.md` for every non-success code.
