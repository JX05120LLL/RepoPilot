"""将已校验的任务产物导出为可移交的本地审计包。"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath

from repopilot_guard.task_store import StoredTask, StoredTaskArtifact, TaskStore


MAX_ARTIFACT_BYTES = 512 * 1024
MAX_EVENT_COUNT = 1_000
MAX_BUNDLE_SOURCE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TaskEvidenceExport:
    thread_id: str
    artifact_count: int
    event_count: int
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "artifact_count": self.artifact_count,
            "event_count": self.event_count,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


class TaskEvidenceExporter:
    """只导出 SQLite 已登记且逐项通过哈希校验的当前产物。"""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def export(self, thread_id: str, output: Path) -> TaskEvidenceExport:
        task = self.store.get(thread_id)
        if task.status not in {"REPORT", "BLOCKED", "CANCELLED"} or task.lease_expires_at is not None:
            raise ValueError("TASK_EXPORT_NOT_FINALIZED")
        target = _export_target(output)
        artifacts = self.store.artifacts(thread_id)
        events = self._events(thread_id)
        entries, source_size = self._artifact_entries(thread_id, artifacts)
        evidence = _jsonl_bytes([event.to_dict() for event in events])
        if source_size + len(evidence) > MAX_BUNDLE_SOURCE_BYTES:
            raise ValueError("TASK_EXPORT_TOO_LARGE")
        manifest = _manifest(task, artifacts, events, evidence)
        self._write_zip(target, entries, evidence, manifest)
        content = target.read_bytes()
        return TaskEvidenceExport(
            thread_id=thread_id,
            artifact_count=len(artifacts),
            event_count=len(events),
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
        )

    def _events(self, thread_id: str) -> tuple[object, ...]:
        events = self.store.events_after(thread_id, 0, limit=MAX_EVENT_COUNT)
        if events:
            remaining = self.store.events_after(thread_id, events[-1].sequence, limit=1)
            if remaining:
                raise ValueError("TASK_EXPORT_EVIDENCE_TOO_LARGE")
        return events

    def _artifact_entries(
        self,
        thread_id: str,
        artifacts: tuple[StoredTaskArtifact, ...],
    ) -> tuple[tuple[tuple[str, bytes], ...], int]:
        entries: list[tuple[str, bytes]] = []
        total = 0
        for artifact in artifacts:
            if artifact.size_bytes > MAX_ARTIFACT_BYTES:
                raise ValueError("TASK_EXPORT_ARTIFACT_TOO_LARGE")
            archive_name = _artifact_archive_name(artifact)
            _, content = self.store.read_artifact(thread_id, artifact.kind, max_bytes=MAX_ARTIFACT_BYTES)
            encoded = content.encode("utf-8")
            total += len(encoded)
            if total > MAX_BUNDLE_SOURCE_BYTES:
                raise ValueError("TASK_EXPORT_TOO_LARGE")
            entries.append((archive_name, encoded))
        return tuple(entries), total

    @staticmethod
    def _write_zip(target: Path, entries: tuple[tuple[str, bytes], ...], evidence: bytes, manifest: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".zip.tmp",
                prefix=f".{target.stem}-",
                dir=target.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            with zipfile.ZipFile(temporary, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                archive.writestr("manifest.json", manifest)
                archive.writestr("evidence.jsonl", evidence)
                for name, content in entries:
                    archive.writestr(name, content)
            os.replace(temporary, target)
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("TASK_EXPORT_WRITE_FAILED") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _export_target(output: Path) -> Path:
    target = output.expanduser().resolve()
    if target.suffix.lower() != ".zip":
        raise ValueError("TASK_EXPORT_OUTPUT_INVALID")
    if target.exists():
        raise ValueError("TASK_EXPORT_OUTPUT_EXISTS")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError("TASK_EXPORT_WRITE_FAILED") from error
    return target


def _artifact_archive_name(artifact: StoredTaskArtifact) -> str:
    path = PurePosixPath(artifact.relative_path.replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError("TASK_EXPORT_ARTIFACT_INVALID")
    return f"artifacts/{path.as_posix()}"


def _manifest(
    task: StoredTask,
    artifacts: tuple[StoredTaskArtifact, ...],
    events: tuple[object, ...],
    evidence: bytes,
) -> bytes:
    payload = {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "task": {
            "thread_id": task.thread_id,
            "trace_id": task.trace_id,
            "task_id": task.task_id,
            "display_title": task.display_title,
            "project_id": task.project_id,
            "task_mode": task.task_mode,
            "task_operation": task.task_operation,
            "permission_mode": task.permission_mode,
            "workspace_mode": task.workspace_mode,
            "status": task.status,
            "verdict": task.verdict,
            "error_summary": task.error_summary,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "archived_at": task.archived_at,
        },
        "artifacts": [artifact.to_dict() for artifact in artifacts],
        "evidence": {
            "event_count": len(events),
            "sha256": sha256(evidence).hexdigest(),
            "path": "evidence.jsonl",
        },
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _jsonl_bytes(events: list[dict[str, object]]) -> bytes:
    return "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events).encode("utf-8")
