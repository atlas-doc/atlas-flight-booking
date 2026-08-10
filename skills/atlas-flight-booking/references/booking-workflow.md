# Safe Booking Workflow

## 1. Search and verify

Run authorization status before search work. Search using complete inputs, present normalized offers, and preserve the selected `offer_id`. Verify only that returned ID.

If the search result has `bookable=false` or `price_status=reference`, present it as real-time flight price search and comparison only. Say that it cannot continue to price verification or ticketing, include the official “价格查询与比价说明” link from `SKILL.md`, and stop the booking workflow. Do not expose internal product labels.

Use `data.price_change`, `data.previous_price`, `data.current_price`, and `data.currency`:

- `unchanged`: state that the price was verified and continue.
- `decreased`: Tell the user when the verified price decreases, including both totals, and continue without requiring price approval.
- `increased`: show both totals and stop. Obtain new explicit confirmation when the verified price increases. Only after that confirmation run `atlas-flight booking confirm-price --booking-id {booking_id} --json`.

Never treat a prior search choice as approval of an increased price.

## 2. Optional services

Use `data.baggage_supported` and `data.seat_supported` from verification. List only supported services and only when the user wants them. Present safe option details and preserve the latest returned IDs.

`BAGGAGE_UNAVAILABLE` or `SEAT_UNAVAILABLE` means only that service cannot be selected. Continue the main booking flow. If both are unavailable, continue with the flight alone.

Before selecting a seat, ask what should happen if that exact seat becomes unavailable during order creation. Present these three choices in natural language:

- continue without a seat;
- cancel the order if the selected seat is unavailable;
- accept a similar seat.

Map the user's choice to the corresponding public CLI argument in `cli-contract.md`. Do not show implementation values.

## 3. Passenger input and order creation

Read `passenger-input.md`. Ask only for fields listed in `data.requirements.required_fields`; use the CLI-provided traveler IDs and passenger types. Prefer one-time passenger input through stdin. Do not place personal data directly in command arguments.

Run order creation once. Never retry it automatically. On `PAYMENT_CONFIRMATION_REQUIRED`, present:

- masked passengers and selected services;
- ticket, baggage, seat, fee, and total amounts when present;
- currency, payment deadline, and price-change summary;
- `data.order_url` when present, so the user can inspect the order on ATRIP. Never invent or derive a link when it is absent.

## 4. Current payment confirmation

Wait for explicit user approval after presenting the current payment summary. Earlier statements such as “book it,” blanket permission, or approval of an earlier price are not payment confirmation.

After approval, call `atlas-flight order pay` once with the exact `data.payment_confirmation_id` from that same current response. Never reuse another confirmation ID.

## 5. Payment and ticketing

Branch on the payment result:

- `TICKETED`: report success, masked ticket details, and the order link when returned.
- `TICKETING_PENDING`: explain that processing continues and show the order link when returned. The CLI has already polled for up to 120 seconds.
- `PAYMENT_BALANCE_CHECK_REQUIRED`: explain that payment could not be confirmed and the ATRIP balance may be insufficient; ask the user to check it. Never pay again.
- a stable terminal code: report the neutral meaning from `error-handling.md` and the order link when returned.
- an unclear payment result: query `atlas-flight order status --order-no {order_no} --json` when `order_no` is returned. Never call `atlas-flight order pay` again.

For later checks, use only `order status`. Do not describe pending ticketing as failure. If status is still pending after the bounded poll, show `data.order_url` only when present; otherwise say that status can be checked again later.

## Side-effect uncertainty

On `ORDER_CREATION_UNKNOWN`, do not create another order. Show the returned order link if available; otherwise report that the result could not be confirmed and do not invent a URL. On `PAYMENT_STATUS_UNKNOWN`, query only. On `PAYMENT_BALANCE_CHECK_REQUIRED`, tell the user the balance may be insufficient without claiming that this is the only cause. Never retry order creation or payment even when `retryable=true` appears elsewhere.
