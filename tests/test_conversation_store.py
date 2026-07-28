from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.conversation_store import ConversationStore


class ConversationStoreTests(unittest.TestCase):
    def test_unassigned_conversation_can_be_renamed_archived_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ConversationStore(Path(temporary_directory) / "state.sqlite")
            try:
                conversation = store.create(
                    project_id=None,
                    display_title="排查订单接口",
                    mode="goal",
                )
                self.assertIsNone(conversation.project_id)
                self.assertEqual("goal", conversation.mode)

                renamed = store.update(
                    conversation.conversation_id,
                    display_title="排查订单接口超时",
                    mode="plan",
                )
                self.assertEqual("排查订单接口超时", renamed.display_title)
                self.assertEqual("plan", renamed.mode)

                archived = store.archive(conversation.conversation_id)
                self.assertIsNotNone(archived.archived_at)
                self.assertEqual((), store.list())
                self.assertEqual(1, len(store.list(include_archived=True)))

                restored = store.restore(conversation.conversation_id)
                self.assertIsNone(restored.archived_at)
                self.assertEqual(conversation.conversation_id, store.list()[0].conversation_id)
            finally:
                store.close()

    def test_update_requires_a_title_when_the_field_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ConversationStore(Path(temporary_directory) / "state.sqlite")
            try:
                conversation = store.create(project_id=None, display_title=None, mode="goal")
                with self.assertRaisesRegex(ValueError, "CONVERSATION_TITLE_INVALID"):
                    store.update(conversation.conversation_id, display_title="   ")
                with self.assertRaisesRegex(ValueError, "CONVERSATION_MODE_INVALID"):
                    store.update(conversation.conversation_id, mode="change")
            finally:
                store.close()

    def test_title_redacts_inline_credentials_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ConversationStore(Path(temporary_directory) / "state.sqlite")
            try:
                record = store.create(
                    project_id=None,
                    display_title="排查 api_key=should-not-be-stored",
                    mode="goal",
                )
                self.assertEqual("排查 api_key=[REDACTED]", record.display_title)
                reopened = store.get(record.conversation_id)
                self.assertNotIn("should-not-be-stored", reopened.display_title)
            finally:
                store.close()
