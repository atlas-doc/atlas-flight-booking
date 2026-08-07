#!/usr/bin/env bash
set -euo pipefail

skill_dir="skills/atlas-flight-booking"
cli_ref="$skill_dir/references/cli-contract.md"
error_ref="$skill_dir/references/error-handling.md"
workflow_ref="$skill_dir/references/booking-workflow.md"
passenger_ref="$skill_dir/references/passenger-input.md"
fixture="tests/skill/fixtures/atlas-flight"
scenarios="tests/skill/scenarios.md"
export ATLAS_FIXTURE_TRACE=0

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  return 1
}

check_structure_and_contracts() {
  test -f "$skill_dir/SKILL.md"
  test -f "$skill_dir/agents/openai.yaml"
  test -f "$cli_ref"
  test -f "$error_ref"
  test -f "$workflow_ref"
  test -f "$passenger_ref"

  test "$(find "$skill_dir" -type f | wc -l | tr -d ' ')" = 6 ||
    fail "Atlas Skill must contain exactly six files"

  for command in \
    'atlas-flight --version' \
    'atlas-flight auth status --json' \
    'atlas-flight auth login --json' \
    'atlas-flight auth poll --timeout 120 --json' \
    'atlas-flight search --origin {origin} --destination {destination} --depart {YYYY-MM-DD} --adults {count} --json' \
    'atlas-flight search --json' \
    'atlas-flight offer list --search-id {search_id} --json' \
    'atlas-flight offer verify --offer-id {offer_id} --json' \
    'atlas-flight booking confirm-price --booking-id {booking_id} --json' \
    'atlas-flight booking baggage list --booking-id {booking_id} --json' \
    'atlas-flight booking baggage select --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --baggage-id {baggage_id} --json' \
    'atlas-flight booking baggage remove --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --json' \
    'atlas-flight booking seat list --booking-id {booking_id} --json' \
    'atlas-flight booking seat select --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --seat-id {seat_id} --json' \
    'atlas-flight booking seat remove --booking-id {booking_id} --traveler-id {traveler_id} --segment-id {segment_id} --json' \
    'atlas-flight order create --booking-id {booking_id} --passengers-stdin --json' \
    'atlas-flight order create --booking-id {booking_id} --passengers-file {absolute_path} --json' \
    'atlas-flight order pay --confirmation-id {payment_confirmation_id} --json' \
    'atlas-flight order status --order-no {order_no} --json' \
    'atlas-flight doctor --json'; do
    rg -Fq "$command" "$cli_ref" || fail "missing exact public command: $command"
  done

  for code in \
    AUTHORIZATION_REQUIRED AUTH_PENDING AUTH_EXPIRED AUTH_SESSION_MISSING \
    AUTH_SERVICE_UNAVAILABLE SEARCH_LIMIT_REACHED SEARCH_NO_RESULTS \
    OFFER_EXPIRED INVALID_ARGUMENT SERVICE_TEMPORARILY_UNAVAILABLE \
    SERVICE_RESPONSE_INVALID SECURE_STORE_UNAVAILABLE SUBSCRIPTION_REQUIRED \
    PRICE_CONFIRMATION_REQUIRED PRICE_CONFIRMED PRICE_CHANGED BOOKING_INPUT_INVALID \
    BAGGAGE_UNAVAILABLE \
    SEAT_UNAVAILABLE ANCILLARY_SELECTION_INVALID PASSENGER_INFO_REQUIRED \
    PASSENGER_INFO_INVALID CONTACT_INFO_INVALID ORDER_CREATION_UNKNOWN PAYMENT_CONFIRMATION_REQUIRED \
    PAYMENT_CONFIRMATION_INVALID PAYMENT_STATUS_UNKNOWN PAYMENT_METHOD_UNAVAILABLE \
    TICKETED TICKETING_PENDING ORDER_CANCELLED ORDER_STATUS_UNAVAILABLE \
    ORDER_CREATION_UNAVAILABLE UNSUPPORTED_BOOKING_FLOW BOOKING_STATE_INVALID \
    ORDER_STATE_INVALID; do
    rg -Fq "$code" "$error_ref" || fail "missing stable code route: $code"
  done

  rg -Fiq 'run `atlas-flight auth login --json`' "$error_ref" ||
    fail "authorization-required status must route through auth login"
  rg -Fq "login response's \`data.authorization_url\`" "$error_ref" ||
    fail "authorization URL provenance is not explicit"
  rg -Fiq 'retain the pending authorization session' "$error_ref" ||
    fail "auth service outage must retain pending session"
  rg -Fq 'uv tool install --python 3.12 git+https://github.com/atlas-doc/atlas-flight-booking.git' "$skill_dir/SKILL.md" ||
    fail "missing exact GitHub CLI installation command"
  rg -Fiq 'ask for permission to install it' "$skill_dir/SKILL.md" ||
    fail "CLI installation must require user permission"

  for phrase in \
    'Explain that Atlas authorization is required before the interrupted task can continue' \
    'descriptive clickable label' \
    'already has an ATRIP account' \
    'does not have an account' \
    'Create one' \
    'return to the conversation' \
    'Ask the user to reply after completing authorization' \
    'Stop the current turn without polling' \
    'After the user confirms completion'; do
    rg -Fiq "$phrase" "$skill_dir" ||
      fail "missing authorization handoff instruction: $phrase"
  done

  if rg -n -i 'show only .*authorization_url|show only the returned authorization URL' "$skill_dir"; then
    fail "bare authorization URL instruction found"
  fi

  if rg -n -i 'branch on `?message|match (the )?message|parse (the )?message unless' "$error_ref"; then
    fail "message-based routing found"
  fi

  if rg -n -i 'sandbox|pre-production|production|faresearch|test1\.atrip|atlas[[:space:]]+env|environment switch' "$skill_dir"; then
    fail "restricted environment content found"
  fi

  if rg -n -i 'curl[[:space:]]|/(search|verify|order|pay|seat|baggage)(\.do|/|\?)|/cli/(auth|pre|production|access)|x-atlas-client|cliauthtoken|access[-_ ]?key|secret[-_ ]?key|AK/SK|JWT|routingIdentifier|sessionId|productCode|upstream (status|code)|status (number|[0-9]{3,})' "$skill_dir"; then
    fail "direct API or credential guidance found"
  fi

  rg -q '^name: atlas-flight-booking$' "$skill_dir/SKILL.md"
  rg -q '^description: Use when ' "$skill_dir/SKILL.md"
  for phrase in \
    'Tell the user when the verified price decreases' \
    'Obtain new explicit confirmation when the verified price increases' \
    'Ask only for fields listed in `data.requirements.required_fields`' \
    'Prefer one-time passenger input through stdin' \
    'continue without a seat' \
    'cancel the order if the selected seat is unavailable' \
    'accept a similar seat' \
    'current payment summary' \
    '`data.order_url`' \
    'only when it is present' \
    'Never invent or derive a link' \
    'Never call `atlas-flight order pay` again'; do
    rg -Fiq "$phrase" "$skill_dir" ||
      fail "missing safe booking instruction: $phrase"
  done

  for phrase in \
    'descriptive clickable link' \
    'asks the user to reply' \
    'must not run `auth poll` in the first turn' \
    'After the user replies' \
    'Price decreased' \
    'Price increased' \
    'asks only for `passengers[0].document.number`' \
    'prefers stdin' \
    'continue without a seat' \
    'cancel the order if the selected seat is unavailable' \
    'accept a similar seat' \
    '`data.order_url`' \
    'does not invent or derive a URL' \
    'Earlier blanket authorization is insufficient' \
    'uses only `order status`'; do
    rg -Fiq "$phrase" "$scenarios" ||
      fail "missing booking evaluation scenario: $phrase"
  done

  uv run --frozen python scripts/quick_validate_skill.py "$skill_dir"
}

fixture_json() {
  local scenario="$1"
  local expected_code="$2"
  shift 2
  local output status

  set +e
  output="$(PATH="$PWD/tests/skill/fixtures:$PATH" ATLAS_TEST_SCENARIO="$scenario" atlas-flight "$@")"
  status=$?
  set -e
  printf '%s\n' "$output" | jq -e . >/dev/null || fail "fixture output is not JSON: $scenario atlas-flight $*"
  test "$(printf '%s\n' "$output" | jq -r '.code')" = "$expected_code" ||
    fail "unexpected fixture code for $scenario atlas-flight $*"
  case "$(printf '%s\n' "$output" | jq -r '.status')" in
    success|action_required) expected_status=0 ;;
    retryable_error) expected_status=20 ;;
    terminal_error) expected_status=30 ;;
    *) fail "unknown fixture status: $scenario atlas-flight $*" ;;
  esac
  test "$status" -eq "$expected_status" ||
    fail "fixture command exited $status instead of $expected_status: $scenario atlas-flight $*"
}

fixture_rejects() {
  local scenario="$1"
  shift
  local output status

  set +e
  output="$(PATH="$PWD/tests/skill/fixtures:$PATH" ATLAS_TEST_SCENARIO="$scenario" atlas-flight "$@")"
  status=$?
  set -e
  test "$status" -ne 0 || fail "malformed/unsupported fixture command succeeded: atlas-flight $*"
  printf '%s\n' "$output" | jq -e '.status == "terminal_error" and .code == "INVALID_ARGUMENT" and .retryable == false' >/dev/null ||
    fail "rejection is not the INVALID_ARGUMENT JSON contract: atlas-flight $*"
}

check_fixture() {
  for scenario in happy_path auth_required no_results search_limit search_retryable offer_expired \
    auth_service_unavailable price_decreased price_increased baggage_unavailable seat_unavailable \
    passenger_required contact_invalid order_ready order_without_link payment_unknown ticketing_pending ticketed subscription_required; do
    version="$(PATH="$PWD/tests/skill/fixtures:$PATH" ATLAS_TEST_SCENARIO="$scenario" atlas-flight --version)"
    test "$version" = 'atlas-flight 0.3.4' || fail "inconsistent plain-text version for $scenario: $version"
  done

  fixture_json auth_required AUTHORIZATION_REQUIRED auth status --json
  fixture_json auth_required AUTHORIZATION_REQUIRED auth login --json
  fixture_json auth_required AUTHORIZED auth poll --timeout 120 --json
  fixture_json auth_service_unavailable AUTH_SERVICE_UNAVAILABLE auth poll --timeout 120 --json
  fixture_json happy_path AUTHORIZED auth status --json
  fixture_json happy_path FLIGHT_SEARCHED search --origin KUL --destination SIN --depart 2026-08-04 --adults 1 --json
  fixture_json happy_path FLIGHT_SEARCHED search --json
  fixture_json happy_path OFFERS_LISTED offer list --search-id srch_example --json
  fixture_json no_results SEARCH_NO_RESULTS search --origin KUL --destination SIN --depart 2026-08-04 --adults 1 --json
  fixture_json search_limit SEARCH_LIMIT_REACHED search --origin KUL --destination SIN --depart 2026-08-04 --adults 1 --json
  fixture_json search_retryable SERVICE_TEMPORARILY_UNAVAILABLE search --origin KUL --destination SIN --depart 2026-08-04 --adults 1 --json
  fixture_json offer_expired OFFER_EXPIRED offer list --search-id srch_old --json
  fixture_json subscription_required SUBSCRIPTION_REQUIRED offer verify --offer-id off_example --json
  fixture_json price_decreased OFFER_VERIFIED offer verify --offer-id off_example --json
  fixture_json price_increased PRICE_CONFIRMATION_REQUIRED offer verify --offer-id off_example --json
  fixture_json price_increased PRICE_CONFIRMED booking confirm-price --booking-id book_increased --json
  fixture_json happy_path OFFER_VERIFIED offer verify --offer-id off_example --json
  fixture_json happy_path BAGGAGE_OPTIONS_LISTED booking baggage list --booking-id book_example --json
  fixture_json happy_path BAGGAGE_SELECTED booking baggage select --booking-id book_example --traveler-id trav_1 --segment-id seg_1 --baggage-id bag_1 --json
  fixture_json happy_path BAGGAGE_REMOVED booking baggage remove --booking-id book_example --traveler-id trav_1 --segment-id seg_1 --json
  fixture_json baggage_unavailable BAGGAGE_UNAVAILABLE booking baggage list --booking-id book_example --json
  fixture_json happy_path SEAT_OPTIONS_LISTED booking seat list --booking-id book_example --json
  fixture_json happy_path SEAT_SELECTED booking seat select --booking-id book_example --traveler-id trav_1 --segment-id seg_1 --seat-id seat_1 --json
  fixture_json happy_path SEAT_REMOVED booking seat remove --booking-id book_example --traveler-id trav_1 --segment-id seg_1 --json
  fixture_json seat_unavailable SEAT_UNAVAILABLE booking seat list --booking-id book_example --json
  fixture_json passenger_required PASSENGER_INFO_REQUIRED order create --booking-id book_example --passengers-stdin --json
  fixture_json contact_invalid CONTACT_INFO_INVALID order create --booking-id book_example --passengers-stdin --json
  fixture_json order_ready PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-stdin --json
  fixture_json order_ready PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-file /tmp/atlas-passengers.json --json
  fixture_json order_without_link PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-stdin --json
  fixture_json order_without_link TICKETING_PENDING order status --order-no ATORDEREXAMPLE --json
  fixture_json happy_path PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-stdin --seat-policy continue-without-seat --json
  fixture_json happy_path PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-stdin --seat-policy cancel-order --json
  fixture_json happy_path PAYMENT_CONFIRMATION_REQUIRED order create --booking-id book_example --passengers-stdin --seat-policy accept-similar-seat --json
  fixture_json happy_path TICKETED order pay --confirmation-id paycfm_current --json
  fixture_json payment_unknown PAYMENT_STATUS_UNKNOWN order pay --confirmation-id paycfm_current --json
  fixture_json payment_unknown TICKETING_PENDING order status --order-no ATORDEREXAMPLE --json
  fixture_json ticketing_pending TICKETING_PENDING order status --order-no ATORDEREXAMPLE --json
  fixture_json ticketed TICKETED order status --order-no ATORDEREXAMPLE --json
  fixture_json happy_path DOCTOR_OK doctor --json

  fixture_rejects happy_path search --origin KUL --destination SIN --depart 2026-08-04 --json
  fixture_rejects happy_path search --json --origin KUL
  fixture_rejects happy_path offer list --search-id srch_example
  fixture_rejects happy_path offer verify --offer-id off_example
  fixture_rejects happy_path booking confirm-price --booking-id book_example
  fixture_rejects happy_path order create --verified-offer-id vof_example --passengers-file /tmp/passengers.json --json
  fixture_rejects happy_path order create --booking-id book_example --passengers-stdin --passengers-file /tmp/passengers.json --json
  fixture_rejects happy_path payment prepare --order-id ord_example --json
  fixture_rejects happy_path payment execute --confirmation-id cnf_example --json
  fixture_rejects happy_path order status --order-id ord_example --json
  fixture_rejects happy_path order status --order-no ATORDEREXAMPLE
  fixture_rejects happy_path doctor --json extra
  fixture_rejects happy_path unsupported command --json

  if rg -n 'curl|wget|http://test1|https://test1' "$fixture"; then
    fail "fixture may access a network or test host"
  fi
}

case "${ATLAS_VALIDATE_SECTION:-all}" in
  contracts) check_structure_and_contracts ;;
  fixture) check_fixture ;;
  all)
    check_structure_and_contracts
    check_fixture
    ;;
  *) fail "unknown ATLAS_VALIDATE_SECTION" ;;
esac
