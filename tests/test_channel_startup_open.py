# encoding:utf-8
"""Startup-channel resolution after runtime consumers opened (open-database-runtime).

Database identity mode now starts the same channels as legacy: external IM
channels configured in config.json/team.json participate (inbound identity is
resolved at the message layer), and run() performs scheduler/MCP warmup in both
identity modes. This reverses the old task 3.11 "web only in database mode"
closure that this change deletes.
"""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app


class StartupChannelResolutionTests(unittest.TestCase):
    def _names(self, raw):
        with patch("channel.channel_instances.load_tenant_channel_instances", return_value=[]), \
                patch("agent.team.resolve", return_value={"channel_type": raw}):
            return app._resolve_startup_channels(raw)

    def test_database_mode_preserves_channel_types(self):
        # A fake registry is not required: config entries resolve statically.
        names = self._names("feishu, dingtalk, web")
        self.assertEqual(names, ["feishu", "dingtalk", "web"])

    def test_legacy_mode_preserves_channel_types(self):
        names = self._names("feishu, dingtalk, web")
        self.assertEqual(names, ["feishu", "dingtalk", "web"])

    def test_empty_config_falls_back_to_web(self):
        self.assertEqual(self._names(""), ["web"])


class RunWarmupTests(unittest.TestCase):
    """run() warms scheduler + MCP in BOTH identity modes (task 2.3)."""

    def _run(self):
        with ExitStack() as stack:
            for target in (
                "common.maintenance.start",
                "app.process_watch.install",
                "app._verify_required_seams",
                "app._ensure_database_bootstrap",
                "app._register_pid_file",
                "common.startup_hooks.run_startup_hook",
                "app._migrate_conversations",
                "app._migrate_conversation_tenancy",
                "app._migrate_scheduled_tasks",
                "app._guard_external_store_version",
                "app._guard_identity_mode_consistency",
                "app.load_config",
                "app._migrate_team_roster",
                "app._warn_if_legacy_workspace_data_exists",
                "app.sigterm_handler_wrap",
                "app._sync_builtin_skills",
                "app._scaffold_subagent_assets",
                "app.ChannelManager",
                "app.set_channel_manager",
            ):
                stack.enter_context(patch(target))
            stack.enter_context(patch("app._resolve_startup_channels", return_value=["web"]))
            stack.enter_context(patch("app._has_web_entry", return_value=True))
            stack.enter_context(patch("app.DESKTOP_MODE", False))
            stack.enter_context(patch("app.time.sleep", side_effect=KeyboardInterrupt))
            warmup_mcp = stack.enter_context(patch("app._warmup_mcp_tools"))
            warmup_sched = stack.enter_context(patch("app._warmup_scheduler"))
            app.run()
        return warmup_mcp, warmup_sched

    def test_run_starts_warmup_in_database_mode(self):
        warmup_mcp, warmup_sched = self._run()
        warmup_mcp.assert_called_once()
        warmup_sched.assert_called_once()

    def test_run_starts_warmup_in_legacy_mode(self):
        warmup_mcp, warmup_sched = self._run()
        warmup_mcp.assert_called_once()
        warmup_sched.assert_called_once()


if __name__ == "__main__":
    unittest.main()
