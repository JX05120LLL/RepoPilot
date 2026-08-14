from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.project_profiles import ProjectProfileDetector, profile_payload
from repopilot_guard.profile_runtime import ProfileRuntimeInspector
from repopilot_guard.project_diagnostics import diagnose_project
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.preflight import PreflightInspector


class ProjectProfileDetectorTests(unittest.TestCase):
    def test_detects_maven_as_currently_executable_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "pom.xml").write_text("<project/>", encoding="utf-8")
            (root / "mvnw.cmd").write_text("@echo off", encoding="utf-8")

            profiles = profile_payload(root)

        self.assertEqual("READY", profiles["java_maven"]["status"])
        self.assertTrue(profiles["java_maven"]["execution_supported"])
        self.assertEqual(["pom.xml", "mvnw.cmd"], profiles["java_maven"]["detected_files"])

    def test_detected_profiles_report_minimum_execution_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("build.gradle.kts", "package.json", "pnpm-lock.yaml", "pyproject.toml"):
                (root / name).write_text("{}", encoding="utf-8")

            profiles = {profile.profile_id: profile for profile in ProjectProfileDetector().detect(root)}

        self.assertEqual({"java_gradle", "node_pnpm", "python_pytest"}, set(profiles))
        gradle = profiles.pop("java_gradle")
        self.assertEqual("READY", gradle.status)
        self.assertTrue(gradle.execution_supported)
        self.assertEqual("JAVA_GRADLE_PROFILE_READY", gradle.code)
        python = profiles.pop("python_pytest")
        self.assertEqual("READY", python.status)
        self.assertTrue(python.execution_supported)
        self.assertEqual("PYTHON_PYTEST_PROFILE_READY", python.code)
        node = profiles.pop("node_pnpm")
        self.assertEqual("READY", node.status)
        self.assertTrue(node.execution_supported)
        self.assertEqual("NODE_PROFILE_READY", node.code)

    def test_missing_directory_has_no_profiles(self) -> None:
        self.assertEqual((), ProjectProfileDetector().detect(Path("Z:/does-not-exist/repopilot")))

    def test_gradle_build_descriptor_satisfies_java_build_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".git").mkdir()
            (root / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")

            result = PreflightInspector().inspect(root)

        self.assertTrue(result.has_gradle_build)
        self.assertFalse(result.has_pom_xml)
        self.assertNotIn("No supported Java build descriptor was found.", result.errors)

    def test_pytest_descriptor_satisfies_python_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".git").mkdir()
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

            result = PreflightInspector().inspect(root)

        self.assertTrue(result.has_pytest_project)
        self.assertNotIn("No supported Java, Python or Node build/test descriptor was found.", result.errors)

    def test_node_descriptor_satisfies_node_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".git").mkdir()
            (root / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")

            result = PreflightInspector().inspect(root)

        self.assertTrue(result.has_node_project)
        self.assertNotIn("No supported Java, Python or Node build/test descriptor was found.", result.errors)

    def test_aggregate_repository_with_nested_source_can_be_researched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".git").mkdir()
            source = root / "services" / "orders" / "src" / "main" / "java" / "OrderService.java"
            source.parent.mkdir(parents=True)
            source.write_text("class OrderService {}\n", encoding="utf-8")

            result = PreflightInspector().inspect(root)

        self.assertTrue(result.ready)
        self.assertTrue(result.has_supported_content)
        self.assertFalse(result.has_pom_xml)
        self.assertIn(
            "No top-level build/test descriptor was found; read-only research can continue, but verification must target a detected module.",
            result.warnings,
        )

    def test_non_git_source_directory_only_reports_git_baseline_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "src" / "main" / "java" / "OrderService.java"
            source.parent.mkdir(parents=True)
            source.write_text("class OrderService {}\n", encoding="utf-8")

            result = PreflightInspector().inspect(root)

        self.assertFalse(result.ready)
        self.assertTrue(result.has_supported_content)
        self.assertEqual(("Repository is not a Git working tree.",), result.errors)

    def test_runtime_inspector_blocks_missing_pnpm_without_exposing_binary_path(self) -> None:
        inspector = ProfileRuntimeInspector(command_lookup=lambda _command: None, module_lookup=lambda _module: None)

        result = inspector.inspect("node_pnpm", Path.cwd())

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("PNPM_RUNTIME_UNAVAILABLE", result.code)
        self.assertEqual("pnpm", result.command)
        self.assertNotIn("C:\\", str(result.to_dict()))

    def test_runtime_inspector_reports_npm_without_returning_lookup_path(self) -> None:
        inspector = ProfileRuntimeInspector(command_lookup=lambda _command: "C:\\tools\\npm.cmd", module_lookup=lambda _module: None)

        result = inspector.inspect("node_npm", Path.cwd())

        self.assertEqual("READY", result.status)
        self.assertEqual("NPM_RUNTIME_READY", result.code)
        self.assertNotIn("C:\\tools", str(result.to_dict()))

    def test_runtime_inspector_reports_corepack_backed_pnpm_without_exposing_path(self) -> None:
        inspector = ProfileRuntimeInspector(
            command_lookup=lambda command: "C:\\tools\\corepack.cmd" if command == "corepack.cmd" else None,
            module_lookup=lambda _module: None,
        )

        result = inspector.inspect("node_pnpm", Path.cwd())

        self.assertEqual("READY", result.status)
        self.assertEqual("PNPM_COREPACK_READY", result.code)
        self.assertEqual("corepack pnpm", result.command)
        self.assertNotIn("C:\\tools", str(result.to_dict()))

    def test_project_diagnostics_projects_profile_runtime_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "node-project"
            repository.mkdir()
            (repository / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                project = registry.add(repository, "Node 项目")
                diagnosis = diagnose_project(
                    project,
                    runtime_inspector=ProfileRuntimeInspector(
                        command_lookup=lambda _command: None,
                        module_lookup=lambda _module: None,
                    ),
                )
            finally:
                registry.close()

        runtime = diagnosis["profiles"]["node_npm"]["runtime"]
        self.assertEqual("BLOCKED", runtime["status"])
        self.assertEqual("NPM_RUNTIME_UNAVAILABLE", runtime["code"])
        self.assertEqual("npm", runtime["command"])


if __name__ == "__main__":
    unittest.main()
