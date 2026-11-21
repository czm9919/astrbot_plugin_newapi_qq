"""自助查询命令（余额 / 签到 / 排行）的离线测试。

复用 test_command_parse 里的 AstrBot 桩与 FakeEvent，只替换站点的 HTTP 调用。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_command_parse import FakeEvent, _load_plugin  # noqa: E402


def _texts(results):
    return [r.text for r in results]


class ParseAnyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()
        cls.plugin = cls.mod.NewApiQQBindPlugin(
            context=None, config={"api_base": "http://127.0.0.1:3000"}
        )

    def test_private_bare_self_commands(self):
        cases = {
            "余额": "me",
            "查询": "me",
            "me": "me",
            "签到": "checkin",
            "排行": "rank",
            "榜单": "rank",
            "rank": "rank",
        }
        for text, expected in cases.items():
            parsed = self.plugin._parse_any(text, FakeEvent(text))
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed[0], expected, text)

    def test_slash_and_mention_forms(self):
        for text in ("/余额", "／余额", "@bot 余额", "@bot /余额"):
            parsed = self.plugin._parse_any(text, FakeEvent(text))
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed[0], "me", text)

    def test_group_requires_prefix(self):
        bare = FakeEvent("余额", private=False)
        self.assertIsNone(self.plugin._parse_any("余额", bare), "群聊裸命令不该触发")
        with_slash = FakeEvent("/余额", private=False)
        self.assertEqual(self.plugin._parse_any("/余额", with_slash)[0], "me")

    def test_chat_sentence_is_not_command(self):
        for text in ("我余额不够了", "余额怎么办", "今天签到没", "排行第一是谁"):
            self.assertIsNone(self.plugin._parse_any(text, FakeEvent(text)), text)

    def test_bind_takes_priority(self):
        parsed = self.plugin._parse_any("绑定 12345678", FakeEvent("绑定 12345678"))
        self.assertEqual(parsed, ("bind", "12345678"))

    def test_legacy_parse_command_ignores_self_commands(self):
        self.assertIsNone(self.plugin._parse_command("/余额"))
        self.assertIsNone(self.plugin._parse_command("签到"))


class SelfHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def _plugin(self, response, token="t0ken"):
        plugin = self.mod.NewApiQQBindPlugin(
            context=None,
            config={"api_base": "http://127.0.0.1:3000", "agent_token": token},
        )

        async def fake_call(method, path, payload=None):
            plugin.last_request = (method, path, payload)
            return response

        plugin._call = fake_call
        return plugin

    def _run(self, plugin, command, event=None):
        event = event or FakeEvent("/余额")
        results = []

        async def collect():
            async for item in plugin._handle_self_inner(event, command):
                results.append(item)

        asyncio.run(collect())
        return _texts(results), event

    def test_me_shows_balance_and_usage(self):
        plugin = self._plugin((200, {"success": True, "data": {
            "bound": True, "username": "tester", "quota": 123456, "quota_usd": 0.2469,
            "used_today": 60000, "daily_limit": 50000,
            "checked_in_today": False, "checkin_enabled": True,
            "checkin_min": 1000, "checkin_max": 10000,
        }}))
        texts, _ = self._run(plugin, "me")
        body = texts[0]
        self.assertIn("tester", body)
        self.assertIn("$0.2469", body)
        self.assertIn("60,000 / 50,000", body)
        self.assertIn("已达上限", body)
        self.assertIn("未签到", body)
        self.assertEqual(plugin.last_request[1], "/api/qq/agent/me?qq=10001")

    def test_me_without_limit(self):
        plugin = self._plugin((200, {"success": True, "data": {
            "bound": True, "username": "tester", "quota": 1000, "quota_usd": 0.002,
            "used_today": 500, "daily_limit": 0,
            "checked_in_today": True, "checkin_enabled": True,
            "checkin_min": 1000, "checkin_max": 10000,
        }}))
        texts, _ = self._run(plugin, "me")
        self.assertIn("未设上限", texts[0])
        self.assertIn("已签到", texts[0])

    def test_me_unbound(self):
        plugin = self._plugin((200, {"success": True, "data": {"bound": False}}))
        texts, _ = self._run(plugin, "me")
        self.assertIn("还没绑定账号", texts[0])

    def test_checkin_success_and_repeat(self):
        plugin = self._plugin((200, {"success": True, "data": {
            "bound": True, "already": False, "quota_awarded": 4009,
            "quota": 4009, "quota_usd": 0.008, "checkin_date": "2026-11-21",
        }}))
        texts, _ = self._run(plugin, "checkin")
        self.assertIn("签到成功", texts[0])
        self.assertIn("4,009", texts[0])

        repeat = self._plugin((200, {"success": True, "data": {"bound": True, "already": True}}))
        texts, _ = self._run(repeat, "checkin")
        self.assertIn("已经签过了", texts[0])

    def test_rank_empty_and_filled(self):
        empty = self._plugin((200, {"success": True, "data": {"date": "2026-11-21", "items": []}}))
        texts, _ = self._run(empty, "rank")
        self.assertIn("榜是空的", texts[0])

        filled = self._plugin((200, {"success": True, "data": {
            "date": "2026-11-21",
            "items": [{"rank": 1, "username": "ad***in", "quota": 60000},
                      {"rank": 2, "username": "te***er", "quota": 1200}],
        }}))
        texts, _ = self._run(filled, "rank")
        self.assertIn("ad***in", texts[0])
        self.assertIn("60,000", texts[0])
        self.assertIn("2. te***er", texts[0])

    def test_cooldown_blocks_second_call(self):
        plugin = self._plugin((200, {"success": True, "data": {
            "bound": True, "username": "t", "quota": 1, "quota_usd": 0.0,
            "used_today": 0, "daily_limit": 0, "checked_in_today": True,
            "checkin_enabled": True, "checkin_min": 1, "checkin_max": 2,
        }}))
        self._run(plugin, "me")
        texts, _ = self._run(plugin, "me")
        self.assertIn("查得太勤", texts[0])

    def test_missing_token_explained(self):
        plugin = self._plugin((200, {"success": True, "data": {"bound": False}}), token="")
        texts, _ = self._run(plugin, "me")
        self.assertIn("服务令牌", texts[0])

    def test_error_is_readable(self):
        plugin = self._plugin((403, {"success": False, "code": "QQ_AGENT_ACCOUNT_DISABLED",
                                     "message": "account is disabled"}))
        texts, _ = self._run(plugin, "me")
        self.assertIn("已被禁用", texts[0])


if __name__ == "__main__":
    unittest.main()
