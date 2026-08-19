from __future__ import annotations

import unittest

from repopilot_guard.models import TaskState
from repopilot_guard.state_machine import InvalidTransition, TaskStateMachine


class TaskStateMachineTests(unittest.TestCase):
    def test_happy_path_to_report(self) -> None:
        state_machine = TaskStateMachine()

        for state in (
            TaskState.UNDERSTAND,
            TaskState.LOCATE,
            TaskState.PLAN,
            TaskState.REVIEW,
            TaskState.REPORT,
        ):
            state_machine.transition(state)

        self.assertEqual(TaskState.REPORT, state_machine.current)
        self.assertEqual(6, len(state_machine.history))

    def test_rejects_skipped_transition(self) -> None:
        state_machine = TaskStateMachine()

        with self.assertRaises(InvalidTransition):
            state_machine.transition(TaskState.PATCH)

