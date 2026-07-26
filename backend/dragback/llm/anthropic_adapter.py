from __future__ import annotations

import json

from dragback.llm.extractor import DecisionExtractionCandidate


class AnthropicDecisionExtractor:
    """Optional structured extraction adapter.

    The returned candidate still passes through deterministic authority rules.
    Keep this adapter out of the critical live-demo path unless it has been
    rehearsed with the exact fixture.
    """

    def __init__(self, *, api_key: str, model: str) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError('Install Dragback with `pip install -e ".[llm]"`') from exc
        self._client = Anthropic(api_key=api_key)
        self._model = model

    def extract(
        self,
        raw_text: str,
        *,
        scope_vocabulary: set[str] | None = None,
    ) -> DecisionExtractionCandidate:
        del scope_vocabulary
        schema = DecisionExtractionCandidate.model_json_schema()
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1200,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract an untrusted candidate company decision mutation from the text "
                        "below. Evidence spans must quote the source exactly and use zero-based, "
                        "half-open character offsets. Do not invent evidence or an authority "
                        "verdict. The decision confidence must reflect extraction confidence. "
                        "Return JSON only and conform to this JSON Schema:\n"
                        f"{json.dumps(schema)}\n\nTEXT:\n{raw_text}"
                    ),
                }
            ],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        return DecisionExtractionCandidate.model_validate_json(text)
