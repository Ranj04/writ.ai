from __future__ import annotations

from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from writai.config import require_production_secrets, settings
from writai.domain import AgentPlan, GrantVerificationRequest, GrantVerificationResult
from writai.integrations.callwright import (
    FIXTURE_PHONE_NUMBER,
    CallwrightClient,
    CallwrightError,
    FileCallwrightAttemptStore,
    FixtureCallwrightClient,
    LiveCallwrightClient,
    build_call_request,
    select_callwright_action,
)
from writai.services.support import (
    CORRELATION_ID_HEADER,
    DEMO_FRONTEND_ORIGINS,
    ApiError,
    correlated_payload,
    install_api_support,
    post_model,
    require_internal_service,
)


class AuthorityContextKind(StrEnum):
    SCENARIO = "scenario"
    WORKSPACE = "workspace"


class ExecuteRequest(BaseModel):
    token: str
    run_id: str
    task_id: str
    plan: AgentPlan
    context_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )
    context_kind: AuthorityContextKind = AuthorityContextKind.SCENARIO


app = FastAPI(title="writ.ai Mock Executor", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEMO_FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[CORRELATION_ID_HEADER],
)
install_api_support(app)
require_production_secrets()


def _create_callwright_client() -> CallwrightClient:
    if (
        settings.execution_provider == "callwright"
        and settings.callwright_live_calls_enabled
        and settings.callwright_api_key
    ):
        return LiveCallwrightClient(
            api_key=settings.callwright_api_key,
            base_url=settings.callwright_base_url,
            timeout_seconds=settings.callwright_timeout_seconds,
            attempt_store=FileCallwrightAttemptStore(
                settings.callwright_attempt_store
            ),
        )
    return FixtureCallwrightClient()


callwright_client: CallwrightClient = _create_callwright_client()


@app.get("/health")
def health() -> dict[str, str]:
    return correlated_payload({"status": "ok"})


@app.post("/execute")
def execute(execution: ExecuteRequest, request: Request) -> dict[str, object]:
    # A verified grant can still be replayed from any log or tab that saw the token;
    # only the agent service and the scenario transport may drive execution.
    require_internal_service(request, secret=settings.grant_secret)
    payload = GrantVerificationRequest(
        token=execution.token,
        run_id=execution.run_id,
        task_id=execution.task_id,
        plan=execution.plan,
    )
    if execution.context_id and execution.context_kind is AuthorityContextKind.WORKSPACE:
        verification_url = (
            f"{settings.authority_url}/live-workspaces/authority/contexts/"
            f"{execution.context_id}/grants/verify"
        )
    elif execution.context_id:
        verification_url = (
            f"{settings.authority_url}/scenario-lab/authority/contexts/"
            f"{execution.context_id}/grants/verify"
        )
    else:
        verification_url = f"{settings.authority_url}/grants/verify"
    verification = post_model(
        url=verification_url,
        payload=payload,
        response_model=GrantVerificationResult,
        upstream_name="Intent authority",
        upstream_code="AUTHORITY",
        timeout_seconds=settings.service_timeout_seconds,
        internal_secret=settings.grant_secret,
    )

    if not verification.valid:
        return correlated_payload(
            {
                "applied": False,
                "reason": verification.reason,
                "verification_code": verification.code.value,
            }
        )

    if verification.payload is None:
        raise ApiError(
            status_code=502,
            code="AUTHORITY_INVALID_RESPONSE",
            message="Intent authority returned an invalid verification response.",
        )

    try:
        call_action = select_callwright_action(execution.plan)
    except CallwrightError as exc:
        return correlated_payload(
            {
                "applied": False,
                "reason": f"Grant verified, but Callwright execution was not submitted: {exc}",
                "verification_code": verification.code.value,
            }
        )

    if call_action is not None:
        if settings.execution_provider == "callwright":
            if not settings.callwright_live_calls_enabled:
                return correlated_payload(
                    {
                        "applied": False,
                        "reason": (
                            "Grant verified, but live Callwright calls are disabled "
                            "by configuration."
                        ),
                        "verification_code": verification.code.value,
                        "execution_mode": "live",
                    }
                )
            live_phone_number = settings.callwright_demo_phone_number
            if not settings.callwright_api_key or not live_phone_number:
                return correlated_payload(
                    {
                        "applied": False,
                        "reason": (
                            "Grant verified, but live Callwright credentials and the demo "
                            "target are incomplete."
                        ),
                        "verification_code": verification.code.value,
                        "execution_mode": "live",
                    }
                )
            execution_mode = "live"
            configured_phone_number = live_phone_number
        elif settings.execution_provider == "fixture":
            execution_mode = "simulated"
            configured_phone_number = (
                settings.callwright_demo_phone_number or FIXTURE_PHONE_NUMBER
            )
        else:
            return correlated_payload(
                {
                    "applied": False,
                    "reason": "Grant verified, but the execution provider is not supported.",
                    "verification_code": verification.code.value,
                }
            )

        allowed_targets = {"demo-venue": configured_phone_number}
        try:
            call_request = build_call_request(
                action=call_action,
                verified_grant=verification.payload,
                allowed_targets=allowed_targets,
            )
            receipt = callwright_client.create_call(call_request)
        except CallwrightError as exc:
            return correlated_payload(
                {
                    "applied": False,
                    "reason": (
                        f"Grant verified, but Callwright execution was not submitted: {exc}"
                    ),
                    "verification_code": verification.code.value,
                    "execution_mode": execution_mode,
                }
            )
        return correlated_payload(
            {
                "applied": True,
                "reason": (
                    "Grant verified; Callwright call submitted."
                    if execution_mode == "live"
                    else "Grant verified; simulated Callwright call submitted."
                ),
                "verification_code": verification.code.value,
                "execution_mode": execution_mode,
                "call_receipt": receipt,
            }
        )

    return correlated_payload(
        {
            "applied": True,
            "reason": "Grant verified; mock pull request created.",
            "verification_code": verification.code.value,
            "pull_request_url": "https://example.invalid/writai/pull/42",
        }
    )
