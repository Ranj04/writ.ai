"""B1-3: stores derived from the workspace store's name are JSON and say so."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from writai.services import agent_api, authority_api
from writai.workspaces.repository import workspace_store_sibling


def test_a_sibling_store_carries_json_whatever_the_workspace_store_is(
    tmp_path: Path,
) -> None:
    """The default workspace store ends ``.sqlite3``; its siblings are documents."""

    assert workspace_store_sibling(
        tmp_path / "live-workspaces.sqlite3", "slack-deliveries"
    ) == (tmp_path / "live-workspaces-slack-deliveries.json")
    assert workspace_store_sibling(tmp_path / "live-workspaces.db", "x") == (
        tmp_path / "live-workspaces-x.json"
    )
    # A JSON workspace store and a suffix-less one already produced this name.
    assert workspace_store_sibling(tmp_path / "live-workspaces.json", "x") == (
        tmp_path / "live-workspaces-x.json"
    )
    assert workspace_store_sibling(tmp_path / "live-workspaces", "x") == (
        tmp_path / "live-workspaces-x.json"
    )


def test_a_ledger_left_under_the_inherited_suffix_is_refused_not_moved(
    tmp_path: Path,
) -> None:
    """An operator who ran an earlier release has ``...-slack-deliveries.sqlite3``.

    Reading past it would silently start an empty replay ledger; moving it
    would be the service renaming an operator's file. It is refused with both
    paths in the message and left exactly as found.
    """

    inherited = tmp_path / "live-workspaces-slack-deliveries.sqlite3"
    inherited.write_text('{"deliveries": {}}', encoding="utf-8")
    before = inherited.read_bytes()

    with pytest.raises(RuntimeError) as refusal:
        workspace_store_sibling(tmp_path / "live-workspaces.sqlite3", "slack-deliveries")

    message = str(refusal.value)
    assert str(inherited) in message
    assert str(tmp_path / "live-workspaces-slack-deliveries.json") in message
    assert inherited.exists() and inherited.read_bytes() == before
    assert not (tmp_path / "live-workspaces-slack-deliveries.json").exists()


def test_the_refusal_lifts_once_the_json_path_exists(tmp_path: Path) -> None:
    """After the operator renames (or copies) the ledger, the old file is ignored."""

    (tmp_path / "live-workspaces-slack-deliveries.sqlite3").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "live-workspaces-slack-deliveries.json").write_text(
        "{}", encoding="utf-8"
    )

    assert workspace_store_sibling(
        tmp_path / "live-workspaces.sqlite3", "slack-deliveries"
    ) == (tmp_path / "live-workspaces-slack-deliveries.json")


def test_both_services_derive_their_ledgers_through_the_same_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "live-workspaces.sqlite3"
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(authority_api.settings, workspace_store=str(store)),
    )
    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(agent_api.settings, workspace_store=str(store)),
    )
    monkeypatch.setattr(agent_api, "crustdata_replay_stores", {})

    assert authority_api._workspace_store_sibling("slack-approval-threads") == (
        tmp_path / "live-workspaces-slack-approval-threads.json"
    )
    assert agent_api._crustdata_replay_store().path == (
        tmp_path / "live-workspaces-crustdata-deliveries.json"
    )
