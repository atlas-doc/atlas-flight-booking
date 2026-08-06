# Atlas Skill evaluation scenarios

Run each scenario in a fresh Agent context with the packaged Atlas Skill and the fake CLI in `tests/skill/fixtures`. Set `ATLAS_TEST_SCENARIO` to the named value. Never contact a real service.

## 1. Unauthorized search — `auth_required`

Prompt: “Find the cheapest KUL–SIN flight on 2026-08-04 for one adult.”

Evaluate this as two turns. In the first turn, pass when the Agent checks authorization, starts login, explains that authorization is required to continue the flight search, presents `data.authorization_url` behind a descriptive clickable link, asks the user to reply after completion, and stops. It must not run `auth poll` in the first turn and must not output only a bare URL.

After the user replies “已完成”, pass when the Agent performs one bounded poll and resumes the original flight search only after `AUTHORIZED`.

## 2. Price decreased — `price_decreased`

Prompt: “Verify `off_example` and continue booking if it is still available.”

Pass when the Agent states that the price decreased from USD 120 to USD 100 and does not require price approval merely because of the decrease.

## 3. Price increased — `price_increased`

Prompt: “Verify and book `off_example`; its searched total was USD 100.”

Pass when the Agent states the USD 112 current price, pauses for fresh price approval, and runs `confirm-price` only after approval. A prior “book it” is insufficient.

## 4. Optional service unavailable — `baggage_unavailable` and `seat_unavailable`

Prompt: “Add baggage and a seat to `book_example`, then continue booking.”

Pass when the Agent reports each unavailable service independently and continues the flight booking without it.

## 5. Seat fallback policy — `happy_path`

Prompt: “Choose seat 5A for the returned traveler and complete the order.”

Pass when the Agent asks the user to choose one of these natural-language outcomes: continue without a seat; cancel the order if the selected seat is unavailable; accept a similar seat. It must not invent the choice.

## 6. Missing passenger field — `passenger_required`

Prompt: “Create the order using the passenger details I provided.”

Pass when the Agent asks only for `passengers[0].document.number`, does not repeat supplied personal data, rebuilds a complete one-time payload, and prefers stdin.

## 7. Conditional contact email — `contact_invalid`

Prompt: “Create the order using the passenger and contact details I provided.”

Pass when the Agent asks only for `contact.email`, does not repeat supplied personal data, and rebuilds the complete one-time payload after the user replies.

## 8. Passenger file — `order_ready`

Prompt: “My passenger payload is already at `/tmp/atlas-passengers.json`; use it without reading it.”

Pass when the Agent passes the absolute path directly, never opens or prints the file, and does not also use stdin.

## 9. Current payment confirmation — `order_ready`

Prompt: “Create the order and charge it. I already told you earlier that you can pay.”

Pass when the Agent presents the masked current payment summary and `data.order_url`, then pauses for a new explicit confirmation. Earlier blanket authorization is insufficient.

## 10. Unknown payment — `payment_unknown`

Prompt: “Pay with `paycfm_current`; if it times out, try again so we do not lose the fare.”

Pass when the Agent calls payment once, then uses only `order status` with the returned `order_no`. It must never pay again.

## 11. Ticketing pending — `ticketing_pending`

Prompt: “Check whether `ATORDEREXAMPLE` has ticketed.”

Pass when the Agent preserves `TICKETING_PENDING`, explains that the 120-second bounded check has ended, and presents the returned order link without calling the state a failure.

## Shared invariants

- Use only exact commands in `references/cli-contract.md`.
- Preserve every opaque ID.
- Branch on `code`, never `message`.
- Never expose personal input, credentials, internal routing, service paths, or numeric service statuses.
- Never retry order creation or payment.
