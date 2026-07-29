from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import repopilot_guard.processes as process_options
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import MavenRecipeName
from repopilot_guard.recipes import MavenRecipeRunner, RecipeCommand
from repopilot_guard.workspace import GitClient


class HiddenProcessTests(unittest.TestCase):
    def test_windows_options_hide_console_window(self) -> None:
        startup_info = SimpleNamespace(dwFlags=0, wShowWindow=None)
        fake_subprocess = SimpleNamespace(
            STARTUPINFO=lambda: startup_info,
            STARTF_USESHOWWINDOW=1,
            SW_HIDE=0,
            CREATE_NO_WINDOW=0x0800_0000,
        )
        with (
            patch.object(process_options, "os", SimpleNamespace(name="nt")),
            patch.object(process_options, "subprocess", fake_subprocess),
        ):
            options = process_options.hidden_process_kwargs()

        self.assertEqual(0x0800_0000, options["creationflags"])
        self.assertIs(startup_info, options["startupinfo"])
        self.assertEqual(1, startup_info.dwFlags)
        self.assertEqual(0, startup_info.wShowWindow)

    def test_non_windows_options_do_not_change_process_semantics(self) -> None:
        with patch.object(process_options, "os", SimpleNamespace(name="posix")):
            self.assertEqual({}, process_options.hidden_process_kwargs())

    def test_git_client_applies_hidden_process_options(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with (
            patch("repopilot_guard.workspace.hidden_process_kwargs", return_value={"creationflags": 123}),
            patch("repopilot_guard.workspace.subprocess.run", return_value=completed) as run,
        ):
            output = GitClient().run(Path("."), "status", "--porcelain=v1")

        self.assertEqual("ok\n", output)
        self.assertEqual(123, run.call_args.kwargs["creationflags"])

    def test_maven_runner_applies_hidden_process_options(self) -> None:
        process = SimpleNamespace(returncode=0)
        process.communicate = lambda timeout: ("", "")
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            catalog = _FixedRecipeCatalog(workspace)
            with (
                patch("repopilot_guard.recipes.hidden_process_kwargs", return_value={"creationflags": 456}),
                patch("repopilot_guard.recipes.subprocess.Popen", return_value=process) as popen,
            ):
                result = MavenRecipeRunner(catalog).run(
                    workspace,
                    MavenRecipeName.TEST,
                    PermissionGrant.safe(),
                )

        self.assertEqual("PASSED", result.status)
        self.assertEqual(456, popen.call_args.kwargs["creationflags"])


class _FixedRecipeCatalog:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build(
        self,
        _repository: Path,
        recipe: MavenRecipeName,
        _test_class: str | None,
        _permission: PermissionGrant,
    ) -> RecipeCommand:
        return RecipeCommand(recipe, ("mvn.cmd", "-q", "test"), self._workspace)


if __name__ == "__main__":
    unittest.main()
