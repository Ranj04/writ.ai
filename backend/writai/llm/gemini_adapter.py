from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from writai.llm.extractor import (
    DecisionExtractionCandidate,
    decision_extraction_error,
    evidence_span_error,
    repair_evidence_offsets,
)

GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

_INSTRUCTION = (
    "Extract an untrusted candidate company decision mutation from the text below. "
    "{scope_instruction}"
    "For every evidence span, copy `text` as an exact, non-empty substring of the "
    "source. Do not calculate character offsets: set `start` and `end` to 0; trusted "
    "code computes them from the exact quote. "
    "`mutation.decision.attributes.requirements` is mandatory and must be a non-empty "
    "object. It must have exactly one key for every value in "
    "`mutation.affected_scopes`, and each value must be a non-empty object describing "
    "the new requirement for that scope. Do not invent evidence or an authority "
    "verdict. The decision confidence must reflect extraction confidence. Return JSON "
    "only and conform to this JSON Schema:\n{schema}\n\nTEXT:\n{raw_text}"
)


class GeminiExtractionError(RuntimeError):
    """The extractor could not produce a candidate. Never a verdict."""


class GeminiDecisionExtractor:
    """Optional structured extraction adapter.

    The returned candidate still passes through deterministic authority rules:
    `apply_extracted_decision` overwrites approval status, authority role,
    confidence, scopes and supersession from `TrustedDecisionContext`, and
    validates every evidence span against the source text. This adapter proposes
    structure and nothing else.

    Uses the REST API through `httpx`, which is already a core dependency, so no
    extra install is required.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = GEMINI_DEFAULT_MODEL,
        base_url: str = GEMINI_DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_attempts: int = 2,
        repair_offsets: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY must be set to use GeminiDecisionExtractor.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._repair_offsets = repair_offsets
        self._auth_scheme: str | None = None

    def extract(
        self,
        raw_text: str,
        *,
        scope_vocabulary: set[str] | None = None,
    ) -> DecisionExtractionCandidate:
        schema = DecisionExtractionCandidate.model_json_schema()
        normalized_scopes = tuple(
            sorted(scope.strip() for scope in (scope_vocabulary or set()) if scope.strip())
        )
        scope_instruction = (
            "Use only these trusted scope identifiers in `mutation.affected_scopes`, "
            "`mutation.decision.scopes`, and the requirements keys: "
            f"{json.dumps(normalized_scopes)}. Never invent, rename, or translate "
            "a scope identifier. "
            if normalized_scopes
            else ""
        )
        prompt = _INSTRUCTION.format(
            schema=json.dumps(schema),
            raw_text=raw_text,
            scope_instruction=scope_instruction,
        )
        last_error: str | None = None

        for attempt in range(self._max_attempts):
            text = self._generate(
                prompt
                if last_error is None
                else f"{prompt}\n\nYour previous response was rejected: {last_error}\n"
                "Return corrected JSON only."
            )
            try:
                candidate = DecisionExtractionCandidate.model_validate_json(text)
            except ValidationError as exc:
                last_error = f"schema validation failed: {exc}"
            else:
                if self._repair_offsets:
                    repair_evidence_offsets(raw_text, candidate)
                last_error = decision_extraction_error(candidate)
                if last_error is None:
                    last_error = evidence_span_error(
                        raw_text,
                        candidate.evidence_spans,
                    )
                if last_error is None:
                    return candidate
            if attempt == self._max_attempts - 1:
                raise GeminiExtractionError(
                    "Gemini returned a candidate that failed deterministic "
                    f"validation: {last_error}"
                )

        raise GeminiExtractionError("Gemini extraction produced no candidate.")

    def _auth_headers(self, scheme: str) -> dict[str, str]:
        base = {"content-type": "application/json"}
        if scheme == "bearer":
            return {**base, "Authorization": f"Bearer {self._api_key}"}
        return {**base, "x-goog-api-key": self._api_key}

    def _generate(self, prompt: str) -> str:
        url = f"{self._base_url}/models/{self._model}:generateContent"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                # responseMimeType guarantees syntactically valid JSON. We deliberately do
                # not send responseSchema: Gemini accepts only an OpenAPI 3.0 subset, and
                # Pydantic emits $defs/$ref/anyOf, which it rejects. The schema travels in
                # the prompt instead and Pydantic validates the result.
                "responseMimeType": "application/json",
                "temperature": 0,
            },
        }
        # AI Studio issues keys in more than one format (older `AIza…`, newer `AQ.…`).
        # `x-goog-api-key` is correct for both as far as we can tell, but a single 401
        # would otherwise look like a bad key, so fall back to bearer auth once before
        # giving up. Whichever succeeds is used for the rest of the process.
        schemes = [self._auth_scheme] if self._auth_scheme else ["x-goog-api-key", "bearer"]
        response = None
        for index, scheme in enumerate(schemes):
            try:
                response = httpx.post(
                    url,
                    headers=self._auth_headers(scheme),
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise GeminiExtractionError(f"Gemini request failed: {exc}") from exc
            if response.status_code not in (401, 403) or index == len(schemes) - 1:
                if response.status_code < 400:
                    self._auth_scheme = scheme
                break

        assert response is not None
        if response.status_code in (401, 403):
            raise GeminiExtractionError(
                f"Gemini rejected the credential (HTTP {response.status_code}) using both "
                "'x-goog-api-key' and bearer auth. Open the key in Google AI Studio, use "
                "'Copy cURL quickstart', and compare the auth header it emits with "
                f"GEMINI_BASE_URL={self._base_url}. Response: {response.text[:300]}"
            )
        if response.status_code == 404:
            raise GeminiExtractionError(
                f"Gemini model {self._model!r} was not found. List the models your key can "
                f"reach with: curl -H 'x-goog-api-key: $GEMINI_API_KEY' {self._base_url}/models"
            )
        if response.status_code >= 400:
            raise GeminiExtractionError(
                f"Gemini returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
            parts = body["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError) as exc:
            raise GeminiExtractionError(
                f"Gemini response had an unexpected shape: {response.text[:500]}"
            ) from exc

        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            raise GeminiExtractionError("Gemini returned an empty candidate.")
        return text
