from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

from dragback.domain import GrantPayload, SignedGrant, Verdict, utc_now


class GrantSigner:
    def __init__(self, secret: str, ttl_seconds: int = 300) -> None:
        if not secret:
            raise ValueError("Grant secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(data: str) -> bytes:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)

    def issue(
        self,
        *,
        run_id: str,
        task_id: str,
        decision_snapshot: str,
        plan_hash: str,
        verdict: Verdict = Verdict.ALLOW,
    ) -> SignedGrant:
        now = utc_now()
        payload = GrantPayload(
            authorization_id=f"AUTH-{uuid.uuid4().hex[:8].upper()}",
            run_id=run_id,
            task_id=task_id,
            decision_snapshot=decision_snapshot,
            plan_hash=plan_hash,
            verdict=verdict,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        payload_bytes = json.dumps(
            payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        body = self._b64(payload_bytes)
        signature = self._b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        return SignedGrant(payload=payload, token=f"{body}.{signature}")

    def decode(self, token: str) -> GrantPayload:
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise ValueError("Malformed grant token") from exc
        expected = self._b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid grant signature")
        raw = json.loads(self._unb64(body).decode("utf-8"))
        return GrantPayload.model_validate(raw)
