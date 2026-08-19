"""项目技术栈 Profile 的只读识别。

识别不等于支持执行：只有已接入补丁与固定验证运行时的 Profile 才能标为 READY。
其余 Profile 先作为可审计诊断信息，避免界面或模型将“检测到文件”误解为“已开放构建命令”。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    """一个项目技术栈的检测结果，不包含任何文件内容或绝对路径。"""

    profile_id: str
    status: str
    code: str
    display_name: str
    detected_files: tuple[str, ...]
    execution_supported: bool
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "display_name": self.display_name,
            "detected_files": list(self.detected_files),
            "execution_supported": self.execution_supported,
            "message": self.message,
        }


class ProjectProfileDetector:
    """仅根据项目根目录内的标准清单文件识别候选技术栈。"""

    def detect(self, repository: Path) -> tuple[ProjectProfile, ...]:
        root = repository.expanduser().resolve()
        if not root.is_dir():
            return ()

        profiles: list[ProjectProfile] = []
        if (root / "pom.xml").is_file():
            profiles.append(
                ProjectProfile(
                    "java_maven",
                    "READY",
                    "JAVA_MAVEN_PROFILE_READY",
                    "Java / Maven",
                    _existing(root, "pom.xml", "mvnw", "mvnw.cmd"),
                    True,
                    "已识别 Java/Maven；可使用当前受控 Maven Recipe 进行验证。",
                )
            )
        if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            profiles.append(
                ProjectProfile(
                    "java_gradle",
                    "READY",
                    "JAVA_GRADLE_PROFILE_READY",
                    "Java / Gradle",
                    _existing(root, "build.gradle", "build.gradle.kts", "gradlew", "gradlew.bat", "settings.gradle", "settings.gradle.kts"),
                    True,
                    "已识别 Java/Gradle；将冻结受控 Gradle Recipe 进行验证。",
                )
            )
        if (root / "package.json").is_file():
            package_manager_files = _existing(root, "package.json", "pnpm-lock.yaml", "package-lock.json", "yarn.lock")
            profile_id = "node_pnpm" if "pnpm-lock.yaml" in package_manager_files else "node_npm"
            display_name = "Node.js / pnpm" if profile_id == "node_pnpm" else "Node.js / npm"
            profiles.append(
                ProjectProfile(
                    profile_id,
                    "READY",
                    "NODE_PROFILE_READY",
                    display_name,
                    package_manager_files,
                    True,
                    f"已识别 {display_name}；将冻结受控 {profile_id.split('_', 1)[1]} test Recipe 进行验证。",
                )
            )
        python_files = _existing(root, "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
        if python_files:
            profiles.append(
                ProjectProfile(
                    "python_pytest",
                    "READY",
                    "PYTHON_PYTEST_PROFILE_READY",
                    "Python / pytest",
                    python_files,
                    True,
                    "已识别 Python/pytest；将冻结受控 pytest Recipe 进行验证。",
                )
            )
        return tuple(profiles)


def profile_payload(repository: Path) -> dict[str, dict[str, object]]:
    """生成 API/CLI 可直接投影的稳定字典，键为 Profile ID。"""

    return {profile.profile_id: profile.to_dict() for profile in ProjectProfileDetector().detect(repository)}


def _discovered(profile_id: str, display_name: str, detected_files: tuple[str, ...]) -> ProjectProfile:
    return ProjectProfile(
        profile_id,
        "DISCOVERED",
        "PROFILE_EXECUTION_NOT_IMPLEMENTED",
        display_name,
        detected_files,
        False,
        f"已识别 {display_name}；当前版本仅展示诊断，尚未开放该 Profile 的补丁或验证执行。",
    )


def _existing(root: Path, *names: str) -> tuple[str, ...]:
    return tuple(name for name in names if (root / name).is_file())
