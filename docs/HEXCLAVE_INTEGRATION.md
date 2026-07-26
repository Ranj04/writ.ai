# Hexclave integration for writ.ai

## Recommendation

Use Hexclave as the protected execution environment for writ.ai's most sensitive
executor operation. The best demo story is:

> writ.ai deterministically decides whether an action is authorized; Hexclave
> protects the secrets and sensitive payload used to perform the authorized action.

This is a good thematic fit for the sponsor prize without weakening writ.ai's
product invariant:

> The LLM may propose structure. Deterministic code decides and enforces.

Hexclave must not issue writ.ai verdicts. `intent-authority` remains the only
service that issues `ALLOW`, `REPLAN`, `BLOCK`, or `HUMAN_REVIEW`.

## Recommended use case

Protect the credentials and payload used by the executor when it creates a pull
request or invokes another external tool.

The live sequence should be:

1. The agent submits its plan to `intent-authority`.
2. The authority returns a signed, snapshot-bound grant.
3. The executor independently verifies the grant.
4. Only after verification succeeds, the executor invokes the Hexclave-backed
   operation.
5. The operation returns a receipt without exposing the protected secret.
6. The executor stores the receipt as execution evidence.

After `graph-v18` is approved, step 3 rejects the old `graph-v17` grant. Hexclave
must not be invoked. After the agent replans and receives a valid `graph-v18`
grant, the protected operation may run.

That makes the sponsor integration part of the safety proof rather than a logo or
an unrelated API call.

## Architecture

```text
agent-service
    |
    | requests authorization
    v
intent-authority -- signs snapshot-bound grant
    |
    v
executor -- verifies grant with intent-authority
    |
    | valid grant only
    v
Hexclave adapter -- protected secret/payload operation
    |
    v
execution receipt / simulated PR URL
```

The Hexclave adapter belongs in the executor service. It must not be imported by
the planner or used inside the authority engine.

## Proposed files

```text
backend/writai/integrations/
  __init__.py
  base.py
  hexclave.py
backend/tests/
  test_hexclave_executor.py
```

Use a small typed interface so the deterministic demo works without network
access:

```python
from typing import Protocol

from pydantic import BaseModel


class ProtectedExecutionRequest(BaseModel):
    authorization_id: str
    run_id: str
    task_id: str
    decision_snapshot: str
    plan_hash: str
    operation: str
    payload: dict[str, object]


class ProtectedExecutionReceipt(BaseModel):
    provider: str
    operation_id: str
    status: str
    evidence_ref: str


class ProtectedExecutor(Protocol):
    def execute(
        self,
        request: ProtectedExecutionRequest,
    ) -> ProtectedExecutionReceipt: ...
```

Provide two implementations:

- `FixtureProtectedExecutor`: deterministic, zero-config, and used by tests.
- `HexclaveProtectedExecutor`: live sponsor integration, enabled explicitly.

Do not let the adapter accept a raw grant token as proof. The executor verifies
the grant first, then creates `ProtectedExecutionRequest` from the verified
`GrantPayload`.

## Executor integration point

The current integration point is
`backend/writai/services/executor_api.py::execute`.

Keep this ordering:

```python
verification = verify_with_intent_authority(request)

if not verification.valid:
    return {
        "applied": False,
        "reason": verification.reason,
        "verification_code": verification.code.value,
    }

assert verification.payload is not None
receipt = protected_executor.execute(
    ProtectedExecutionRequest(
        authorization_id=verification.payload.authorization_id,
        run_id=verification.payload.run_id,
        task_id=verification.payload.task_id,
        decision_snapshot=verification.payload.decision_snapshot,
        plan_hash=verification.payload.plan_hash,
        operation="create_pull_request",
        payload={"repository": "writai", "branch": "agent/corrected-plan"},
    )
)
```

Never call Hexclave before `verification.valid` is true.

## Configuration

Use explicit configuration with live mode off by default:

```dotenv
WRITAI_PROTECTED_EXECUTOR=fixture
HEXCLAVE_API_KEY=
HEXCLAVE_PROJECT_ID=
HEXCLAVE_BASE_URL=
HEXCLAVE_TIMEOUT_SECONDS=10
```

The exact Hexclave SDK package, authentication headers, endpoint names, and
response fields must be copied from the sponsor's current documentation. Do not
guess them. Keep those vendor-specific details isolated inside
`HexclaveProtectedExecutor`.

Recommended runtime behavior:

- `fixture` is the default for `make demo`, `make test`, and CI.
- `hexclave` requires all live credentials at startup.
- Missing live credentials fail startup with a clear message.
- A network failure returns an execution failure; it never changes the authority
  verdict.
- Never log API keys, raw grants, or protected payload secrets.

## Evidence to retain

Return and display:

- writ.ai authorization ID
- decision snapshot
- plan hash
- Hexclave operation/receipt ID
- operation status
- timestamp
- a redacted evidence reference

Do not store the secret value or a raw signed grant in the evidence record.

## Required tests

1. A stale `graph-v17` grant does not call the Hexclave adapter.
2. A plan-hash mismatch does not call the adapter.
3. An expired grant does not call the adapter.
4. A valid corrected `graph-v18` grant calls the adapter exactly once.
5. The request passed to the adapter is built from the verified grant payload.
6. A Hexclave failure does not mutate the graph or manufacture a new verdict.
7. Logs and API responses do not expose credentials or the raw grant token.

Use a spy implementation in tests and assert its call count. No sponsor network
request should occur in the deterministic test suite.

## Demo script

1. Start with the plan authorized against `graph-v17`.
2. Explain that the protected executor is ready but can run only with a valid
   writ.ai grant.
3. Approve the upstream decision that creates `graph-v18`.
4. Attempt execution with the old grant.
5. Show `STALE_SNAPSHOT` and show that the Hexclave invocation count stayed zero.
6. Replan and obtain the corrected `graph-v18` authorization.
7. Execute again.
8. Show the Hexclave receipt beside the writ.ai authorization and provenance
   evidence.

The key line for judges:

> Hexclave protects execution, while writ.ai decides whether that execution is
> still authorized by the latest approved company intent.

## Alternative use cases

- Protect a GitHub installation token used to create the simulated PR.
- Protect a customer-support credential used for an authorized refund.
- Protect a deployment credential so stale plans cannot trigger releases.
- Run a sensitive policy evaluation over private company-decision data.

The protected PR/deployment credential is the clearest fit for the existing
writ.ai demo.

## Definition of done

The integration is demo-ready only when:

- the canonical tests still pass;
- fixture mode remains zero-config and deterministic;
- stale grants prevent any Hexclave invocation;
- the corrected grant produces a Hexclave receipt;
- the UI explains the separation between authorization and protected execution;
  and
- the live adapter uses sponsor-confirmed SDK/API details.

## Source status

James Tan's hackathon announcement confirms a **$1,000 prize for projects
utilizing Hexclave**. No Hexclave API documentation was included in the supplied
announcement, so vendor-specific API names are intentionally left unasserted in
this guide.
