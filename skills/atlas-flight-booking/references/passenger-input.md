# Passenger Input

## Collection rule

Use the verification response as the source of truth. Ask only for fields listed in `data.requirements.required_fields`. Carry each `traveler_id` and `passenger_type` from `data.travelers`; never ask the user to invent IDs. Ask only for missing values, then build the payload for the user.

The order envelope also requires a contact name. Ask for it only if it is not already available from the passenger details. Contact email and mobile are optional unless the user supplies them.

## One-time delivery

Prefer one-time passenger input through stdin. Start `atlas-flight order create --booking-id {booking_id} --passengers-stdin --json`, send exactly one JSON object to that process's standard input, then close the input. Use the Agent runtime's stdin channel; do not interpolate personal values into the shell command.

Do not echo the payload back to the user, save it, place it in shell history, or log it.

If the user already gives an absolute local file path, pass it with `--passengers-file`. Do not read, print, copy, or modify the file. Never use stdin and file input together.

## Payload shape

Construct one JSON object with this shape; omit optional fields that are neither required nor supplied:

```json
{
  "passengers": [
    {
      "traveler_id": "{traveler_id}",
      "name": "{FAMILY/GIVEN}",
      "passenger_type": "{adult|child|infant}",
      "gender": "{M|F}",
      "birthday": "{YYYY-MM-DD}",
      "nationality": "{ISO-2}",
      "document": {
        "type": "{PP|GA|TW|TB|HY}",
        "number": "{document_number}",
        "issuing_country": "{ISO-2}",
        "expires": "{YYYY-MM-DD}"
      }
    }
  ],
  "contact": {
    "name": "{FAMILY/GIVEN}",
    "email": "{email}",
    "mobile": "{00_country_code-local_number}"
  }
}
```

Names use uppercase `FAMILY/GIVEN`. Preserve document numbers exactly. Mobile numbers use `00` plus the country calling code, a hyphen, and the local number, for example the shape `00{country_code}-{local_number}`. If the country calling code cannot be determined reliably, ask the user instead of guessing.

## Safe correction

On `PASSENGER_INFO_REQUIRED`, `PASSENGER_INFO_INVALID`, or `CONTACT_INFO_INVALID`, read only `details.fields`, ask for those fields, rebuild the full one-time payload, and submit once. Never repeat rejected personal data in the explanation. Contact email remains optional unless the CLI specifically returns `contact.email` in `details.fields`.
