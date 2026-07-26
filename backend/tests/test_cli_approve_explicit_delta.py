"""`--scope/--was/--now`: the extraction bypass, which had no coverage at all.

Phase 3 of the integration asked whether a Gemini extraction has ever succeeded
end to end with the live key. It has — the call works and returns structured
JSON — but on the canonical Slack sentence the model returned an evidence span
running past the end of the source, which `evidence_span_error` rejects, and a
decision carrying no `requirements` at all, which the engine's three-way match
would reject next. So the extraction path reaches `HUMAN_REVIEW`, and this
bypass is not a convenience: on this input it is the path that works.

It shipped untested. These are its tests.

The bypass is deterministic and does no extraction: it reads the current
requirement out of the workspace's own approved decisions and refuses unless
`--was` matches it exactly. That refusal is the interesting part — it stops an
operator applying a delta computed against a decision that has already moved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dragback.cli import CliError
from dragback.cli_approve import _proposal_from_explicit_delta

SCOPE = "export.authorization"


def _workspace(**overrides: Any) -> dict[str, Any]:
    workspace: dict[str, Any] = {
        "id": "csv-exports",
        # Seeded as a permission id, not a role name: `authority_policy`
        # compares a single `decision.authority_role` string against this set.
        "authority_policy": {SCOPE: ["approve_compliance"]},
        "baseline_decision": {
            "id": "DEC-004",
            "kind": "Decision",
            "title": "CSV export baseline",
            "text": "Exports are open to all users.",
            "scopes": [SCOPE],
            "approval_status": "approved",
            "authority_role": "approve_compliance",
            "effective_at": "2026-07-24T00:00:00Z",
            "source_ref": "slack://T1/C1/0",
            "attributes": {"requirements": {SCOPE: {"audience": "all_users"}}},
        },
        "approved_mutations": [],
    }
    workspace.update(overrides)
    return workspace


def _proposal(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "workspace": _workspace(),
        "raw_text": "Exports must be admin-only.",
        "scope": SCOPE,
        "was": "all_users",
        "now_value": "admin_only",
    }
    arguments.update(overrides)
    return _proposal_from_explicit_delta(**arguments)


def test_the_bypass_builds_a_proposal_without_any_extraction() -> None:
    body = _proposal()

    mutation = body["mutation"] if "mutation" in body else body
    decision = mutation["decision"]

    assert mutation["supersedes_id"] == "DEC-004"
    assert mutation["affected_scopes"] == [SCOPE]
    # The engine requires a three-way exact match between decision.scopes,
    # mutation.affected_scopes and the keys of attributes["requirements"].
    # This is the part the live Gemini extraction did not produce.
    assert decision["scopes"] == [SCOPE]
    assert set(decision["attributes"]["requirements"]) == {SCOPE}
    assert decision["attributes"]["requirements"][SCOPE] == {"audience": "admin_only"}
    # `effective_at` must be set: current_requirements() orders by it and sorts
    # a None to the BOTTOM of precedence, so an "effective immediately" decision
    # without one loses to the decision it supersedes.
    assert decision["effective_at"]


def test_a_stale_was_is_refused_rather_than_applied() -> None:
    """The delta is computed against a decision that has already moved."""

    with pytest.raises(CliError) as raised:
        _proposal(was="admins_only")

    assert raised.value.code == "STALE_EXPLICIT_DELTA"
    # The refusal quotes what the requirement actually is, so the operator can
    # correct it rather than guess.
    assert json.dumps({"audience": "all_users"}, sort_keys=True) in str(raised.value)


def test_a_scope_no_decision_governs_is_refused() -> None:
    with pytest.raises(CliError) as raised:
        _proposal(scope="billing.invoicing")

    assert raised.value.code in {"SCOPE_NOT_GOVERNED", "CURRENT_REQUIREMENT_MISSING"}


def test_a_multi_field_requirement_demands_json_objects() -> None:
    """A bare scalar is ambiguous when the scope carries several fields."""

    workspace = _workspace()
    workspace["baseline_decision"]["attributes"]["requirements"][SCOPE] = {
        "audience": "all_users",
        "format": "csv",
    }

    with pytest.raises(CliError) as raised:
        _proposal(workspace=workspace)
    assert raised.value.code == "AMBIGUOUS_REQUIREMENT_DELTA"

    body = _proposal(
        workspace=workspace,
        was=json.dumps({"audience": "all_users", "format": "csv"}),
        now_value=json.dumps({"audience": "admin_only", "format": "csv"}),
    )
    mutation = body["mutation"] if "mutation" in body else body
    assert mutation["decision"]["attributes"]["requirements"][SCOPE] == {
        "audience": "admin_only",
        "format": "csv",
    }


def test_the_bypass_never_reaches_an_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the fallback: it works with no LLM configured or reachable."""

    import dragback.llm.provider as provider

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the explicit delta must not build an extractor")

    monkeypatch.setattr(provider, "build_decision_extractor", explode)
    assert _proposal() is not None
