from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field

from writai.workspaces.models import LiveWorkspaceRecord

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class LiveWorkspaceNotFound(KeyError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        self.workspace_id = workspace_id


class LiveWorkspaceConflict(ValueError):
    pass


class LiveWorkspaceRepository(Protocol):
    def create(self, record: LiveWorkspaceRecord) -> None: ...

    def save(self, record: LiveWorkspaceRecord) -> None: ...

    def get(self, workspace_id: str) -> LiveWorkspaceRecord: ...

    def list(self) -> list[LiveWorkspaceRecord]: ...

    def mutate(
        self,
        workspace_id: str,
        apply: Callable[[LiveWorkspaceRecord], LiveWorkspaceRecord],
    ) -> LiveWorkspaceRecord:
        """Read, apply and write one record without a concurrent writer between.

        ``apply`` receives the stored record, returns the record to write, and
        may raise to leave the store untouched. Raises ``LiveWorkspaceNotFound``
        when there is no such workspace.
        """
        ...


class _WorkspaceStoreDocument(BaseModel):
    schema_version: int = 1
    workspaces: list[LiveWorkspaceRecord] = Field(default_factory=list)


class JsonFileLiveWorkspaceRepository:
    """Small atomic JSON store for hackathon-grade persistent workspaces."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def _read(self) -> _WorkspaceStoreDocument:
        if not self.path.exists():
            return _WorkspaceStoreDocument()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _WorkspaceStoreDocument.model_validate(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Live Workspace store is unreadable: {self.path}"
            ) from exc

    def _write(self, document: _WorkspaceStoreDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = document.model_dump_json(indent=2)
        try:
            temporary.write_text(f"{payload}\n", encoding="utf-8")
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def create(self, record: LiveWorkspaceRecord) -> None:
        with self._lock:
            document = self._read()
            if any(item.definition.id == record.definition.id for item in document.workspaces):
                raise LiveWorkspaceConflict(
                    f"Live Workspace already exists: {record.definition.id}"
                )
            document.workspaces.append(record.model_copy(deep=True))
            self._write(document)

    def save(self, record: LiveWorkspaceRecord) -> None:
        with self._lock:
            document = self._read()
            for index, current in enumerate(document.workspaces):
                if current.definition.id == record.definition.id:
                    document.workspaces[index] = record.model_copy(deep=True)
                    self._write(document)
                    return
            raise LiveWorkspaceNotFound(record.definition.id)

    def get(self, workspace_id: str) -> LiveWorkspaceRecord:
        with self._lock:
            for record in self._read().workspaces:
                if record.definition.id == workspace_id:
                    return record.model_copy(deep=True)
        raise LiveWorkspaceNotFound(workspace_id)

    def list(self) -> list[LiveWorkspaceRecord]:
        with self._lock:
            return [
                record.model_copy(deep=True)
                for record in sorted(
                    self._read().workspaces,
                    key=lambda item: item.updated_at,
                    reverse=True,
                )
            ]

    def mutate(
        self,
        workspace_id: str,
        apply: Callable[[LiveWorkspaceRecord], LiveWorkspaceRecord],
    ) -> LiveWorkspaceRecord:
        """Read, apply and write under one lock acquisition.

        Serialised only within this process: the lock does nothing for a second
        process rewriting the same document. That is the property the SQLite
        store exists to fix.
        """

        with self._lock:
            document = self._read()
            for index, current in enumerate(document.workspaces):
                if current.definition.id == workspace_id:
                    updated = apply(current.model_copy(deep=True))
                    _require_same_workspace(workspace_id, updated)
                    document.workspaces[index] = updated.model_copy(deep=True)
                    self._write(document)
                    return updated
            raise LiveWorkspaceNotFound(workspace_id)


def _require_same_workspace(workspace_id: str, record: LiveWorkspaceRecord) -> None:
    if record.definition.id != workspace_id:
        raise LiveWorkspaceConflict(
            "A workspace mutation may not change the workspace id: "
            f"{workspace_id} -> {record.definition.id}"
        )


_LEGACY_STORE_SUFFIX = ".json"
#: The first 16 bytes of every SQLite database file (format 3).
_SQLITE_HEADER = b"SQLite format 3\x00"


def open_live_workspace_repository(path: str | Path) -> LiveWorkspaceRepository:
    """Select the store from the configured path's suffix.

    ``.json`` keeps the legacy document store; every other suffix (``.sqlite3``,
    ``.db``) selects the SQLite store, matching the ``workspace_store`` comment in
    ``config.py``.
    """

    store_path = Path(path).expanduser()
    if store_path.suffix == _LEGACY_STORE_SUFFIX:
        return JsonFileLiveWorkspaceRepository(store_path)
    return SqliteLiveWorkspaceRepository(store_path)


#: Every store derived from the workspace store's name is a JSON document.
_SIBLING_STORE_SUFFIX = ".json"


def workspace_store_sibling(workspace_store: str | Path, label: str) -> Path:
    """``<dir>/<stem>-<label>.json`` next to the configured workspace store.

    The Slack approval-thread, Slack delivery and CrustData delivery ledgers are
    JSON documents whatever the workspace store is, so they carry ``.json``
    rather than inheriting its ``.sqlite3``. Earlier releases inherited it. A
    ledger still sitting under that name is refused, not read and not moved:
    it is a replay ledger or an approval-thread map, and an operator should
    rename it knowingly rather than have the service copy or lose it.
    """

    workspace_path = Path(workspace_store).expanduser()
    sibling = workspace_path.with_name(
        f"{workspace_path.stem}-{label}{_SIBLING_STORE_SUFFIX}"
    )
    inherited_suffix = workspace_path.suffix
    if inherited_suffix and inherited_suffix != _SIBLING_STORE_SUFFIX:
        inherited = workspace_path.with_name(
            f"{workspace_path.stem}-{label}{inherited_suffix}"
        )
        if inherited.exists() and not sibling.exists():
            raise RuntimeError(
                f"Sibling store {inherited} carries the workspace store's suffix "
                f"from an earlier release; it is now read from {sibling}. Rename "
                "it to that path and restart. It was neither read nor moved."
            )
    return sibling


class SqliteLiveWorkspaceRepository:
    """One row per workspace; owner-only from creation; serialised across processes.

    Shaped after ``notify/escalation_source.py``'s
    ``SqliteInterruptEscalationGrantStore``: ``record_json`` column,
    ``BEGIN IMMEDIATE`` for every write, ``_secure_permissions()`` after every
    connection. ``get()`` is one indexed lookup, so the ``PreToolUse`` hot path
    no longer pays for every unrelated workspace in the store.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()
        legacy_records: list[LiveWorkspaceRecord] | None = None
        legacy_path = self.path.with_suffix(_LEGACY_STORE_SUFFIX)
        if not self.path.exists() and legacy_path.exists():
            # Read the legacy document before the durable file exists so an
            # unreadable document leaves nothing behind that would skip the
            # migration on the next start.
            legacy_records = JsonFileLiveWorkspaceRepository(legacy_path).list()
        self._initialize()
        if legacy_records is not None:
            self._migrate(legacy_path, legacy_records)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._require_sqlite_database()
        try:
            # Owner-only from the first byte: the mode is fixed before SQLite ever
            # opens the file, so signed grants never sit behind the process umask.
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
        except FileExistsError:
            pass
        connection = self._connect()
        try:
            # WAL lets the enforcement hot path read while an approval writes;
            # FULL is the right durability for a store holding authorization state.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS live_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    updated_at   TEXT NOT NULL,
                    revision     INTEGER NOT NULL,
                    record_json  TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS live_workspaces_updated_at
                    ON live_workspaces (updated_at DESC)
                """
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            raise self._not_a_database() from exc
        finally:
            connection.close()
            self._secure_permissions()

    def _require_sqlite_database(self) -> None:
        """Refuse an existing file that SQLite would silently treat as empty.

        SQLite opens a zero-length file as a fresh database, so an interrupted
        create, a failed write or a bad restore would come up as a clean, empty
        store with every approved workspace gone and the JSON migration skipped.
        The file is left exactly as found; the operator decides what it was.
        """

        if not self.path.exists():
            return
        try:
            with self.path.open("rb") as handle:
                header = handle.read(len(_SQLITE_HEADER))
        except OSError as exc:
            raise RuntimeError(
                f"Live Workspace store is unreadable: {self.path}"
            ) from exc
        if header != _SQLITE_HEADER:
            raise self._not_a_database()

    def _not_a_database(self) -> RuntimeError:
        # T0 moved the default path to ``.sqlite3`` before this store existed,
        # so a JSON document can sit under the SQLite name. Say so.
        return RuntimeError(
            f"Live Workspace store is not a SQLite database: {self.path} "
            f"({self.path.stat().st_size} bytes). It was not initialised as an "
            f"empty store. If it is a JSON document, rename it to "
            f"{self.path.stem}{_LEGACY_STORE_SUFFIX} and restart to migrate it; "
            "otherwise move it aside and restart."
        )

    def _migrate(self, legacy_path: Path, records: list[LiveWorkspaceRecord]) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for record in records:
                    self._insert(connection, record)
                connection.commit()
            except BaseException:
                connection.rollback()
                connection.close()
                self._discard_database_files()
                raise
            finally:
                connection.close()
                self._secure_permissions()
        logger.info(
            "Migrated %d Live Workspace record(s) from %s into %s; "
            "the JSON store was left in place.",
            len(records),
            legacy_path,
            self.path,
        )

    def _sibling_paths(self) -> tuple[Path, Path]:
        return (
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )

    def _discard_database_files(self) -> None:
        # Only reached from the constructor, for files this constructor created.
        for candidate in (self.path, *self._sibling_paths()):
            candidate.unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self.path, timeout=30.0)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Live Workspace store is unavailable: {self.path}"
            ) from exc

    def _secure_permissions(self) -> None:
        # The WAL carries every committed row, signed grants included, until the
        # next checkpoint. SQLite creates both siblings owner-only when the
        # database file is, but a sibling that was already wider stays wider
        # unless it is re-secured here; a missing sibling is normal.
        for candidate in (self.path, *self._sibling_paths()):
            try:
                candidate.chmod(0o600)
            except OSError as exc:
                if candidate.exists():
                    raise RuntimeError(
                        "Live Workspace store permissions could not be secured: "
                        f"{candidate}"
                    ) from exc

    @staticmethod
    def _decode(raw: str, workspace_id: str) -> LiveWorkspaceRecord:
        try:
            return LiveWorkspaceRecord.model_validate_json(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Live Workspace store holds an unreadable record: {workspace_id}"
            ) from exc

    @staticmethod
    def _insert(connection: sqlite3.Connection, record: LiveWorkspaceRecord) -> None:
        try:
            connection.execute(
                """
                INSERT INTO live_workspaces (
                    workspace_id, updated_at, revision, record_json
                ) VALUES (?, ?, 1, ?)
                """,
                (
                    record.definition.id,
                    record.updated_at.isoformat(),
                    record.model_dump_json(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LiveWorkspaceConflict(
                f"Live Workspace already exists: {record.definition.id}"
            ) from exc

    @staticmethod
    def _update(connection: sqlite3.Connection, record: LiveWorkspaceRecord) -> None:
        workspace_id = record.definition.id
        row = connection.execute(
            "SELECT revision FROM live_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise LiveWorkspaceNotFound(workspace_id)
        cursor = connection.execute(
            """
            UPDATE live_workspaces
               SET record_json = ?, updated_at = ?, revision = revision + 1
             WHERE workspace_id = ? AND revision = ?
            """,
            (
                record.model_dump_json(),
                record.updated_at.isoformat(),
                workspace_id,
                row[0],
            ),
        )
        if cursor.rowcount != 1:
            raise LiveWorkspaceNotFound(workspace_id)

    def _write_transaction(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        # BEGIN IMMEDIATE takes SQLite's write lock, which serialises writers
        # across processes; the RLock only spares in-process writers the busy wait.
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except sqlite3.Error as exc:
                connection.rollback()
                raise RuntimeError(
                    f"Live Workspace store is unavailable: {self.path}"
                ) from exc
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
                self._secure_permissions()

    def create(self, record: LiveWorkspaceRecord) -> None:
        stored = record.model_copy(deep=True)
        self._write_transaction(lambda connection: self._insert(connection, stored))

    def save(self, record: LiveWorkspaceRecord) -> None:
        stored = record.model_copy(deep=True)
        self._write_transaction(lambda connection: self._update(connection, stored))

    def get(self, workspace_id: str) -> LiveWorkspaceRecord:
        # Readers deliberately take no process lock: under WAL a read never waits
        # on a writer, which is what keeps the hook path inside its timeout.
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT record_json FROM live_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Live Workspace store is unavailable: {self.path}"
            ) from exc
        finally:
            connection.close()
        if row is None:
            raise LiveWorkspaceNotFound(workspace_id)
        return self._decode(row[0], workspace_id)

    def list(self) -> list[LiveWorkspaceRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT workspace_id, record_json
                  FROM live_workspaces
                 ORDER BY updated_at DESC, rowid ASC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Live Workspace store is unavailable: {self.path}"
            ) from exc
        finally:
            connection.close()
        return [self._decode(raw, workspace_id) for workspace_id, raw in rows]

    def mutate(
        self,
        workspace_id: str,
        apply: Callable[[LiveWorkspaceRecord], LiveWorkspaceRecord],
    ) -> LiveWorkspaceRecord:
        """Read, apply and write inside one ``BEGIN IMMEDIATE`` transaction.

        Atomic across processes, not merely serialised: a second writer cannot
        read the row until this transaction has committed or rolled back.
        """

        def operation(connection: sqlite3.Connection) -> LiveWorkspaceRecord:
            row = connection.execute(
                "SELECT record_json FROM live_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise LiveWorkspaceNotFound(workspace_id)
            updated = apply(self._decode(row[0], workspace_id))
            _require_same_workspace(workspace_id, updated)
            self._update(connection, updated.model_copy(deep=True))
            return updated

        return self._write_transaction(operation)
