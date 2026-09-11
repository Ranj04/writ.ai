from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest
from writai.workspaces.repository import SqliteLiveWorkspaceRepository


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sqlite_write_resecures_existing_wal_and_shm_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live-workspaces.sqlite3"
    repository = SqliteLiveWorkspaceRepository(path)
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM live_workspaces").fetchone()
        repository._write_transaction(
            lambda connection: connection.execute(
                "INSERT INTO live_workspaces VALUES (?, ?, ?, ?)",
                ("first", "2026-01-01T00:00:00+00:00", 1, "{}"),
            )
        )
        wal = path.with_name(f"{path.name}-wal")
        shm = path.with_name(f"{path.name}-shm")
        assert wal.exists() and shm.exists()
        wal.chmod(0o644)
        shm.chmod(0o644)

        repository._write_transaction(
            lambda connection: connection.execute(
                "UPDATE live_workspaces SET revision = revision + 1"
            )
        )

        assert _mode(wal) == 0o600
        assert _mode(shm) == 0o600
    finally:
        reader.close()


def test_zero_length_sqlite_file_does_not_suppress_json_migration(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "live-workspaces.sqlite3"
    json_path = tmp_path / "live-workspaces.json"
    sqlite_path.touch()
    json_path.write_text(
        '{"schema_version": 1, "workspaces": [{"authorization_state": "must-not-drop"}]}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unreadable|not a SQLite database"):
        SqliteLiveWorkspaceRepository(sqlite_path)

    assert json_path.exists()
