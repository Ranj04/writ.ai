# VOYAGR Callwright integration for Dragback

## Recommendation

Use Callwright as Dragback's real-world consequential executor.

The strongest demo is an agent attempting to place a phone call to schedule or
change an appointment. The original call plan is authorized against
`graph-v17`. A newly approved upstream decision creates `graph-v18` and changes
the allowed call instructions without mentioning the downstream task directly.
The executor rejects the stale grant, the agent replans, and only the corrected
call is submitted.

This is more compelling than attaching Callwright to the existing mock PR
because the cost of stale intent is immediately understandable: an autonomous
agent was about to say or do the wrong thing on a real phone call.

## Recommended use case

Use a safe reservation or appointment scenario:

- `TASK-101`, an unrelated sibling, remains valid.
- `TASK-102`, "call the venue and book the original time," is invalidated.
- The approved upstream decision changes the time, party size, budget, or
  permitted commitment.
- The old call authorization is rejected.
- The corrected plan receives a `graph-v18` grant.
- Callwright places the corrected call or runs in its sponsor-provided test mode.

For a live hackathon demo, prefer a controlled number owned by a teammate or a
venue/test target explicitly approved for calls. Never surprise a real business
with repeated test calls.

## Architecture

```text
approved decision -> provenance graph -> selective invalidation
                                             |
                                             v
agent-service -> intent-authority -> signed grant
                                      |
                                      v
executor -> verify grant -> Callwright adapter -> call job
                                      |
                                      v
                          call status / result evidence
```

Callwright is an execution tool, not an authority source. It must never issue or
override `ALLOW`, `REPLAN`, `BLOCK`, or `HUMAN_REVIEW`.

## Implementation files

```text
backend/dragback/integrations/
  __init__.py
  callwright.py
backend/tests/
  test_callwright_executor.py
```

The sponsor-specific HTTP details live behind one typed adapter. The plan keeps
structured intent; the adapter converts that intent into Callwright's single
`brief` field only after the grant is verified:

```python
from typing import Protocol

from pydantic import BaseModel, Field


class CallRequest(BaseModel):
    authorization_id: str
    run_id: str
    task_id: str
    decision_snapshot: str
    plan_hash: str
    target_phone: str
    brief: str
    language: str = "en"


class CallReceipt(BaseModel):
    provider: str = "voyagr-callwright"
    call_id: str
    status: str
    evidence_ref: str


class CallwrightClient(Protocol):
    def create_call(self, request: CallRequest) -> CallReceipt: ...
```

Provide:

- `FixtureCallwrightClient`: deterministic receipt for tests and offline demos.
- `LiveCallwrightClient`: exact sponsor API integration, enabled explicitly.
- `FileCallwrightAttemptStore`: single-process durable, at-most-once submission
  protection keyed by Dragback authorization ID.

## Representing the call in an agent plan

Put the exact action-driving fields in `PlanAction.attributes` so they are
covered by Dragback's stable plan hash:

```json
{
  "id": "ACTION-CALL-001",
  "description": "Call the venue to request the approved reservation",
  "scopes": ["reservation.time", "reservation.party_size"],
  "attributes": {
    "provider": "voyagr-callwright",
    "phone_number_ref": "demo-venue",
    "requested_time": "2026-07-26T19:00:00-07:00",
    "party_size": 4,
    "max_deposit_usd": 0,
    "objective": "Request a reservation; do not make a paid commitment",
    "instructions": [
      "Confirm the time before booking",
      "Do not provide payment details"
    ],
    "allowed_commitments": [
      "Book only if no deposit is required"
    ],
    "language": "en"
  }
}
```

Do not place the real phone number or API key in the graph. Resolve
`phone_number_ref` inside the executor from configuration after grant
verification.

The changed decision should affect one of the action scopes, for example
`reservation.time`, while a sibling task uses a different scope. This preserves
Dragback's selective-invalidation proof.

## Executor integration

Extend the executor only after its existing authority verification succeeds:

```python
verification = verify_with_intent_authority(request)

if not verification.valid:
    return {
        "applied": False,
        "reason": verification.reason,
        "verification_code": verification.code.value,
    }

assert verification.payload is not None
call_action = select_callwright_action(request.plan)
call_request = build_call_request(
    action=call_action,
    verified_grant=verification.payload,
)
receipt = callwright_client.create_call(call_request)
```

`build_call_request` must use only:

- fields in the hashed plan;
- the verified `GrantPayload`; and
- executor-owned configuration such as the phone-number lookup.

It must not let the LLM rewrite instructions between authorization and
execution. Any change to objective, time, party size, budget, or allowed
commitments requires a new plan hash and authorization.

## Configuration

```dotenv
DRAGBACK_EXECUTION_PROVIDER=fixture
CALLWRIGHT_API_KEY=
CALLWRIGHT_BASE_URL=https://api.voygr.tech
CALLWRIGHT_DEMO_PHONE_NUMBER=
CALLWRIGHT_TIMEOUT_SECONDS=15
CALLWRIGHT_ATTEMPT_STORE=.dragback/callwright-attempts.json
CALLWRIGHT_POLL_INTERVAL_SECONDS=2
CALLWRIGHT_MAX_POLL_SECONDS=30
CALLWRIGHT_LIVE_CALLS_ENABLED=false
```

The current sponsor materials confirm this create-call contract:

```http
POST https://api.voygr.tech/calls
X-API-Key: <team key>
Content-Type: application/json

{
  "target_phone": "+15551234567",
  "brief": "Objective: ...",
  "language": "en"
}
```

The accepted response contains `call_id` and `status`. Status is retrieved with
the same API-key header:

```http
GET https://api.voygr.tech/calls/<call_id>
X-API-Key: <team key>
```

The status response can contain `status`, `outcome_type`, `summary`, and
`transcript_full`. Dragback keeps the status and summary as execution evidence
but deliberately discards the full transcript.

Authoritative sponsor materials:

- [Callwright quickstart](https://docs.google.com/document/d/1nqdCN7io1UZrrWiXsW5Sq2GJuE0ZmHOkx2nsGQJ4CbI/edit)
- [VOYAGR API Access](https://docs.google.com/spreadsheets/d/1Ls1XZ4fljxqbWDb6ogD-xtO7HULtuaLOdpaJ9O88Ve8/edit?gid=1507417457#gid=1507417457)

`LiveCallwrightClient` sends only `target_phone`, `brief`, and `language`.
Dragback authorization metadata and secrets never enter the request body. The
base URL is pinned to the exact HTTPS origin, and redirects are rejected.

## Sync, polling, and webhook behavior

Use the sponsor's polling flow:

1. Submit one call after grant verification.
2. Store the returned `call_id`.
3. Return `submitted` immediately.
4. Poll `GET /calls/<call_id>` after completion.
5. Attach the sanitized status and summary as execution evidence.

The current adapter does not process webhooks. Call results cannot become an
authority decision or mutate the provenance graph.

## Safety constraints

- Live calling is disabled by default.
- Permit only configured phone-number references.
- Require an explicit environment flag for live mode.
- Allow at most one call per authorization ID.
- Persist a reservation before the outbound request and never automatically
  replay a timeout or ambiguous response.
- Set hard boundaries for payments, quotes, reservations, or commitments.
- Never let call results mutate approved company intent automatically.
- Treat transcripts as untrusted evidence that may inform a proposal or human
  review, not as an approved decision.
- Redact API keys, grant tokens, and phone numbers from logs.

The service should refuse live calls unless both the provider is `callwright`
and this flag is `true`. Callwright does not currently document a server-side
idempotency key, so Dragback does not invent an unsupported header. An ambiguous
submission is recorded as `UNKNOWN` and requires human reconciliation instead
of a retry that might ring the target twice.

## Required tests

1. A stale `graph-v17` grant results in zero Callwright requests.
2. A mismatched plan hash results in zero requests.
3. An expired grant results in zero requests.
4. A valid `graph-v18` corrected grant submits exactly one call.
5. Retrying the same authorization does not place a duplicate call.
6. Changing any call instruction changes the plan hash.
7. An out-of-scope sibling remains valid.
8. Callwright errors do not change the authority verdict or graph.
9. Unapproved phone-number references are rejected.
10. Secrets and raw phone numbers are absent from logs and API responses.

All canonical tests must continue to use `FixtureCallwrightClient`.

## Built-in demo fixture

The Live Workspace starter now loads this deterministic VOYAGR fixture:

### `graph-v17`

- Approved decision: customer events may be booked at 7:00 PM.
- `TASK-101`: prepare an event summary; scope `event.copy`.
- `TASK-102`: call the venue for a 7:00 PM booking; scope
  `reservation.time`.
- Plan is authorized and tests pass.

### Approved change creating `graph-v18`

- New decision: bookings must be at 8:30 PM.
- The decision mentions the scheduling policy, not `TASK-102`.
- Provenance traversal reaches `TASK-102` through the specification and ticket.
- `TASK-101` remains `VALID`.
- `TASK-102` becomes `INVALIDATED`.
- The active plan becomes `NEEDS_REVIEW`.

### Execution result

- Old call request: rejected with `STALE_SNAPSHOT`; no phone call is placed.
- Loop state: `REPLAN`.
- Corrected call request: 8:30 PM.
- New authorization: valid against `graph-v18`.
- Callwright: receives one corrected request.

## Demo script

1. Show the call plan and `graph-v17` authorization.
2. Apply the approved upstream decision.
3. Show the multi-hop path and selective invalidation.
4. Press "Place call" using the old grant.
5. Show `STALE_SNAPSHOT` and "Callwright not invoked."
6. Replan to the corrected instructions.
7. Obtain the `graph-v18` grant.
8. Submit the call.
9. Show its call ID/status as execution evidence.

The key line for judges:

> The phone API was ready, the tests passed, and the ticket never changed—but
> Dragback stopped the call because its authorization no longer matched approved
> company intent.

## Additional use cases

- Schedule a medical or service appointment without exceeding approved terms.
- Request quotes while preventing an agent from accepting one without authority.
- Cancel or reschedule reservations after an upstream policy change.
- Confirm inventory or operating hours without permitting purchases.
- Place outbound customer notifications whose script is bound to an approved
  snapshot.

The reservation-time change is safest and easiest to understand in a short demo.

## Definition of done

- The live call is downstream of independent grant verification.
- The complete call intent is included in the hashed plan.
- Old grants cannot submit calls.
- Corrected grants submit at most one call.
- The selective sibling behavior remains visible.
- Fixture mode is deterministic and zero-config.
- Live mode uses only sponsor-confirmed API details and approved test numbers.
- The UI explicitly labels phone/API behavior as live or simulated.

## Source status

James Tan's announcement states that VOYAGR provides early access to Callwright,
an API for making calls for reservations, appointments, and quote requests, and
offers a **$1,500 Apple Gift Card** for the most impressive project using the
dataset. The linked Google Doc and access sheet are the authoritative sources for
vendor-specific API details.

## Current implementation status

The executor now has the typed Callwright boundary, deterministic fixture client,
configured target allowlist, authorization-ID idempotency, redacted receipts, and
tests proving that rejected grants cause zero call submissions. Fixture mode is
the zero-config default. The Live Workspace starts with the VOYAGR reservation
fixture, labels the stale attempt as “Callwright not invoked,” requires a final
“Place authorized call” action, and displays a redacted simulated/live receipt.

The live HTTP adapter now implements the sponsor-confirmed `POST /calls` and
`GET /calls/<call_id>` contracts. It includes exact-origin pinning, E.164 target
validation, redirect rejection, sanitized vendor errors, typed status parsing,
and a durable attempt reservation that prevents accidental replay after an
ambiguous response.

Live mode remains intentionally gated. Claim one team key through the access
sheet, store it only in the ignored local `.env`, configure a controlled E.164
demo number, and leave `CALLWRIGHT_LIVE_CALLS_ENABLED=false` until the team is
ready for an explicit real-call test. No API key or live phone call is required
for the deterministic Dragback proof.
