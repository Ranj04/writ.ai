from __future__ import annotations

from writai.config import Settings
from writai.config import settings as default_settings
from writai.llm.extractor import DecisionExtractor
from writai.llm.gemini_adapter import GeminiDecisionExtractor
from writai.llm.venice_adapter import VeniceDecisionExtractor

SUPPORTED_LLM_PROVIDERS = ("fixture", "gemini", "venice")


class LLMProviderConfigurationError(RuntimeError):
    """The configured extraction provider cannot be constructed."""


def build_decision_extractor(
    settings: Settings | None = None,
) -> DecisionExtractor | None:
    """Construct the configured extractor, or None when extraction is fixture-driven.

    `None` means the caller should keep using its deterministic fixture path; it is
    the default so the canonical demo and CSV proof need no credentials. Anything
    other than a supported provider raises rather than silently degrading, because a
    typo in LLM_PROVIDER must not quietly disable live extraction.
    """

    active = settings or default_settings
    provider = active.llm_provider

    if provider == "fixture":
        return None

    if provider == "venice":
        if not active.venice_api_key:
            raise LLMProviderConfigurationError(
                "LLM_PROVIDER=venice requires VENICE_API_KEY to be set."
            )
        return VeniceDecisionExtractor(
            api_key=active.venice_api_key,
            model=active.llm_model,
            backup_model=active.llm_backup_model,
            base_url=active.llm_base_url,
            timeout_seconds=active.llm_timeout_seconds,
        )

    if provider == "gemini":
        if not active.gemini_api_key:
            raise LLMProviderConfigurationError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set."
            )
        return GeminiDecisionExtractor(
            api_key=active.gemini_api_key,
            model=active.gemini_model,
            base_url=active.gemini_base_url,
            timeout_seconds=active.gemini_timeout_seconds,
        )

    raise LLMProviderConfigurationError(
        f"Unknown LLM_PROVIDER {provider!r}. Supported: {', '.join(SUPPORTED_LLM_PROVIDERS)}."
    )
