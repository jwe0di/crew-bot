from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.ai_approvals import approval_recipient_ids, batch_action_status


class ApprovalRecipientTests(unittest.TestCase):
    def test_includes_guild_owner_bot_owner_and_configured_role_members(self) -> None:
        role = SimpleNamespace(members=[
            SimpleNamespace(id=300, bot=False),
            SimpleNamespace(id=400, bot=False),
            SimpleNamespace(id=500, bot=True),
            SimpleNamespace(id=100, bot=False),
        ])
        guild = SimpleNamespace(
            owner_id=100,
            get_role=lambda role_id: role if role_id == 77 else None,
        )

        self.assertEqual(
            approval_recipient_ids(guild, 77, 200),
            {100, 200, 300, 400},
        )

    def test_owners_remain_approvers_when_no_role_is_configured(self) -> None:
        guild = SimpleNamespace(owner_id=100, get_role=lambda _role_id: None)
        self.assertEqual(approval_recipient_ids(guild, None, 200), {100, 200})

    def test_missing_role_does_not_drop_owner_approvers(self) -> None:
        guild = SimpleNamespace(owner_id=100, get_role=lambda _role_id: None)
        self.assertEqual(approval_recipient_ids(guild, 999, None), {100})


class BatchActionStatusTests(unittest.TestCase):
    def test_all_successful_actions_are_completed(self) -> None:
        self.assertEqual(batch_action_status(3, 3), "completed")

    def test_mixed_batch_is_partial(self) -> None:
        self.assertEqual(batch_action_status(2, 3), "partial")

    def test_no_successful_actions_are_failed(self) -> None:
        self.assertEqual(batch_action_status(0, 3), "failed")

    def test_empty_batch_is_failed(self) -> None:
        self.assertEqual(batch_action_status(0, 0), "failed")


if __name__ == "__main__":
    unittest.main()
