from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Protocol

from pydantic import BaseModel, Field

from dragback.workspaces.models import LiveWorkspaceRecord


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
