"""在 Agent 工作前以只读方式识别仓库能力。"""

from __future__ import annotations

from pathlib import Path

from repopilot_guard.models import PreflightResult


class PreflightInspector:
    """不运行外部命令，检测当前支持的 Java、Python 与 Node 项目描述。"""

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
        has_gradle_build = (repository / "build.gradle").is_file() or (repository / "build.gradle.kts").is_file()
        has_pytest_project = any((repository / name).is_file() for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"))
        has_node_project = (repository / "package.json").is_file()
        java_source_root = repository / "src" / "main" / "java"
        if not java_source_root.is_dir():
            java_source_root = None

        wrapper_candidates = (repository / "mvnw.cmd", repository / "mvnw")
        maven_wrapper = next((path for path in wrapper_candidates if path.is_file()), None)
        gradle_wrapper = next((path for path in (repository / "gradlew.bat", repository / "gradlew") if path.is_file()), None)

        if not is_git_repository:
            errors.append("Repository is not a Git working tree.")
        if not has_pom_xml and not has_gradle_build and not has_pytest_project and not has_node_project:
            errors.append("No supported Java, Python or Node build/test descriptor was found.")
        if java_source_root is None and (has_pom_xml or has_gradle_build):
            warnings.append("src/main/java was not found; project may use a non-standard Java layout.")
        if has_pom_xml and maven_wrapper is None:
            warnings.append("Maven Wrapper was not found; a later execution stage may use system Maven.")
        if has_gradle_build and gradle_wrapper is None:
            warnings.append("Gradle Wrapper was not found; a later execution stage may use system Gradle.")

        return PreflightResult(
            repository=repository,
            is_git_repository=is_git_repository,
            has_pom_xml=has_pom_xml,
            java_source_root=java_source_root,
            maven_wrapper=maven_wrapper,
            has_gradle_build=has_gradle_build,
            gradle_wrapper=gradle_wrapper,
            has_pytest_project=has_pytest_project,
            has_node_project=has_node_project,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
