# Atlas Flight Booking CLI Contract

## Rules

Use only the commands listed here. Run `atlas-flight --version` without `--json`; request `--json` for every subcommand. Consume one JSON envelope and branch on `code`. Never run `--help`, probe variants, inspect configuration, or call a service directly.

Preserve `search_id`, `offer_id`, `booking_id`, `traveler_id`, `segment_id`, option IDs, `order_no`, and `payment_confirmation_id` exactly as returned.

## Authorization and diagnostics

| Operation | Exact command |
| --- | --- |
| Version | `atlas-flight --version` |
| Authorization status | `atlas-flight auth status --json` |
| Start authorization | `atlas-flight auth login --json` |
| Poll once | `atlas-flight auth poll --timeout 120 --json` |
| Diagnose readiness | `atlas-flight doctor --json` |

On `AUTHORIZATION_REQUIRED`:

1. Run login. Explain in the user's language that Atlas authorization is required before the interrupted task can continue.
2. Use the login response's `data.authorization_url` unchanged as the target of a descriptive clickable label such as “Open Atlas authorization.” Do not display a bare URL or expose other login response fields. In the user's language, give these brief page instructions:
   - If the user already has an ATRIP account, sign in and complete authorization.
   - If the user does not have an account, choose **Create one**, finish registration, then sign in and complete authorization.
   Ask the user to reply after completing authorization, remind them to return to the conversation, state that the interrupted task will then continue, and stop the current turn without polling.
3. After the user confirms completion, run the bounded poll once. Resume the interrupted task only after `AUTHORIZED`. On `AUTH_PENDING`, explain that authorization is still incomplete and wait for the user; do not start an automatic polling loop.

On `AUTHORIZED`, retain `data.ticketing_available` and the optional `data.ticketing_activation_url` and `data.ticketing_blocker` for the current conversation. Show the URL only when it was returned and ticketing is unavailable. `TICKETING_ACTIVATION_REQUIRED` means the remaining ticketing activation steps are incomplete; do not guess which step. `TOP_UP_REQUIRED` means flight and price search remains available, but price verification, order creation, and ticketing require an effective balance top-up. Do not describe this availability as “real-time.” If the user completes the top-up, check authorization status again. When ticketing becomes available, verify a previously selected offer with `price_status=current` even if its original `bookable` value was `false`; do not reuse a `price_status=reference` offer. Search again only when no current-price offer was selected or verification reports that the selected offer expired or became unavailable.

## Search and verify

| Operation | Exact command |
| --- | --- |
| Initial or new search | `atlas-flight search --origin {origin} --destination {destination} --depart {YYYY-MM-DD} --adults {count} --json` |
| Replay retained search | `atlas-flight search --json` |
| List offers | `atlas-flight offer list --search-id {search_id} --json` |
| Verify an offer | `atlas-flight offer verify --offer-id {offer_id} --json` |
| Confirm an increased price | `atlas-flight booking confirm-price --booking-id {booking_id} --json` |

The new-search command also accepts `--return-date {YYYY-MM-DD}`, `--children {count}`, `--infants {count}`, repeated `--airline {IATA}`, `--currency {currency}`, and `--multiple-fare-families` before `--json`. Require origin, destination, departure date, and adult count together. Replay only a search retained by the CLI.

Each new-search command accepts exactly one departure date and, optionally, one return date. Flexible-date and multi-date comparisons are Agent-side orchestration: issue one complete new-search command for every requested departure date or bounded date pair, and retain each response's date, `search_id`, and offer IDs separately. Never construct unsupported date-range arguments.

## Optional services

| Operation | Exact command |
| --- | --- |
| List baggage | `atlas-flight booking baggage list --booking-id {booking_id} --json` |
| Select baggage | `atlas-flight booking baggage select --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --baggage-id {baggage_id} --json` |
| Remove baggage | `atlas-flight booking baggage remove --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --json` |
| List seats | `atlas-flight booking seat list --booking-id {booking_id} --json` |
| Select seat | `atlas-flight booking seat select --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --seat-id {seat_id} --json` |
| Remove seat | `atlas-flight booking seat remove --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --json` |

Only select IDs returned by the latest list response and bound to the same traveler and segment. Listing or selecting one optional service does not imply that the other is available.

## Order, payment, and status

| Operation | Exact command |
| --- | --- |
| Create with one-time stdin | `atlas-flight order create --booking-id {booking_id} --passengers-stdin --json` |
| Create from an existing file | `atlas-flight order create --booking-id {booking_id} --passengers-file {absolute_path} --json` |
| Pay once | `atlas-flight order pay --confirmation-id {payment_confirmation_id} --json` |
| Query and poll ticketing | `atlas-flight order status --order-no {order_no} --json` |

`--seat-policy` belongs only to `atlas-flight order create`; never add it to `atlas-flight booking seat select` or another command. When a seat is selected, construct the order command with exactly one policy before `--json`:

- `atlas-flight order create --booking-id {booking_id} --passengers-stdin --seat-policy continue-without-seat --json`
- `atlas-flight order create --booking-id {booking_id} --passengers-stdin --seat-policy cancel-order --json`
- `atlas-flight order create --booking-id {booking_id} --passengers-stdin --seat-policy accept-similar-seat --json`

The same placement applies when `--passengers-file` is used. Ask the user which natural-language outcome they want; do not infer it.

Passenger sources are mutually exclusive. Prefer stdin. Pass a file path only when the user already supplied an absolute local path; do not open or print that file.

## Response envelope

Read `schema_version`, `status`, `code`, `message`, `retryable`, `request_id`, `data`, and `details`. A `next_action` field is not required. Treat every ID as opaque and every payment confirmation ID as single-use.
