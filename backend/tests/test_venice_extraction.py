from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from writai.config import settings as default_settings
from writai.llm import (
    LLMProviderConfigurationError,
    VeniceDecisionExtractor,
    VeniceExtractionError,
    build_decision_extractor,
)

SOURCE_TEXT = "Compliance approved this change: exports must be admin-only for every account."


def _candidate_json() -> str:
    """A minimal candidate that satisfies DecisionExtractionCandidate."""

    from writai.fixtures import load_decision_v18

    start = SOURCE_TEXT.index("exports must be admin-only")
    return json.dumps(
        {
            "mutation": json.loads(load_decision_v18().model_dump_json()),
            "evidence_spans": [
                {
                    "start": start,
                    "end": start + len("exports must be admin-only"),
                    "text": "exports must be admin-only",
                }
            ],
        }
    )


def _response(payload_text: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json={"choices": [{"message": {"content": payload_text}}]},
        request=httpx.Request("POST", "https://api.venice.ai/api/v1/chat/completions"),
    )


def _extractor(**kwargs) -> VeniceDecisionExtractor:
    return VeniceDecisionExtractor(api_key="test-key", **kwargs)


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        VeniceDecisionExtractor(api_key="")


def test_extracts_plain_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response(_candidate_json()))
    candidate = _extractor().extract(SOURCE_TEXT)
    assert candidate.evidence_spans[0].text == "exports must be admin-only"


def test_strips_markdown_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasoning models such as minimax-m25 wrap JSON in a fenced block."""

    fenced = f"```json\n{_candidate_json()}\n```"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response(fenced))
    candidate = _extractor().extract(SOURCE_TEXT)
    assert candidate.evidence_spans[0].start >= 0


def test_falls_back_to_backup_model_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        seen.append(json["model"])
        if len(seen) == 1:
            return _response("", status=500)
        return _response(_candidate_json())

    monkeypatch.setattr(httpx, "post", fake_post)
    _extractor(model="primary", backup_model="backup").extract(SOURCE_TEXT)
    assert seen == ["primary", "backup"]


def test_does_not_retry_backup_on_bad_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 will not be fixed by another model, so it must not burn the backup."""

    seen: list[str] = []

    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        seen.append(json["model"])
        return _response("", status=401)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(VeniceExtractionError, match="credential"):
        _extractor(model="primary", backup_model="backup").extract(SOURCE_TEXT)
    assert seen == ["primary"]


def test_empty_content_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response("   "))
    with pytest.raises(VeniceExtractionError, match="empty"):
        _extractor().extract(SOURCE_TEXT)


def test_unexpected_shape_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*a, **k):
        return httpx.Response(
            status_code=200,
            json={"unexpected": True},
            request=httpx.Request("POST", "https://api.venice.ai/api/v1/chat/completions"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(VeniceExtractionError, match="unexpected shape"):
        _extractor().extract(SOURCE_TEXT)


def test_provider_fixture_returns_none() -> None:
    assert build_decision_extractor(replace(default_settings, llm_provider="fixture")) is None


def test_provider_venice_requires_key() -> None:
    broken = replace(default_settings, llm_provider="venice", venice_api_key=None)
    with pytest.raises(LLMProviderConfigurationError, match="VENICE_API_KEY"):
        build_decision_extractor(broken)


def test_provider_venice_builds_extractor() -> None:
    configured = replace(
        default_settings,
        llm_provider="venice",
        venice_api_key="test-key",
        llm_model="openai-gpt-4o-mini-2024-07-18",
    )
    assert isinstance(build_decision_extractor(configured), VeniceDecisionExtractor)


def test_unknown_provider_raises_rather_than_degrading() -> None:
    with pytest.raises(LLMProviderConfigurationError, match="Unknown LLM_PROVIDER"):
        build_decision_extractor(replace(default_settings, llm_provider="typo"))


def test_timeout_ms_is_normalised_to_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_MS", "12000")
    from writai.config import Settings

    assert Settings().llm_timeout_seconds == 12.0


def test_repairs_wrong_offsets_for_a_genuine_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Models quote correctly but miscount offsets; the quote still has to be real."""

    from writai.fixtures import load_decision_v18
    from writai.llm import evidence_span_error

    quote = "exports must be admin-only"
    bad = json.dumps(
        {
            "mutation": json.loads(load_decision_v18().model_dump_json()),
            # Deliberately wrong arithmetic, exactly what the live model returns.
            "evidence_spans": [{"start": 0, "end": 38, "text": quote}],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response(bad))
    candidate = _extractor().extract(SOURCE_TEXT)
    assert evidence_span_error(SOURCE_TEXT, candidate.evidence_spans) is None
    assert SOURCE_TEXT[candidate.evidence_spans[0].start : candidate.evidence_spans[0].end] == quote


def test_does_not_invent_offsets_for_fabricated_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repair must never rescue a quote that is absent from the source."""

    from writai.fixtures import load_decision_v18
    from writai.llm import evidence_span_error

    fabricated = json.dumps(
        {
            "mutation": json.loads(load_decision_v18().model_dump_json()),
            "evidence_spans": [{"start": 0, "end": 20, "text": "legal signed off on this"}],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response(fabricated))
    candidate = _extractor().extract(SOURCE_TEXT)
    assert evidence_span_error(SOURCE_TEXT, candidate.evidence_spans) is not None


def test_repair_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from writai.fixtures import load_decision_v18
    from writai.llm import evidence_span_error

    bad = json.dumps(
        {
            "mutation": json.loads(load_decision_v18().model_dump_json()),
            "evidence_spans": [{"start": 0, "end": 38, "text": "exports must be admin-only"}],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _response(bad))
    candidate = _extractor(repair_offsets=False).extract(SOURCE_TEXT)
    assert evidence_span_error(SOURCE_TEXT, candidate.evidence_spans) is not None
