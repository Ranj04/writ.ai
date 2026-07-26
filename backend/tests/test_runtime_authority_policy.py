from __future__ import annotations

from dataclasses import replace

import pytest
from writai import runtime as runtime_module
from writai.authority.engine import DEFAULT_AUTHORITY_POLICY
from writai.fixtures import load_decision_v18, load_graph_fixture
from writai.graph.memory import MemoryGraphStore


def _disable_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_module,
        "settings",
        replace(runtime_module.settings, demo_reset_enabled=False),
    )
    monkeypatch.setattr(
        runtime_module,
        "create_graph_store",
        lambda _settings: MemoryGraphStore(),
    )


def test_runtime_keeps_existing_default_authority_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reset(monkeypatch)

    runtime = runtime_module.create_authority_runtime()

    assert runtime.authority.authority_policy == DEFAULT_AUTHORITY_POLICY


def test_runtime_injects_a_defensive_copy_of_permission_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reset(monkeypatch)
    policy = {
        "export.authorization": {"approve_compliance"},
        "export.generation": {"approve_engineering"},
    }

    runtime = runtime_module.create_authority_runtime(authority_policy=policy)
    policy["export.authorization"].add("untrusted-role")

    assert runtime.authority.authority_policy == {
        "export.authorization": {"approve_compliance"},
        "export.generation": {"approve_engineering"},
    }


def test_runtime_rejects_an_empty_injected_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reset(monkeypatch)

    with pytest.raises(ValueError, match="must not be empty"):
        runtime_module.create_authority_runtime(authority_policy={})


def test_injected_permission_policy_applies_a_valid_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reset(monkeypatch)
    runtime = runtime_module.create_authority_runtime(
        authority_policy={
            "export.authorization": {"approve_compliance"},
        }
    )
    version, artifacts, edges, _ = load_graph_fixture()
    runtime.graph.reset(version=version, artifacts=artifacts, edges=edges)
    mutation = load_decision_v18().model_copy(deep=True)
    mutation.decision.authority_role = "approve_compliance"

    result = runtime.authority.apply_decision_change(mutation)

    assert result.applied is True
    assert result.graph_version == "graph-v18"
