from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from writai.llm.extractor import (
    DecisionExtractionCandidate,
    repair_evidence_offsets,
)

VENICE_DEFAULT_BASE_URL = "https://api.venice.ai/api/v1"
VENICE_DEFAULT_MODEL = "openai-gpt-4o-mini-2024-07-18"

_INSTRUCTION = (
    "Extract an untrusted candidate company decision mutation from the text below. "
    "Evidence spans must quote the source exactly and use zero-based, half-open "
    "character offsets. Do not invent evidence or an authority verdict. The decision "
    "confidence must reflect extraction confidence. Return JSON only and conform to "
    "this JSON Schema:\n{schema}\n\nTEXT:\n{raw_text}"
)

# Venice serves many models. Only some advertise `supportsResponseSchema`, and reasoning
# models routinely wrap JSON in a fenced block regardless, so strip fences before validating.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class VeniceExtractionError(RuntimeError):
    """The extractor could not produce a candidate. Never a verdict."""


class VeniceDecisionExtractor:
    """Optional structured extraction adapter backed by Venice's OpenAI-compatible API.

    The returned candidate still passes through deterministic authority rules:
    `apply_extracted_decision` overwrites approval status, authority role,
    confidence, scopes and supersession from `TrustedDecisionContext`, and
    validates every evidence span against the source text. This adapter proposes
    structure and nothing else.

    Uses the REST API through `httpx`, which is already a core dependency, so no
    extra install is required. When `backup_model` is set it is tried once after a
    transport or server-side failure on the primary model.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = VENICE_DEFAULT_MODEL,
        backup_model: str | None = None,
        base_url: str = VENICE_DEFAULT_BASE_URL,
        timeout_seconds: float = 12.0,
        max_attempts: int = 2,
        repair_offsets: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("VENICE_API_KEY must be set to use VeniceDecisionExtractor.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self._api_key = api_key
        self._model = model
        self._backup_model = backup_model or None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._repair_offsets = repair_offsets

    def extract(
        self,
        raw_text: str,
        *,
        scope_vocabulary: set[str] | None = None,
    ) -> DecisionExtractionCandidate:
        del scope_vocabulary
        schema = DecisionExtractionCandidate.model_json_schema()
        prompt = _INSTRUCTION.format(schema=json.dumps(schema), raw_text=raw_text)
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
                last_error = str(exc)
                if attempt == self._max_attempts - 1:
                    raise VeniceExtractionError(
                        f"Venice returned a candidate that failed schema validation: {exc}"
                    ) from exc
                continue
            if self._repair_offsets:
                repair_evidence_offsets(raw_text, candidate)
            return candidate

        raise VeniceExtractionError("Venice extraction produced no candidate.")

    def _models_to_try(self) -> list[str]:
        if self._backup_model and self._backup_model != self._model:
            return [self._model, self._backup_model]
        return [self._model]

    def _post(self, model: str, prompt: str) -> httpx.Response:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        return httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "content-type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            json=payload,
            timeout=self._timeout_seconds,
        )

    def _generate(self, prompt: str) -> str:
        models = self._models_to_try()
        response: httpx.Response | None = None
        transport_error: httpx.HTTPError | None = None

        for index, model in enumerate(models):
            is_last = index == len(models) - 1
            try:
                response = self._post(model, prompt)
            except httpx.HTTPError as exc:
                transport_error = exc
                response = None
                if is_last:
                    raise VeniceExtractionError(f"Venice request failed: {exc}") from exc
                continue
            # A credential or model-name problem will not be fixed by the backup model.
            if response.status_code in (401, 403, 404) or response.status_code < 400 or is_last:
                break

        if response is None:
            raise VeniceExtractionError(f"Venice request failed: {transport_error}")
        if response.status_code in (401, 403):
            raise VeniceExtractionError(
                f"Venice rejected the credential (HTTP {response.status_code}). Check "
                f"VENICE_API_KEY. Response: {response.text[:300]}"
            )
        if response.status_code == 404:
            raise VeniceExtractionError(
                f"Venice model {self._model!r} was not found. List the models your key can "
                f"reach with: curl -H 'Authorization: Bearer $VENICE_API_KEY' "
                f"{self._base_url}/models"
            )
        if response.status_code >= 400:
            raise VeniceExtractionError(
                f"Venice returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise VeniceExtractionError(
                f"Venice response had an unexpected shape: {response.text[:500]}"
            ) from exc

        if not isinstance(text, str) or not text.strip():
            raise VeniceExtractionError("Venice returned an empty candidate.")

        fenced = _FENCE.match(text)
        return fenced.group(1) if fenced else text
