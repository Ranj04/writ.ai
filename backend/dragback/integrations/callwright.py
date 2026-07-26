from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dragback.domain import AgentPlan, GrantPayload, PlanAction
from dragback.hashing import stable_hash

CALLWRIGHT_PROVIDER = "voyagr-callwright"
CALLWRIGHT_DEFAULT_BASE_URL = "https://api.voygr.tech"
FIXTURE_PHONE_NUMBER = "fixture://demo-venue"
_E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
_CALL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"


class CallwrightError(RuntimeError):
    """A sanitized execution failure safe to expose through the executor API."""


class CallwrightPlanError(CallwrightError):
    """The verified plan does not contain a safe, executable Callwright action."""


class CallwrightConfigurationError(CallwrightError):
    """The executor is not safely configured for a live Callwright request."""


class CallActionAttributes(BaseModel):
    """Structured intent that is hashed before it becomes a vendor brief."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["voyagr-callwright"]
    phone_number_ref: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=2000)
    requested_time: str | None = Field(default=None, max_length=128)
    party_size: int | None = Field(default=None, ge=1, le=100)
    max_deposit_usd: float | None = Field(default=None, ge=0)
    instructions: list[str] = Field(default_factory=list, max_length=32)
    allowed_commitments: list[str] = Field(default_factory=list, max_length=32)
    language: str = Field(
        default="en",
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$",
    )


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
    provider: str = CALLWRIGHT_PROVIDER
    call_id: str = Field(pattern=_CALL_ID_PATTERN)
    status: str
    evidence_ref: str


class CallStatus(BaseModel):
    provider: str = CALLWRIGHT_PROVIDER
    call_id: str = Field(pattern=_CALL_ID_PATTERN)
    status: str
    outcome_type: str | None = None
    summary: str | None = None
    evidence_ref: str


class CallwrightClient(Protocol):
    def create_call(self, request: CallRequest) -> CallReceipt: ...

    def get_call(self, call_id: str) -> CallStatus: ...


class CallwrightCreateResponse(BaseModel):
    call_id: str = Field(pattern=_CALL_ID_PATTERN)
    status: str = Field(min_length=1, max_length=128)


class CallwrightGetResponse(BaseModel):
    status: str = Field(min_length=1, max_length=128)
    outcome_type: str | None = Field(default=None, max_length=256)
    summary: str | None = Field(default=None, max_length=8000)


class CallAttemptState(StrEnum):
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class CallAttemptRecord(BaseModel):
    authorization_id: str
    request_hash: str
    state: CallAttemptState
    receipt: CallReceipt | None = None


class CallwrightAttemptStore(Protocol):
    def reserve(
        self,
        *,
        authorization_id: str,
        request_hash: str,
    ) -> tuple[bool, CallAttemptRecord]: ...

    def transition(
        self,
        *,
        authorization_id: str,
        state: CallAttemptState,
        receipt: CallReceipt | None = None,
    ) -> CallAttemptRecord: ...


class InMemoryCallwrightAttemptStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, CallAttemptRecord] = {}

    def reserve(
        self,
        *,
        authorization_id: str,
        request_hash: str,
    ) -> tuple[bool, CallAttemptRecord]:
        with self._lock:
            existing = self._records.get(authorization_id)
            if existing is not None:
                return False, existing.model_copy(deep=True)
            record = CallAttemptRecord(
                authorization_id=authorization_id,
                request_hash=request_hash,
                state=CallAttemptState.RESERVED,
            )
            self._records[authorization_id] = record
            return True, record.model_copy(deep=True)

    def transition(
        self,
        *,
        authorization_id: str,
        state: CallAttemptState,
        receipt: CallReceipt | None = None,
    ) -> CallAttemptRecord:
        with self._lock:
            existing = self._records.get(authorization_id)
            if existing is None:
                raise CallwrightError(
                    "The Callwright attempt reservation could not be found."
                )
            updated = existing.model_copy(
                update={"state": state, "receipt": receipt},
                deep=True,
            )
            self._records[authorization_id] = updated
            return updated.model_copy(deep=True)


class FileCallwrightAttemptStore:
    """Single-process durable store for at-most-once hackathon call submission."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = Lock()

    def reserve(
        self,
        *,
        authorization_id: str,
        request_hash: str,
    ) -> tuple[bool, CallAttemptRecord]:
        with self._lock:
            records = self._read()
            existing = records.get(authorization_id)
            if existing is not None:
                return False, existing.model_copy(deep=True)
            record = CallAttemptRecord(
                authorization_id=authorization_id,
                request_hash=request_hash,
                state=CallAttemptState.RESERVED,
            )
            records[authorization_id] = record
            self._write(records)
            return True, record.model_copy(deep=True)

    def transition(
        self,
        *,
        authorization_id: str,
        state: CallAttemptState,
        receipt: CallReceipt | None = None,
    ) -> CallAttemptRecord:
        with self._lock:
            records = self._read()
            existing = records.get(authorization_id)
            if existing is None:
                raise CallwrightError(
                    "The Callwright attempt reservation could not be found."
                )
            updated = existing.model_copy(
                update={"state": state, "receipt": receipt},
                deep=True,
            )
            records[authorization_id] = updated
            self._write(records)
            return updated.model_copy(deep=True)

    def _read(self) -> dict[str, CallAttemptRecord]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Callwright attempt store must contain an object")
            return {
                key: CallAttemptRecord.model_validate(value)
                for key, value in raw.items()
            }
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise CallwrightConfigurationError(
                "The Callwright attempt store is unavailable or invalid."
            ) from exc

    def _write(self, records: Mapping[str, CallAttemptRecord]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp")
            serialized = {
                key: value.model_dump(mode="json")
                for key, value in sorted(records.items())
            }
            temporary.write_text(
                json.dumps(serialized, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except OSError as exc:
            raise CallwrightConfigurationError(
                "The Callwright attempt store could not be persisted."
            ) from exc


class FixtureCallwrightClient:
    """Deterministic, network-free Callwright substitute for tests and demos."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: dict[str, CallRequest] = {}
        self._receipts: dict[str, CallReceipt] = {}

    @property
    def submission_count(self) -> int:
        with self._lock:
            return len(self._receipts)

    def request_for(self, authorization_id: str) -> CallRequest | None:
        with self._lock:
            request = self._requests.get(authorization_id)
            return request.model_copy(deep=True) if request is not None else None

    def create_call(self, request: CallRequest) -> CallReceipt:
        with self._lock:
            previous_request = self._requests.get(request.authorization_id)
            if previous_request is not None:
                if previous_request != request:
                    raise CallwrightError(
                        "The authorization ID was already used for a different call request."
                    )
                return self._receipts[request.authorization_id].model_copy(deep=True)

            digest = hashlib.sha256(request.authorization_id.encode("utf-8")).hexdigest()
            call_id = f"CALL-FIXTURE-{digest[:12].upper()}"
            receipt = CallReceipt(
                provider=f"{CALLWRIGHT_PROVIDER}-fixture",
                call_id=call_id,
                status="queued",
                evidence_ref=f"callwright://fixture/{call_id}",
            )
            self._requests[request.authorization_id] = request.model_copy(deep=True)
            self._receipts[request.authorization_id] = receipt
            return receipt.model_copy(deep=True)

    def get_call(self, call_id: str) -> CallStatus:
        with self._lock:
            receipt = next(
                (
                    candidate
                    for candidate in self._receipts.values()
                    if candidate.call_id == call_id
                ),
                None,
            )
            if receipt is None:
                raise CallwrightError("The simulated Callwright call was not found.")
            return CallStatus(
                provider=receipt.provider,
                call_id=receipt.call_id,
                status=receipt.status,
                outcome_type="fixture",
                summary="Simulated Callwright submission accepted.",
                evidence_ref=receipt.evidence_ref,
            )


class LiveCallwrightClient:
    """Minimal adapter for VOYAGR's confirmed create-call and call-status APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = CALLWRIGHT_DEFAULT_BASE_URL,
        timeout_seconds: float = 15,
        http_client: httpx.Client | None = None,
        attempt_store: CallwrightAttemptStore | None = None,
    ) -> None:
        if not api_key.strip():
            raise CallwrightConfigurationError("The Callwright API key is missing.")
        _validate_base_url(base_url)
        if timeout_seconds <= 0:
            raise CallwrightConfigurationError(
                "The Callwright timeout must be greater than zero."
            )

        self._api_key = api_key
        self._base_url = CALLWRIGHT_DEFAULT_BASE_URL
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.Client(follow_redirects=False)
        self._attempt_store = attempt_store or InMemoryCallwrightAttemptStore()

    def create_call(self, request: CallRequest) -> CallReceipt:
        if not _E164_PATTERN.fullmatch(request.target_phone):
            raise CallwrightPlanError(
                "The configured live Callwright target must use E.164 format."
            )

        request_hash = stable_hash(request)
        created, attempt = self._attempt_store.reserve(
            authorization_id=request.authorization_id,
            request_hash=request_hash,
        )
        if not created:
            if attempt.request_hash != request_hash:
                raise CallwrightError(
                    "The authorization ID was already used for a different call request."
                )
            if attempt.state is CallAttemptState.SUBMITTED and attempt.receipt is not None:
                return attempt.receipt.model_copy(deep=True)
            raise CallwrightError(
                "A previous Callwright submission has no confirmed reusable receipt; "
                "refusing to place another call."
            )

        try:
            response = self._http_client.post(
                f"{self._base_url}/calls",
                headers={
                    "X-API-Key": self._api_key,
                    "Accept": "application/json",
                },
                json={
                    "target_phone": request.target_phone,
                    "brief": request.brief,
                    "language": request.language,
                },
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            self._mark_attempt(request.authorization_id, CallAttemptState.UNKNOWN)
            raise CallwrightError(
                "Callwright timed out with an unknown submission outcome; "
                "automatic replay is disabled."
            ) from exc
        except httpx.RequestError as exc:
            self._mark_attempt(request.authorization_id, CallAttemptState.UNKNOWN)
            raise CallwrightError(
                "Callwright became unavailable with an unknown submission outcome; "
                "automatic replay is disabled."
            ) from exc

        if 300 <= response.status_code < 400:
            self._mark_attempt(request.authorization_id, CallAttemptState.REJECTED)
            raise CallwrightError("Callwright returned an unexpected redirect.")
        if response.status_code == 402:
            self._mark_attempt(request.authorization_id, CallAttemptState.REJECTED)
            raise CallwrightError(
                "Callwright has insufficient credits; no call was placed."
            )
        if response.status_code in {401, 403}:
            self._mark_attempt(request.authorization_id, CallAttemptState.REJECTED)
            raise CallwrightError("Callwright rejected the configured API key.")
        if response.status_code == 429:
            self._mark_attempt(request.authorization_id, CallAttemptState.REJECTED)
            raise CallwrightError("Callwright rate-limited the call request.")
        if response.is_error:
            self._mark_attempt(request.authorization_id, CallAttemptState.REJECTED)
            raise CallwrightError(
                f"Callwright rejected the call request with HTTP {response.status_code}."
            )

        try:
            created_call = CallwrightCreateResponse.model_validate(response.json())
        except (TypeError, ValueError, ValidationError) as exc:
            self._mark_attempt(request.authorization_id, CallAttemptState.UNKNOWN)
            raise CallwrightError(
                "Callwright returned an invalid create-call response; "
                "automatic replay is disabled."
            ) from exc

        encoded_call_id = quote(created_call.call_id, safe="")
        receipt = CallReceipt(
            call_id=created_call.call_id,
            status=created_call.status,
            evidence_ref=f"callwright://calls/{encoded_call_id}",
        )
        self._attempt_store.transition(
            authorization_id=request.authorization_id,
            state=CallAttemptState.SUBMITTED,
            receipt=receipt,
        )
        return receipt.model_copy(deep=True)

    def get_call(self, call_id: str) -> CallStatus:
        if re.fullmatch(_CALL_ID_PATTERN, call_id) is None:
            raise CallwrightPlanError("The Callwright call ID is invalid.")
        encoded_call_id = quote(call_id, safe="")
        try:
            response = self._http_client.get(
                f"{self._base_url}/calls/{encoded_call_id}",
                headers={
                    "X-API-Key": self._api_key,
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise CallwrightError("Callwright status lookup timed out.") from exc
        except httpx.RequestError as exc:
            raise CallwrightError("Callwright status lookup is unavailable.") from exc

        if 300 <= response.status_code < 400:
            raise CallwrightError(
                "Callwright status lookup returned an unexpected redirect."
            )
        if response.status_code in {401, 403}:
            raise CallwrightError("Callwright rejected the configured API key.")
        if response.is_error:
            raise CallwrightError(
                f"Callwright status lookup failed with HTTP {response.status_code}."
            )
        try:
            result = CallwrightGetResponse.model_validate(response.json())
        except (TypeError, ValueError, ValidationError) as exc:
            raise CallwrightError(
                "Callwright returned an invalid call-status response."
            ) from exc
        return CallStatus(
            call_id=call_id,
            status=result.status,
            outcome_type=result.outcome_type,
            summary=result.summary,
            evidence_ref=f"callwright://calls/{encoded_call_id}",
        )

    def _mark_attempt(
        self,
        authorization_id: str,
        state: CallAttemptState,
    ) -> None:
        self._attempt_store.transition(
            authorization_id=authorization_id,
            state=state,
        )


def select_callwright_action(plan: AgentPlan) -> PlanAction | None:
    matches = [
        action
        for action in plan.actions
        if action.attributes.get("provider") == CALLWRIGHT_PROVIDER
    ]
    if len(matches) > 1:
        raise CallwrightPlanError(
            "A plan may contain at most one Callwright action per execution."
        )
    return matches[0] if matches else None


def build_call_request(
    *,
    action: PlanAction,
    verified_grant: GrantPayload,
    allowed_targets: Mapping[str, str],
) -> CallRequest:
    """Build a vendor request from hashed structure, a verified grant, and executor config."""

    try:
        attributes = CallActionAttributes.model_validate(action.attributes)
    except ValidationError as exc:
        raise CallwrightPlanError(
            "The authorized Callwright action is missing or has invalid instructions."
        ) from exc

    target_phone = allowed_targets.get(attributes.phone_number_ref)
    if not target_phone:
        raise CallwrightPlanError("The authorized Callwright target is not configured.")

    return CallRequest(
        authorization_id=verified_grant.authorization_id,
        run_id=verified_grant.run_id,
        task_id=verified_grant.task_id,
        decision_snapshot=verified_grant.decision_snapshot,
        plan_hash=verified_grant.plan_hash,
        target_phone=target_phone,
        brief=_render_brief(attributes),
        language=attributes.language,
    )


def _render_brief(attributes: CallActionAttributes) -> str:
    lines = [attributes.objective]
    if attributes.requested_time is not None:
        lines.append(f"Requested time: {attributes.requested_time}.")
    if attributes.party_size is not None:
        lines.append(f"Party size: {attributes.party_size}.")
    if attributes.max_deposit_usd is not None:
        if attributes.max_deposit_usd == 0:
            lines.append("Do not agree to any deposit or payment.")
        else:
            lines.append(
                "Do not agree to a deposit above "
                f"${attributes.max_deposit_usd:.2f} USD."
            )
    if attributes.instructions:
        lines.append("Instructions:")
        lines.extend(f"- {instruction}" for instruction in attributes.instructions)
    if attributes.allowed_commitments:
        lines.append(
            "The only allowed commitments are: "
            + ", ".join(attributes.allowed_commitments)
            + "."
        )
    else:
        lines.append("Do not make any commitment beyond gathering information.")
    return "\n".join(lines)


def _validate_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.voygr.tech"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise CallwrightConfigurationError(
            "The Callwright base URL must be https://api.voygr.tech."
        )
