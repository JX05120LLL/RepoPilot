from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from repopilot_guard import __version__
from repopilot_guard.evaluation import (
    BaselineValidator,
    EvaluationCatalog,
    EvaluationMatrixRunner,
    EvaluationProviderSummary,
    EvaluationRunner,
    FixtureBuilder,
)
from repopilot_guard.cli import main
from repopilot_guard.recipes import GradleExecutionResult, MavenExecutionResult, NodeExecutionResult, PytestExecutionResult
from repopilot_guard.policy import GradleRecipeName, NodeRecipeName, PytestRecipeName


class EvaluationCatalogTests(unittest.TestCase):
    def test_baseline_validator_dispatches_gradle_and_pytest_and_projects_generic_evidence(self) -> None:
        class SingleTaskCatalog:
            def __init__(self, task: object, catalog_path: Path) -> None:
                self._task = task
                self.catalog_path = catalog_path

            def load(self) -> tuple[object, ...]:
                return (self._task,)

        class FakeGradleRunner:
            def run(self, repository: Path, recipe: object, permission: object, test_class: str | None = None) -> GradleExecutionResult:
                self.repository = repository
                return GradleExecutionResult("PASSED", "GRADLE_SUCCEEDED", recipe, ("gradle", "test"), 0, 8, "ok", "", ("build/test-results/test/TEST-demo.xml",))

        class FakePytestRunner:
            def run(self, repository: Path, recipe: object, permission: object, test_class: str | None = None) -> PytestExecutionResult:
                self.repository = repository
                return PytestExecutionResult("PASSED", "PYTEST_SUCCEEDED", recipe, ("python", "-m", "pytest", "-q"), 0, 6, "1 passed", "", ())

        class FakeNodeRunner:
            def run(self, repository: Path, recipe: object, permission: object, test_class: str | None = None) -> NodeExecutionResult:
                self.repository = repository
                return NodeExecutionResult("PASSED", "NODE_TEST_SUCCEEDED", recipe, ("npm", "run", "test", "--silent"), 0, 5, "ok", "", ())

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            for task_id in ("G01", "P01", "N01"):
                repository = fixtures / task_id / "repository"
                repository.mkdir(parents=True)
                FixtureBuilder._write_base_project(repository)
                FixtureBuilder._git(repository, "init", "-b", "main")
                FixtureBuilder._git(repository, "config", "user.name", "RepoPilot Test")
                FixtureBuilder._git(repository, "config", "user.email", "test@example.invalid")
                FixtureBuilder._git(repository, "add", ".")
                FixtureBuilder._git(repository, "commit", "-m", "fixture")

            gradle_task = SimpleNamespace(task_id="G01", category="gradle", recipe="gradle_test", target_test_class=None, baseline_status="PASSED")
            gradle_results = BaselineValidator(
                SingleTaskCatalog(gradle_task, catalog_path),
                gradle_runner=FakeGradleRunner(),
            ).validate(fixtures, root / "gradle-results")
            pytest_task = SimpleNamespace(task_id="P01", category="python", recipe="pytest_test", target_test_class=None, baseline_status="PASSED")
            pytest_results = BaselineValidator(
                SingleTaskCatalog(pytest_task, catalog_path),
                pytest_runner=FakePytestRunner(),
            ).validate(fixtures, root / "pytest-results")
            node_task = SimpleNamespace(task_id="N01", category="node", recipe="npm_test", target_test_class=None, baseline_status="PASSED")
            node_results = BaselineValidator(
                SingleTaskCatalog(node_task, catalog_path),
                node_runner=FakeNodeRunner(),
            ).validate(fixtures, root / "node-results")

        self.assertEqual("gradle", gradle_results[0].build_system)
        self.assertEqual("gradle_test_results", gradle_results[0].report_kind)
        self.assertEqual(("build/test-results/test/TEST-demo.xml",), gradle_results[0].verification_reports)
        self.assertEqual("pytest", pytest_results[0].build_system)
        self.assertEqual("pytest_output", pytest_results[0].report_kind)
        self.assertEqual((), pytest_results[0].verification_reports)
        self.assertEqual("node", node_results[0].build_system)
        self.assertEqual("node_test_output", node_results[0].report_kind)

    def test_catalog_has_fifteen_unique_tasks_and_safe_assertions(self) -> None:
        tasks = json.loads((Path(__file__).parents[1] / "evaluation" / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(15, len(tasks))
        self.assertEqual(15, len({item["id"] for item in tasks}))
        self.assertTrue({"secret", "path_escape", "prompt_injection", "approval"}.issubset({item["category"] for item in tasks}))
        self.assertTrue(all(item["recipe"] in {"compile", "test", "targeted_test"} for item in tasks))
        self.assertEqual(
            {"J01", "J02", "J03", "J04", "J06", "V01", "V02"},
            {item["id"] for item in tasks if item.get("baseline_status") == "FAILED"},
        )
        self.assertEqual("com.repopilot.demo.OrderRequestValidationTest", tasks[3]["target_test_class"])

    def test_profile_catalog_prepares_gradle_pytest_and_node_fixtures_with_normal_failure_safety_and_resume_cases(self) -> None:
        catalog_path = Path(__file__).parents[1] / "evaluation" / "profiles" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "profile-fixtures"
            results = FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(output)
            manifest = json.loads((output / "fixtures.json").read_text(encoding="utf-8"))
            self.assertTrue((output / "G01/repository/build.gradle.kts").is_file())
            gradle_build = (output / "G01/repository/build.gradle.kts").read_text(encoding="utf-8")
            self.assertIn("org.junit.platform:junit-platform-launcher", gradle_build)
            self.assertTrue((output / "G01/repository/src/test/java/com/repopilot/gradle/OrderServiceTest.java").is_file())
            self.assertTrue((output / "P01/repository/pyproject.toml").is_file())
            self.assertTrue((output / "P01/repository/tests/test_service.py").is_file())
            self.assertTrue((output / "N01/repository/package.json").is_file())
            self.assertTrue((output / "N01/repository/package-lock.json").is_file())
            npm_package = json.loads((output / "N02/repository/package.json").read_text(encoding="utf-8"))
            self.assertEqual("node --test", npm_package["scripts"]["test"])
            self.assertTrue((output / "N02/repository/test/failure.test.js").is_file())
            self.assertTrue((output / "Q01/repository/package.json").is_file())
            self.assertTrue((output / "Q01/repository/pnpm-lock.yaml").is_file())
            self.assertTrue((output / "Q02/repository/test/failure.test.js").is_file())
            self.assertTrue((output / "G03/repository/.env").is_file())

        self.assertEqual(16, len(results))
        self.assertEqual(16, manifest["fixture_count"])
        self.assertEqual({"java_gradle", "python_pytest", "node_npm", "node_pnpm"}, {result.profile for result in results})
        self.assertTrue(all(result.expected_paths_present for result in results))

    def test_prepares_fifteen_independent_git_fixtures_and_machine_readable_manifest(self) -> None:
        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "fixtures"
            results = FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(output)
            manifest = json.loads((output / "fixtures.json").read_text(encoding="utf-8"))
            self.assertEqual(15, len(results))
            self.assertEqual(15, manifest["fixture_count"])
            self.assertTrue(all(item.expected_paths_present for item in results))
            self.assertTrue(all((item.repository / ".git").exists() and len(item.baseline_commit) == 40 for item in results))
            self.assertTrue((output / "fixtures.csv").is_file())
            j01_repository = output / "J01" / "repository"
            self.assertIn("junit-jupiter", (j01_repository / "pom.xml").read_text(encoding="utf-8"))
            self.assertIn("maven-compiler-plugin", (j01_repository / "pom.xml").read_text(encoding="utf-8"))
            self.assertIn(
                "assertThrows(IllegalArgumentException.class",
                (j01_repository / "src/test/java/com/repopilot/demo/OrderControllerTest.java").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "assertThrows(SecurityException.class",
                (output / "J02/repository/src/test/java/com/repopilot/demo/OrderServiceTenantTest.java").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "limit #{offset}, #{pagesize}",
                (output / "J03/repository/src/test/java/com/repopilot/demo/OrderMapperXmlTest.java").read_text(encoding="utf-8"),
            )
            self.assertTrue((output / "V01/repository/src/test/java/com/repopilot/demo/UnrelatedFailureTest.java").is_file())
            self.assertTrue((output / "V02/repository/src/test/java/com/repopilot/demo/OrderControllerTest.java").is_file())
            self.assertTrue((output / "S02/repository/.env").is_file())
            self.assertEqual(
                "",
                FixtureBuilder._git(output / "S02/repository", "status", "--porcelain"),
            )
            self.assertIn(
                "getAnnotation(NotBlank.class)",
                (output / "J04/repository/src/test/java/com/repopilot/demo/OrderRequestValidationTest.java").read_text(encoding="utf-8"),
            )
            self.assertIn("<maven.compiler.release>99</maven.compiler.release>", (output / "J06/repository/pom.xml").read_text(encoding="utf-8"))
            self.assertEqual(2, manifest["schema_version"])
            with self.assertRaisesRegex(ValueError, "FIXTURE_ALREADY_EXISTS"):
                FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(output)

    def test_baseline_validator_runs_in_clone_and_writes_evidence_without_changing_fixture(self) -> None:
        class FakeFailedMavenRunner:
            def run(self, repository: Path, recipe: object, permission: object, test_class: str | None = None) -> MavenExecutionResult:
                self.repository = repository
                self.test_class = test_class
                return MavenExecutionResult(
                    status="FAILED",
                    code="MAVEN_FAILED",
                    recipe=recipe,
                    argv=("mvn", "-q", "test"),
                    exit_code=1,
                    duration_ms=12,
                    stdout_summary="1 test failed",
                    stderr_summary="",
                    surefire_reports=("target/surefire-reports/TEST-demo.xml",),
                )

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        fake_runner = FakeFailedMavenRunner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(fixtures)
            source = fixtures / "J01/repository"
            status_before = FixtureBuilder._git(source, "status", "--porcelain")
            results = BaselineValidator(EvaluationCatalog(catalog_path), fake_runner).validate(
                fixtures,
                root / "baseline-results",
                task_ids={"J01"},
            )
            report = json.loads((root / "baseline-results/baseline-report.json").read_text(encoding="utf-8"))

            self.assertEqual(1, len(results))
            self.assertTrue(results[0].matched_expectation)
            self.assertTrue(results[0].source_unchanged)
            self.assertNotEqual(source.resolve(), fake_runner.repository.resolve())
            self.assertEqual(status_before, FixtureBuilder._git(source, "status", "--porcelain"))
            self.assertTrue(report["all_matched"])
            self.assertEqual(2, report["schema_version"])
            self.assertEqual(["J01"], report["metadata"]["selected_task_ids"])
            self.assertEqual(64, len(report["metadata"]["catalog_sha256"]))
            self.assertEqual(64, len(report["metadata"]["fixture_set_sha256"]))
            self.assertEqual(__version__, report["metadata"]["repopilot"]["version"])
            self.assertIn(report["metadata"]["repopilot"]["source_tree_state"], {"CLEAN", "DIRTY", "UNAVAILABLE"})
            self.assertEqual("J01/workspace", report["results"][0]["validation_workspace"])
            self.assertEqual("J01/maven-stdout.txt", report["results"][0]["stdout_log"])
            self.assertNotIn(str(root), json.dumps(report, ensure_ascii=False))
            self.assertTrue((root / "baseline-results/baseline-report.csv").is_file())
            self.assertTrue((root / "baseline-results/baseline-report.md").is_file())
            self.assertEqual("1 test failed", (root / "baseline-results/J01/maven-stdout.txt").read_text(encoding="utf-8"))

    def test_cli_prepares_empty_output_and_refuses_to_overwrite_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "fixtures"
            stream = StringIO()
            with redirect_stdout(stream):
                exit_code = main(["evaluate", "prepare", "--output", str(output)])
            self.assertEqual(0, exit_code)
            self.assertEqual("READY", json.loads(stream.getvalue())["status"])
            with redirect_stdout(StringIO()):
                blocked = main(["evaluate", "prepare", "--output", str(output)])
            self.assertEqual(2, blocked)

    def test_runner_uses_actual_graph_result_and_writes_three_report_formats(self) -> None:
        class FakeGraphRunner:
            def __init__(self) -> None:
                self.resumes: dict[str, int] = {}

            def run(self, request: object, thread_id: str, permission: object) -> object:
                self.request = request
                self.resumes[thread_id] = 0
                return SimpleNamespace(status="WAITING_APPROVAL", verdict=None, pending_approval=True, state={"git_diff": None, "verification_result": None})

            def resume(self, thread_id: str, approved: bool) -> object:
                self.resumes[thread_id] += 1
                if not approved:
                    return SimpleNamespace(status="BLOCKED", verdict="BLOCKED", pending_approval=False, state={"git_diff": None, "verification_result": None, "error_summary": "已拒绝"})
                if self.resumes[thread_id] < 2:
                    return SimpleNamespace(status="WAITING_APPROVAL", verdict=None, pending_approval=True, state={"git_diff": None, "verification_result": None})
                return SimpleNamespace(
                    status="REPORT",
                    verdict="PASSED",
                    pending_approval=False,
                    state={
                        "git_diff": "diff --git",
                        "patch_result": {"paths": ["src/main/java/com/repopilot/demo/web/OrderController.java"]},
                        "verification_result": {
                            "status": "PASSED",
                            "code": "MAVEN_SUCCEEDED",
                            "recipe": "test",
                            "argv": [r"D:\private-tools\apache-maven\bin\mvn.cmd", "-q", "test"],
                            "exit_code": 0,
                            "duration_ms": 120,
                            "surefire_reports": ["target/surefire-reports/TEST-OrderControllerTest.xml"],
                        },
                    },
                )

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(fixtures)
            fake_graph = FakeGraphRunner()
            provider = EvaluationProviderSummary(
                chat_model="deepseek-chat",
                embedding_model="sk-secretToken123",
                embedding_dimensions=1024,
            )
            results = EvaluationRunner(EvaluationCatalog(catalog_path), fake_graph, provider).run(
                fixtures,
                root / "results",
                task_ids={"J01"},
                approval="auto",
            )
            report_path = root / "results" / "evaluation-report.json"
            report_text = report_path.read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(1, len(results))
            self.assertEqual("PASSED", results[0].actual_status)
            self.assertTrue(results[0].matched_expectation)
            self.assertEqual(2, results[0].approval_count)
            self.assertTrue(results[0].source_unchanged)
            self.assertTrue(results[0].scope_valid)
            self.assertTrue(results[0].verification_contract_valid)
            self.assertEqual("test", results[0].verification_recipe)
            self.assertEqual(0, results[0].verification_exit_code)
            self.assertEqual(("target/surefire-reports/TEST-OrderControllerTest.xml",), results[0].verification_surefire_reports)
            self.assertEqual(("src/main/java/com/repopilot/demo/web/OrderController.java",), results[0].changed_paths)
            self.assertEqual("test", fake_graph.request.verification_contract.recipe)
            self.assertEqual(2, report["schema_version"])
            self.assertEqual("deepseek-chat", report["metadata"]["provider"]["chat_model"])
            self.assertEqual("INVALID_IDENTIFIER_REDACTED", report["metadata"]["provider"]["embedding_model"])
            self.assertEqual(1024, report["metadata"]["provider"]["embedding_dimensions"])
            self.assertEqual(["J01"], report["metadata"]["selected_task_ids"])
            self.assertEqual("mvn.cmd", report["results"][0]["verification_argv"][0])
            self.assertNotIn("private-tools", report_text)
            self.assertNotIn("sk-secretToken123", report_text)
            self.assertTrue(report_path.is_file())
            self.assertTrue((root / "results" / "evaluation-report.csv").is_file())
            self.assertTrue((root / "results" / "evaluation-report.md").is_file())

    def test_matrix_runner_keeps_each_run_evidence_and_aggregates_stability(self) -> None:
        class StableGraphRunner:
            def run(self, _request: object, _thread_id: str, _permission: object) -> object:
                return SimpleNamespace(
                    status="REPORT",
                    verdict="PASSED",
                    pending_approval=False,
                    state={
                        "git_diff": "diff --git",
                        "patch_result": {"paths": ["src/main/java/com/repopilot/demo/web/OrderController.java"]},
                        "verification_result": {
                            "status": "PASSED",
                            "code": "MAVEN_SUCCEEDED",
                            "recipe": "test",
                            "argv": ["mvn", "-q", "test"],
                            "exit_code": 0,
                            "duration_ms": 50,
                            "surefire_reports": [],
                        },
                    },
                )

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(fixtures)
            matrix = EvaluationMatrixRunner(
                EvaluationCatalog(catalog_path),
                StableGraphRunner,
                EvaluationProviderSummary(chat_model="safe-model", embedding_model="sk-secret-token"),
            ).run(
                fixtures,
                root / "matrix",
                repetitions=3,
                task_ids={"J01"},
            )
            report_text = (root / "matrix" / "evaluation-matrix.json").read_text(encoding="utf-8")
            run_report_exists = (root / "matrix" / "run-001" / "evaluation-report.json").is_file()
            csv_exists = (root / "matrix" / "evaluation-matrix.csv").is_file()
            markdown_exists = (root / "matrix" / "evaluation-matrix.md").is_file()

        self.assertTrue(matrix.all_matched)
        self.assertEqual(3, matrix.repetitions)
        self.assertEqual(3, len(matrix.runs))
        self.assertEqual(("PASSED", 1), matrix.runs[0].status_counts[0])
        self.assertEqual("J01", matrix.tasks[0].task_id)
        self.assertEqual(3, matrix.tasks[0].matched_expectations)
        self.assertEqual(1.0, matrix.tasks[0].match_rate)
        self.assertNotIn("sk-secret-token", report_text)
        self.assertTrue(run_report_exists)
        self.assertTrue(csv_exists)
        self.assertTrue(markdown_exists)

    def test_matrix_runner_rejects_single_repetition_before_writing_evidence(self) -> None:
        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "EVALUATION_MATRIX_REPETITIONS_INVALID"):
                EvaluationMatrixRunner(EvaluationCatalog(catalog_path), object).run(
                    root / "fixtures",
                    root / "matrix",
                    repetitions=1,
                    task_ids={"J01"},
                )

        self.assertFalse((root / "matrix").exists())

    def test_matrix_cli_rejects_invalid_repetitions_before_loading_model_configuration(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            exit_code = main(
                [
                    "evaluate",
                    "matrix",
                    "--fixtures",
                    "C:/fixtures",
                    "--output",
                    "C:/matrix",
                    "--task-id",
                    "J01",
                    "--repetitions",
                    "1",
                ]
            )

        payload = json.loads(stream.getvalue())
        self.assertEqual(2, exit_code)
        self.assertEqual("EVALUATION_MATRIX_REPETITIONS_INVALID", payload["code"])

    def test_runner_rejects_green_maven_result_when_patch_is_outside_expected_scope(self) -> None:
        class OutOfScopeGraphRunner:
            def run(self, request: object, thread_id: str, permission: object) -> object:
                return SimpleNamespace(
                    status="REPORT",
                    verdict="PASSED",
                    pending_approval=False,
                    state={
                        "git_diff": "diff --git",
                        "patch_result": {"paths": ["src/test/java/com/repopilot/demo/OrderControllerTest.java"]},
                        "verification_result": {
                            "status": "PASSED",
                            "code": "MAVEN_SUCCEEDED",
                            "recipe": "test",
                            "argv": ["mvn", "-q", "test"],
                            "exit_code": 0,
                            "duration_ms": 120,
                            "surefire_reports": [],
                        },
                    },
                )

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(fixtures)
            result = EvaluationRunner(EvaluationCatalog(catalog_path), OutOfScopeGraphRunner()).run(
                fixtures,
                root / "results",
                task_ids={"J01"},
            )[0]

        self.assertEqual("FAILED", result.actual_status)
        self.assertFalse(result.scope_valid)
        self.assertFalse(result.matched_expectation)

    def test_runner_rejects_green_maven_result_when_recipe_does_not_match_task_contract(self) -> None:
        class WrongRecipeGraphRunner:
            def run(self, request: object, thread_id: str, permission: object) -> object:
                return SimpleNamespace(
                    status="REPORT",
                    verdict="PASSED",
                    pending_approval=False,
                    state={
                        "git_diff": "diff --git",
                        "patch_result": {"paths": ["src/main/java/com/repopilot/demo/web/OrderController.java"]},
                        "verification_result": {
                            "status": "PASSED",
                            "code": "MAVEN_SUCCEEDED",
                            "recipe": "compile",
                            "argv": ["mvn", "-q", "-DskipTests", "compile"],
                            "exit_code": 0,
                            "duration_ms": 90,
                            "surefire_reports": [],
                        },
                    },
                )

        catalog_path = Path(__file__).parents[1] / "evaluation" / "tasks.json"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixtures = root / "fixtures"
            FixtureBuilder(EvaluationCatalog(catalog_path)).prepare_all(fixtures)
            result = EvaluationRunner(EvaluationCatalog(catalog_path), WrongRecipeGraphRunner()).run(
                fixtures,
                root / "results",
                task_ids={"J01"},
            )[0]

        self.assertEqual("FAILED", result.actual_status)
        self.assertFalse(result.verification_contract_valid)
        self.assertFalse(result.matched_expectation)
