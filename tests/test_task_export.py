from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

from repopilot_guard.task_export import TaskEvidenceExporter
from repopilot_guard.task_store import TaskStore


class TaskEvidenceExporterTests(unittest.TestCase):
    def test_exports_registered_mcp_output_artifact_after_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-mcp-export",
                    task_id="task-mcp-export",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                content = ("MCP 原始输出\n" * 2_000).encode("utf-8")
                digest = sha256(content).hexdigest()
                relative_path = f"mcp/outputs/{digest}.json"
                source = root / "runs" / "task-mcp-export" / relative_path
                source.parent.mkdir(parents=True)
                source.write_bytes(content)
                store.sync_graph_result(
                    {
                        "thread_id": "thread-mcp-export",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {
                            "tool_events": [
                                {
                                    "type": "TOOL_CALL",
                                    "artifact": {
                                        "status": "READY",
                                        "kind": "mcp_tool_output",
                                        "relative_path": relative_path,
                                        "sha256": digest,
                                        "output_sha256": digest,
                                        "size_bytes": len(content),
                                    },
                                }
                            ]
                        },
                    },
                    execution_finished=True,
                )

                destination = root / "evidence.zip"
                exported = TaskEvidenceExporter(store).export("thread-mcp-export", destination)
                with ZipFile(destination) as archive:
                    names = archive.namelist()
                    raw = archive.read(f"artifacts/{relative_path}")

                self.assertGreaterEqual(exported.artifact_count, 1)
                self.assertIn(f"artifacts/{relative_path}", names)
                self.assertEqual(content, raw)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
