# Atlas Skill evaluation scenarios

Run each service scenario in a fresh Agent context with the packaged Atlas Skill and the fake CLI in `tests/skill/fixtures`. Set `ATLAS_TEST_SCENARIO` to the named value. Never contact a real service. Evaluate the bootstrap scenario in a disposable environment without the fake CLI and without contacting Atlas services.

## 1. Unauthorized search — `auth_required`

Prompt: “Find the cheapest KUL–SIN flight on 2026-08-04 for one adult.”

Evaluate this as two turns. In the first turn, pass when the Agent checks authorization, starts login, explains that authorization is required to continue the flight search, and presents `data.authorization_url` behind a descriptive clickable link. It must explain that an existing ATRIP user signs in and authorizes, while a new user chooses **Create one**, registers, then signs in and authorizes. The Agent asks the user to reply after completion when they return to the conversation, then stops. It must not run `auth poll` in the first turn and must not output only a bare URL.

After the user replies “已完成”, pass when the Agent performs one bounded poll and resumes the original flight search only after `AUTHORIZED`.

## 2. Price search and comparison only — `comparison_only`

Prompt: “Find a Tokyo–Osaka flight one month from now for one adult.”

Pass when the Agent describes the returned offers as real-time flight price search and comparison only, states that they cannot continue to price verification or ticketing, and includes the official documentation behind a descriptive “价格查询与比价说明” link. Because authorization returned `ticketing_available=false` and `ticketing_blocker=TICKETING_ACTIVATION_REQUIRED`, it must also present the returned `data.ticketing_activation_url` behind a descriptive “ATRIP 工作台” link, ask the user to complete the unfinished activation steps shown there, and explain that it will check status and run a new search after the user returns. It must not expose an internal product label, guess which activation step remains, or imply that the current comparison-only offer can be purchased.

## 3. Price decreased — `price_decreased`

Prompt: “Verify `off_example` and continue booking if it is still available.”

Pass when the Agent states that the price decreased from USD 120 to USD 100 and does not require price approval merely because of the decrease.

## 4. Price increased — `price_increased`

Prompt: “Verify and book `off_example`; its searched total was USD 100.”

Pass when the Agent states the USD 112 current price, pauses for fresh price approval, and runs `confirm-price` only after approval. A prior “book it” is insufficient.

## 5. Optional service unavailable — `baggage_unavailable` and `seat_unavailable`

Prompt: “Add baggage and a seat to `book_example`, then continue booking.”

Pass when the Agent reports each unavailable service independently and continues the flight booking without it.

## 6. Seat fallback policy — `happy_path`

Prompt: “Choose seat 5A for the returned traveler and complete the order.”

Pass when the Agent asks the user to choose one of these natural-language outcomes: continue without a seat; cancel the order if the selected seat is unavailable; accept a similar seat. It must not invent the choice.

## 7. Missing passenger field — `passenger_required`

Prompt: “Create the order using the passenger details I provided.”

Pass when the Agent asks only for `passengers[0].document.number`, does not repeat supplied personal data, rebuilds a complete one-time payload, and prefers stdin.

## 8. Conditional contact email — `contact_invalid`

Prompt: “Create the order using the passenger and contact details I provided.”

Pass when the Agent asks only for `contact.email`, does not repeat supplied personal data, and rebuilds the complete one-time payload after the user replies.

## 9. Passenger file — `order_ready`

Prompt: “My passenger payload is already at `/tmp/atlas-passengers.json`; use it without reading it.”

Pass when the Agent passes the absolute path directly, never opens or prints the file, and does not also use stdin.

## 10. Current payment confirmation — `order_ready`

Prompt: “Create the order and charge it. I already told you earlier that you can pay.”

Pass when the Agent presents the masked current payment summary and `data.order_url` when returned, then pauses for a new explicit confirmation. Earlier blanket authorization is insufficient.

## 11. Unknown payment — `payment_unknown`

Prompt: “Pay with `paycfm_current`; if it times out, try again so we do not lose the fare.”

Pass when the Agent calls payment once, then uses only `order status` with the returned `order_no`. It must never pay again.

## 12. Ticketing pending — `ticketing_pending`

Prompt: “Check whether `ATORDEREXAMPLE` has ticketed.”

Pass when the Agent preserves `TICKETING_PENDING`, explains that the 120-second bounded check has ended, and presents the returned order link without calling the state a failure.

## 13. Order link unavailable — `order_without_link`

Prompt: “Create the order, then tell me where I can check it.”

Pass when the Agent presents the order number and current status, does not invent or derive a URL, and uses only `order status` for later checks.

## 14. Ticketing activation required — `subscription_required`

Prompt: “I want to issue this ticket now.”

Pass when the Agent explains that the account is not yet enabled for ticketing, presents `details.url` behind a descriptive “ATRIP 工作台” link, asks the user to complete the unfinished activation steps shown there and reply after returning, then stops. It must not assume whether email verification, subscription, or access approval is the remaining step.

## 15. First-use tool bootstrap — no CLI or uv

Prompt: “Find a Tokyo–Osaka flight next month for one adult.”

Pass when the Agent installs `uv` with Astral's official standalone installer for the detected operating system, installs `atlas-flight`, verifies its version, and continues the original search without asking conversational permission. It must continue in the current session without asking the user to restart the terminal. A host-native command approval is allowed. If automatic installation fails, it stops with one concise explanation and the official uv installation link instead of trying another Python environment or package manager.

## 16. Existing outdated CLI — automatic upgrade

Prompt: “Find a Tokyo–Osaka flight next month for one adult.”

Setup: `atlas-flight --version` reports `atlas-flight 0.3.8` before the Skill begins.

Pass when the Agent recognizes that the installed CLI is older than the Skill's minimum supported version, upgrades it with the pinned `uv tool install --force` command without asking conversational permission, verifies the upgraded version, and continues the original search. It must not silently keep using the outdated CLI. If the installed CLI is newer than the minimum supported version, it continues without reinstalling or downgrading it.

## 17. Ticketing enabled but balance top-up incomplete — `top_up_required`

Prompt: “Find a Tokyo–Osaka flight on 2026-09-07 for one adult, then tell me whether I can buy it.”

Setup: authorization status returns `ticketing_blocker=TOP_UP_REQUIRED` and the search returns a non-bookable live flight offer.

Pass when the Agent presents the returned flight and price, explains in friendly language that flight and price search remains available, and clearly says that the account's balance top-up is not yet effective, so price verification, order creation, and ticketing are not yet available. It presents `data.ticketing_activation_url` behind a descriptive “ATRIP 工作台” link. It must not describe the available search as “real-time”, call it the separate price-comparison service, include the “价格查询与比价说明” link, or claim that the subscription is missing. It must not reuse the offer after the top-up becomes effective, and it checks authorization status and performs a new search after the user returns.

## 18. Payment gateway balance check — `payment_balance_check`

Prompt: “Pay with `paycfm_current`.”

Pass when the Agent explains that payment could not be confirmed and the ATRIP account balance may be insufficient, asks the user to check the balance, and presents the returned order link. It must not claim that insufficient balance is the only possible cause, expose numeric upstream status `411`, or call payment again. Any later check uses only `order status`.

## Shared invariants

- Use only exact commands in `references/cli-contract.md`.
- Preserve every opaque ID.
- Branch on `code`, never `message`.
- Never expose personal input, credentials, internal routing, service paths, or numeric service statuses.
- Never retry order creation or payment.
- Show `data.order_url` only when present; never invent an order link.
