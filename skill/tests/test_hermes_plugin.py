#!/usr/bin/env python3
"""Integration tests for the paired Hermes native personal-memo plugin."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"
PACKAGE = "personal_memo_plugin_test"
SPEC = importlib.util.spec_from_file_location(
    PACKAGE, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
)
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[PACKAGE] = plugin
SPEC.loader.exec_module(plugin)


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, tuple[str, dict, object]] = {}
        self.commands: dict[str, object] = {}

    def register_tool(self, *, name, toolset, schema, handler, description):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description):
        self.commands[name] = handler


class HermesPluginTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HERMES_HOME")
        self.home = Path(self.temp.name)
        destination = self.home / "lib" / "personal_memo_core"
        shutil.copytree(ROOT / "core" / "personal_memo_core", destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.environ["HERMES_HOME"] = str(self.home)
        plugin.store.close_thread_store()

    def tearDown(self) -> None:
        plugin.store.close_thread_store()
        if self.old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old_home
        self.temp.cleanup()

    def invoke(self, handler, payload):
        return json.loads(handler(payload))

    def test_registers_normal_and_admin_toolsets(self):
        context = FakeContext()
        plugin.register(context)
        self.assertIn("memo_add", context.tools)
        self.assertIn("memo_admin", context.tools)
        self.assertEqual(context.tools["memo_add"][0], "personal_memo")
        self.assertEqual(context.tools["memo_admin"][0], "personal_memo_admin")
        self.assertIn("memos", context.commands)
        self.assertNotIn("memo-table", context.commands)

    def test_add_list_transition_and_validation_use_the_paired_skill_database(self):
        added = self.invoke(plugin.tools.memo_add, {
            "title": "Plugin task", "content": "Remember this", "item_type": "task",
            "platform": "telegram", "user_id": "u1", "chat_id": "c1", "idempotency_key": "plugin-add-1",
        })
        self.assertTrue(added["ok"], added)
        item_id = added["result"]["id"]
        replay = self.invoke(plugin.tools.memo_add, {
            "title": "Plugin task", "content": "Remember this", "item_type": "task", "idempotency_key": "plugin-add-1",
        })
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["result"]["id"], item_id)
        listed = self.invoke(plugin.tools.memo_list, {"platform": "telegram", "user_id": "u1", "chat_id": "c1"})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(listed["result"]["count"], 1)
        self.assertIn("| # | 总结 | 类型 | 截止时间 | 优先级 | 状态 | 内容 |", listed["result"]["display_markdown"])
        completed = self.invoke(plugin.tools.memo_transition, {
            "action": "complete", "reference": item_id, "platform": "telegram", "user_id": "u1", "chat_id": "c1",
        })
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["result"]["status"], "completed")
        validated = self.invoke(plugin.tools.memo_admin, {"action": "validate"})
        self.assertTrue(validated["ok"], validated)
        self.assertTrue(validated["result"]["ok"])

    def test_purge_requires_the_core_second_confirmation_token(self):
        added = self.invoke(plugin.tools.memo_add, {"title": "Temporary", "content": "Temporary"})
        item_id = added["result"]["id"]
        rejected = self.invoke(plugin.tools.memo_admin, {"action": "purge", "reference": item_id, "confirm": "DELETE"})
        self.assertFalse(rejected["ok"])
        accepted = self.invoke(plugin.tools.memo_admin, {
            "action": "purge", "reference": item_id, "confirm": f"PERMANENTLY-DELETE:{item_id}",
        })
        self.assertTrue(accepted["ok"], accepted)
        self.assertTrue(accepted["result"]["purged"])

    def test_memos_command_returns_a_core_rendered_markdown_table(self):
        self.invoke(plugin.tools.memo_add, {"title": "Table item", "content": "Table item"})
        context = FakeContext()
        plugin.register(context)
        table = context.commands["memos"]("")
        self.assertIn("**1. —**", table)
        self.assertIn("- 内容：Table item", table)
        self.assertNotIn("| # |", table)
        self.assertIn("Table item", table)

    def test_admin_timezone_migration_uses_shanghai_without_moving_instants(self):
        added = self.invoke(plugin.tools.memo_add, {
            "title": "Timezone", "content": "Timezone", "due_at": "2030-05-01T10:00:00+09:00", "timezone": "Asia/Seoul",
        })
        item_id = added["result"]["id"]
        original_due_at = added["result"]["due_at"]
        migrated = self.invoke(plugin.tools.memo_admin, {"action": "timezone_migrate", "timezone": "Asia/Shanghai"})
        self.assertTrue(migrated["ok"], migrated)
        self.assertEqual(migrated["result"]["timezone"], "Asia/Shanghai")
        shown = self.invoke(plugin.tools.memo_show, {"reference": item_id})
        self.assertTrue(shown["ok"], shown)
        self.assertEqual(shown["result"]["timezone"], "Asia/Shanghai")
        self.assertEqual(shown["result"]["due_at"], original_due_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
