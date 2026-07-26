"""`writai doctor` — and the config-driven / fail-closed contract it reports on.

Two things are pinned here.

1. **Doctor tells the truth.** A dead credential is never rendered as a pass,
   "unverified" is its own answer rather than a quiet success, and an
   integration running on a replay says so.
2. **Every integration is config-driven and fails closed.** Setting the
   credential is sufficient to enable the capability with no code change, and an
   unset credential degrades to a labelled fallback rather than a crash or a
   silent pass. Each one is tested BOTH ways.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from writai import doctor
from writai.config import Settings
from writai.doctor import ProbeStatus, render, run_probes


def _settings(**overrides: Any) -> Settings:
    return replace(Settings(), **overrides)


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, _size: int | None = None) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _stub_http(monkeypatch: pytest.MonkeyPatch, status: int, body: str) -> None:
    monkeypatch.setattr(
        doctor.urllib.request,
        "urlopen",
        lambda *_a, **_k: _Response(status, body),
    )


# --------------------------------------------------------------------------------------
# 1. Doctor's own honesty
# --------------------------------------------------------------------------------------


def test_an_absent_credential_is_absent_not_a_failure() -> None:
    """Choosing not to configure an integration must not fail a preflight."""

    results = run_probes(
        _settings(gemini_api_key=None, crustdata_api_key=None),
        only=["gemini", "crustdata"],
    )
    assert {item.status for item in results} == {ProbeStatus.ABSENT}
    assert not any(item.ok for item in results)
    # Each one still names what is lost, in product terms.
    for item in results:
        assert item.degrades_to
        assert item.variables


def test_a_dead_credential_is_reported_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this command exists for: set, non-empty, and rejected."""

    _stub_http(monkeypatch, 401, "{}")
    (result,) = run_probes(_settings(gemini_api_key="dead-key"), only=["gemini"])

    assert result.status is ProbeStatus.INVALID
    assert not result.ok
    assert "rejected" in result.detail
    assert "DEAD" in render([result])


def test_unverified_is_never_rendered_as_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """"We could not check" is not "it works"."""

    def explode(*_a: object, **_k: object) -> None:
        raise OSError("network down")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", explode)
    (result,) = run_probes(_settings(gemini_api_key="k"), only=["gemini"])

    assert result.status is ProbeStatus.UNVERIFIED
    assert not result.ok
    rendered = render([result])
    assert "LIVE" not in rendered
    assert "not a pass" in rendered


def test_a_live_key_pointed_at_a_missing_model_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working key does not mean a working configuration."""

    _stub_http(monkeypatch, 200, '{"models":[{"name":"models/gemini-9.9-pro"}]}')
    (result,) = run_probes(
        _settings(gemini_api_key="k", gemini_model="gemini-does-not-exist"),
        only=["gemini"],
    )
    assert result.status is ProbeStatus.INVALID
    assert "not in the" in result.detail


def test_a_replayed_integration_says_so_even_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CrustData with a valid key STILL cannot fire live. Never imply it can."""

    _stub_http(monkeypatch, 200, "{}")
    (result,) = run_probes(_settings(crustdata_api_key="k"), only=["crustdata"])

    assert result.status is ProbeStatus.LIVE
    assert result.replayed is True
    assert "replay" in result.detail.lower() or "replay" in result.replay_note.lower()
    assert "REPLAYED" in render([result])


def test_hexclave_distinguishes_a_bad_key_from_missing_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These need different fixes, so they must not share one message."""

    base = _settings(
        hexclave_project_id="p", hexclave_secret_key="s", hexclave_team_id=None
    )

    _stub_http(monkeypatch, 401, "{}")
    (rejected,) = run_probes(base, only=["hexclave"])
    assert rejected.status is ProbeStatus.INVALID
    assert "rejected" in rejected.detail

    # Key good, project real, but nobody has created a team yet.
    _stub_http(monkeypatch, 200, '{"items":[]}')
    (unprovisioned,) = run_probes(base, only=["hexclave"])
    assert unprovisioned.status is ProbeStatus.INVALID
    assert "VALID" in unprovisioned.detail
    assert "ZERO teams" in unprovisioned.detail

    # A team exists but is not pasted in: name the value to paste.
    _stub_http(monkeypatch, 200, '{"items":[{"id":"team-abc"}]}')
    (unset,) = run_probes(base, only=["hexclave"])
    assert unset.status is ProbeStatus.INVALID
    assert "team-abc" in unset.detail

    # Fully configured.
    (live,) = run_probes(replace(base, hexclave_team_id="team-abc"), only=["hexclave"])
    assert live.status is ProbeStatus.LIVE
    assert live.ok


def test_a_probe_that_itself_crashes_does_not_kill_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        doctor.PROBES,
        "gemini",
        replace(
            doctor.PROBES["gemini"],
            run=lambda _s: (_ for _ in ()).throw(RuntimeError("probe bug")),
        ),
    )
    (result,) = run_probes(_settings(), only=["gemini"])
    assert result.status is ProbeStatus.UNVERIFIED
    assert "probe itself failed" in result.detail


def test_exit_code_fails_only_on_a_dead_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfigured integration is a choice; a broken one is a fault."""

    monkeypatch.setattr(doctor, "default_settings", _settings(gemini_api_key=None))
    assert doctor.main(["gemini"]) == 0

    monkeypatch.setattr(doctor, "default_settings", _settings(gemini_api_key="dead"))
    _stub_http(monkeypatch, 403, "{}")
    assert doctor.main(["gemini"]) == 1
    capsys.readouterr()


def test_every_integration_names_its_variables_and_its_loss() -> None:
    """A status with no fix-it and no consequence is not actionable."""

    assert set(doctor.PROBES) == {
        "gemini",
        "hexclave",
        "composio",
        "callwright",
        "crustdata",
        "superset",
    }
    for key, probe in doctor.PROBES.items():
        assert probe.variables, key
        assert len(probe.degrades_to) > 40, key


# --------------------------------------------------------------------------------------
# 2. Config-driven and fail-closed, tested BOTH ways
# --------------------------------------------------------------------------------------


def test_gemini_is_config_driven_both_ways() -> None:
    """Unset -> deterministic fixture path. Set -> live adapter. No code change."""

    from writai.llm.provider import build_decision_extractor

    # Unset provider: the caller keeps its fixture path rather than crashing.
    assert build_decision_extractor(_settings(llm_provider="fixture")) is None

    # Set: constructing the live adapter is enough to enable it.
    extractor = build_decision_extractor(
        _settings(llm_provider="gemini", gemini_api_key="k")
    )
    assert type(extractor).__name__ == "GeminiDecisionExtractor"


def test_gemini_fails_closed_when_selected_without_a_key() -> None:
    """A typo must not silently disable extraction."""

    from writai.llm.provider import (
        LLMProviderConfigurationError,
        build_decision_extractor,
    )

    with pytest.raises(LLMProviderConfigurationError):
        build_decision_extractor(_settings(llm_provider="gemini", gemini_api_key=None))
    with pytest.raises(LLMProviderConfigurationError):
        build_decision_extractor(_settings(llm_provider="typo"))


def test_composio_verifier_fails_closed_without_its_secret() -> None:
    """No signing secret means no delivery can be verified, so none is accepted."""

    from writai.intake.slack import ComposioSlackWebhookVerifier, SlackWebhookError

    with pytest.raises(SlackWebhookError, match="COMPOSIO_WEBHOOK_SECRET"):
        ComposioSlackWebhookVerifier(
            triggers=cast("Any", object()), webhook_secret=""
        )


def test_hexclave_checker_fails_closed_on_partial_configuration() -> None:
    """Two of three variables is not a working identity."""

    from writai.auth.hexclave import (
        HexclaveConfigurationError,
        HexclavePermissionChecker,
    )

    with pytest.raises(HexclaveConfigurationError):
        HexclavePermissionChecker(
            project_id="p",
            secret_key="s",
            team_id=None,
            transport=cast("Any", object()),
        )


def test_callwright_is_config_driven_and_defaults_to_the_fixture() -> None:
    """An unset key must record the attempt, never place a call."""

    from writai.integrations import callwright

    assert hasattr(callwright, "FixtureCallwrightClient")
    assert hasattr(callwright, "LiveCallwrightClient")
    # Live calls stay off unless explicitly enabled, independently of the key.
    assert _settings(callwright_api_key="k").callwright_live_calls_enabled is False


def test_crustdata_replay_fails_closed_without_its_bearer() -> None:
    from writai.intake.crustdata import (
        CrustDataAuthenticationNotConfigured,
        CrustDataWebhookBearerVerifier,
    )

    with pytest.raises(CrustDataAuthenticationNotConfigured):
        CrustDataWebhookBearerVerifier(expected_bearer="").require("Bearer anything")


# --------------------------------------------------------------------------------------
# Step 1 of the runbook: state AND what to do about it, in one command
# --------------------------------------------------------------------------------------


def test_every_probe_carries_a_concrete_fallback() -> None:
    """A DEAD integration with no stated workaround is a dead end on stage.

    `doctor` is the runbook's first step. An operator who sees DEAD must learn
    what to type next from this same output, not from a second document they
    have to go find while a room waits.
    """

    from writai import doctor

    for key, probe in doctor.PROBES.items():
        assert probe.fallback.strip(), f"{key} has no fallback"
        # A fallback names an action, not a feeling. Each of ours points at a
        # command, a switch, or an explicit "nothing to do".
        assert any(
            token in probe.fallback
            for token in ("writai ", "scripts/", "=1", "=true", "nothing to do")
        ), f"{key}'s fallback names no concrete action: {probe.fallback}"


def test_a_dead_integration_renders_its_fallback() -> None:
    """The rendered report, not just the dataclass, has to carry it."""

    from writai.doctor import ProbeResult, ProbeStatus, render

    rendered = render(
        [
            ProbeResult(
                name="Hexclave",
                status=ProbeStatus.INVALID,
                detail="zero teams",
                degrades_to="approvals fail closed",
                fallback="keep WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1",
            )
        ]
    )
    assert "DEAD" in rendered
    assert "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1" in rendered


def test_a_live_integration_does_not_nag_with_a_fallback() -> None:
    """Working is working. Printing a workaround under LIVE invites doubt."""

    from writai.doctor import ProbeResult, ProbeStatus, render

    rendered = render(
        [
            ProbeResult(
                name="Gemini",
                status=ProbeStatus.LIVE,
                detail="key accepted",
                degrades_to="no extraction",
                fallback="use the explicit delta",
            )
        ]
    )
    assert "LIVE" in rendered
    assert "use the explicit delta" not in rendered


def test_long_guidance_is_wrapped_rather_than_scrolled_off_screen() -> None:
    """An unwrapped fallback is one nobody reads."""

    from writai.doctor import ProbeResult, ProbeStatus, render

    rendered = render(
        [
            ProbeResult(
                name="Composio",
                status=ProbeStatus.INVALID,
                detail="rejected " * 40,
                degrades_to="the Slack loop is shut " * 20,
                fallback="scripts/demo/fire.sh " * 20,
            )
        ]
    )
    assert max(len(line) for line in rendered.splitlines()) <= 80
