# Atlas Error Handling

## Routing rule

Branch on `code`; never parse `message`. Keep internal causes out of user-facing output. Use only normalized CLI fields and stable codes from this reference.

## Authorization and access

| Code | Agent behavior |
| --- | --- |
| `AUTHORIZATION_REQUIRED` | Treat missing or expired authorization the same way; never expose an internal service code. Run `atlas-flight auth login --json`. Explain why authorization is needed and present the login response's `data.authorization_url` as a descriptive clickable link. Explain that an existing ATRIP account can sign in and authorize; a new user should choose **Create one**, finish registration, then sign in and authorize. Ask the user to return and reply after completing authorization, and stop without polling. After the user's confirmation, run one bounded poll and resume the interrupted task only after `AUTHORIZED`. |
| `AUTH_PENDING` | Explain that authorization is still incomplete and wait. Poll again only after the user says authorization is complete; never loop automatically. |
| `AUTH_EXPIRED` / `AUTH_SESSION_MISSING` | Start a new authorization flow. |
| `AUTH_SERVICE_UNAVAILABLE` | Retain the pending authorization session. Retry the identical auth read once only when `retryable=true`. |
| `SUBSCRIPTION_REQUIRED` | Branch on the optional normalized `details.ticketing_blocker`. For `TOP_UP_REQUIRED`, explain that flight and price search remains available, but the balance top-up is not yet effective, so price verification, order creation, and ticketing are unavailable. Do not call this availability “real-time.” For `TICKETING_ACTIVATION_REQUIRED` or a missing blocker, explain that the account is not yet enabled for ticketing without guessing which activation step remains. Present `details.url` as a descriptive “ATRIP 工作台” link and wait for the user to complete the indicated step. |
| `SECURE_STORE_UNAVAILABLE` | Report that secure local storage is unavailable and stop. |
| `CREDENTIAL_REJECTED` | Report the neutral CLI result and stop; recovery is already exhausted. |

## Search and verification

| Code | Agent behavior |
| --- | --- |
| `SEARCH_NO_RESULTS` | Treat as a successful empty search; present safe alternative dates when returned. |
| `SEARCH_LIMIT_REACHED` | Report the limit and do not retry automatically. |
| `OFFER_EXPIRED` / `BOOKING_EXPIRED` | Replay retained search once. If unavailable, collect complete new-search inputs. Never continue with old IDs. |
| `PRICE_CONFIRMATION_REQUIRED` | Present old and new totals and wait for new confirmation. |
| `PRICE_CONFIRMED` | Continue the same booking. |
| `PRICE_VERIFICATION_UNAVAILABLE` | Retry the identical verify command at most once when `retryable=true`. |
| `FLIGHT_UNAVAILABLE` | Report that the selected flight is unavailable and offer a new search. |
| `BOOKING_INPUT_INVALID` | Correct only the input fields identified by the CLI; otherwise stop. |

## Optional services and passengers

| Code | Agent behavior |
| --- | --- |
| `BAGGAGE_UNAVAILABLE` | Skip baggage and continue booking. |
| `SEAT_UNAVAILABLE` | Skip seats and continue booking. |
| `ANCILLARY_SELECTION_INVALID` | Relist that service; ask the user to choose a current option or continue without it. |
| `PASSENGER_INFO_REQUIRED` | Ask only for the safe field names in `details.fields`, then rebuild one one-time payload. |
| `PASSENGER_INFO_INVALID` | Correct only fields named in `details.fields`; never repeat rejected values. |
| `CONTACT_INFO_INVALID` | Ask only for the contact fields named in `details.fields`; rebuild the complete one-time payload after the user supplies them. |
| `PASSENGER_COMBINATION_UNSUPPORTED` | Report that the passenger combination cannot be booked and stop. |

## Order, payment, and ticketing

| Code | Agent behavior |
| --- | --- |
| `PAYMENT_CONFIRMATION_REQUIRED` | Present the current summary and the order link when returned, then wait for explicit approval. |
| `PAYMENT_CONFIRMATION_INVALID` | Do not pay. A fresh order response and confirmation are required. |
| `PRICE_CHANGED` | Do not create another order. Search and verify again before asking for a new decision. |
| `ORDER_CREATION_UNAVAILABLE` | Report that the order could not be created and stop. |
| `PAYMENT_METHOD_UNAVAILABLE` | Report that balance payment is unavailable; show the order link when returned. |
| `PAYMENT_DEADLINE_EXPIRED` | Report expiry and do not pay. |
| `PAYMENT_BALANCE_CHECK_REQUIRED` | Explain that payment could not be confirmed and the ATRIP balance may be insufficient. Ask the user to check the balance, show the order link only when returned, and never pay again. |
| `ORDER_CREATION_UNKNOWN` / `DUPLICATE_BOOKING_SUSPECTED` | Never create again. Show the order link if returned; otherwise report the uncertainty without inventing a URL. |
| `PAYMENT_STATUS_UNKNOWN` / `PAYMENT_PROCESSING` | Never pay again. Query `order status` using the returned `order_no`. |
| `TICKETED` | Report issued tickets using only masked CLI fields and show the order link when returned. |
| `TICKETING_PENDING` | Report that ticketing continues; show the order link when returned. Do not call it failure. |
| `ORDER_CANCELLED` | Report cancellation and show the order link when returned. |
| `ORDER_NOT_FOUND` | Report that the order could not be found and stop. |
| `ORDER_STATUS_UNAVAILABLE` | Retry the identical status query at most once when `retryable=true`; never repay. |
| `UNSUPPORTED_BOOKING_FLOW` | Report that this booking flow is unavailable and stop. |
| `BOOKING_STATE_INVALID` / `ORDER_STATE_INVALID` | Report that saved booking state cannot continue; do not reconstruct or guess it. |

## General failures

| Code | Agent behavior |
| --- | --- |
| `INVALID_ARGUMENT` | Correct only the identified argument or field. |
| `SERVICE_TEMPORARILY_UNAVAILABLE` | Repeat the identical read-only command at most once when `retryable=true`; never repeat order creation or payment. |
| `SERVICE_REQUEST_FAILED` / `SERVICE_RESPONSE_INVALID` | Report that the request could not be completed and stop. If a side effect might have occurred, follow the query-only rule. |

`retryable=true` never authorizes a different command and never authorizes a second order creation or payment attempt. Upstream payment status `411` is normalized as `PAYMENT_BALANCE_CHECK_REQUIRED`; do not expose the numeric status to the user or claim that insufficient balance is the only possible cause.
