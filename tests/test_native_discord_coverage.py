from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_SOURCE = (ROOT / "bot.py").read_text(encoding="utf-8")
DASHBOARD_SOURCE = (ROOT / "extensions" / "ai_dashboard.py").read_text(encoding="utf-8")
EXECUTOR = BOT_SOURCE.split("async def _exec_ai_tool", 1)[1].split("async def _run_ai", 1)[0]


class NativeDiscordCoverageTests(unittest.TestCase):
    REQUIRED_TOOLS = {
        "create_forum_channel",
        "create_stage_channel",
        "list_discord_scheduled_events",
        "create_discord_scheduled_event",
        "edit_discord_scheduled_event",
        "delete_discord_scheduled_event",
        "list_automod_rules",
        "create_automod_keyword_rule",
        "edit_automod_rule",
        "delete_automod_rule",
        "get_railway_runtime_logs",
    }

    def test_every_added_tool_is_registered_and_dispatched(self) -> None:
        for name in self.REQUIRED_TOOLS:
            with self.subTest(tool=name):
                self.assertRegex(BOT_SOURCE, rf'_ai_tool\\("{re.escape(name)}"')
                self.assertIn(f'"{name}"', EXECUTOR)

    def test_high_impact_event_and_automod_changes_require_approval(self) -> None:
        approval_block = BOT_SOURCE.split("_AI_OWNER_APPROVAL_TOOLS = {", 1)[1].split("}", 1)[0]
        for name in {
            "delete_discord_scheduled_event",
            "create_automod_keyword_rule",
            "edit_automod_rule",
            "delete_automod_rule",
        }:
            with self.subTest(tool=name):
                self.assertIn(f'"{name}"', approval_block)

    def test_dashboard_is_loaded_and_persistent(self) -> None:
        self.assertIn('load_plugin("extensions.ai_dashboard")', BOT_SOURCE)
        self.assertIn("bot.add_view(DashboardView(bot, timeout=None))", DASHBOARD_SOURCE)
        self.assertIn('name="ai-dashboard"', DASHBOARD_SOURCE)
        self.assertIn('name="publish-ai-dashboard"', DASHBOARD_SOURCE)
        self.assertIn("self.bot.ai_run", DASHBOARD_SOURCE)

    def test_log_sources_are_explicitly_separated(self) -> None:
        self.assertIn("not Railway runtime logs", BOT_SOURCE)
        self.assertIn("Railway deployment runtime logs", BOT_SOURCE)
        self.assertIn("Discord Audit Log is separate from Railway runtime logs", DASHBOARD_SOURCE)


if __name__ == "__main__":
    unittest.main()
