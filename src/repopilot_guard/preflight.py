"""在 Agent 工作前以只读方式识别仓库能力。"""

from __future__ import annotations

from pathlib import Path

from repopilot_guard.models import PreflightResult


class PreflightInspector:
    """不运行外部命令，检测最小 Java/Maven 前置条件。"""

    def inspect(self, repository: Path) -> PreflightResult:
        repository = repository.expanduser().resolve()
        errors: list[str] = []
        warnings: list[str] = []

        if not repository.is_dir():
            return PreflightResult(
                repository=repository,
                is_git_repository=False,
                has_pom_xml=False,
                java_source_root=None,
                maven_wrapper=None,
                errors=("Repository path does not exist or is not a directory.",),
            )

        is_git_repository = (repository / ".git").exists()
        has_pom_xml = (repository / "pom.xml").is_file()
        java_source_root = repository / "src" / "main" / "java"
        if not java_source_root.is_dir():
            java_source_root = None

        wrapper_candidates = (repository / "mvnw.cmd", repository / "mvnw")
        maven_wrapper = next((path for path in wrapper_candidates if path.is_file()), None)

        if not is_git_repository:
            errors.append("Repository is not a Git working tree.")
        if not has_pom_xml:
            errors.append("Maven pom.xml was not found.")
        if java_source_root is None:
            warnings.append("src/main/java was not found; project may use a non-standard Java layout.")
        if maven_wrapper is None:
            warnings.append("Maven Wrapper was not found; a later execution stage may use system Maven.")

        return PreflightResult(
            repository=repository,
            is_git_repository=is_git_repository,
            has_pom_xml=has_pom_xml,
            java_source_root=java_source_root,
            maven_wrapper=maven_wrapper,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
