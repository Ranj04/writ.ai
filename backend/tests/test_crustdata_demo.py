from __future__ import annotations

from writai.crustdata_demo import render_rehearsal, run_rehearsal


def test_rehearsal_uses_documentation_reconstructed_provenance() -> None:
    result = run_rehearsal()

    assert result.fixture_provenance.kind == "documentation-reconstructed"
    assert result.fixture_provenance.captured_from_crustdata is False
    assert len(result.flags) == 1
    assert result.flags[0].change_kind == "role-change"


def test_rehearsal_requires_human_review_without_mutating_graph() -> None:
    result = run_rehearsal()
    flag = result.flags[0]

    assert result.human_review_required is True
    assert result.graph_mutated is False
    assert flag.review_status == "pending-human-review"
    assert flag.human_confirmation_required is True
    assert flag.graph_mutated is False


def test_rehearsal_output_cannot_be_mistaken_for_live() -> None:
    output = render_rehearsal(run_rehearsal())

    assert "NOT LIVE" in output
    assert "CrustData API called: no" in output
    assert "Callback captured: no" in output
    assert "not live sponsor-usage evidence" in output
    assert "Source: live" not in output
    assert "replayed from server capture" not in output


def test_rehearsal_is_repeatable() -> None:
    first = render_rehearsal(run_rehearsal())
    second = render_rehearsal(run_rehearsal())

    assert first == second
