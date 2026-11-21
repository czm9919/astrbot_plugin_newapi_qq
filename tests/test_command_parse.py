"""命令解析与去重的离线测试。

AstrBot 不在测试环境里，这里用最小桩把 `astrbot.api.*` 顶掉，
然后直接 import 插件的 main.py。目的是把「命令没反应」这类问题钉死在
解析层，而不是靠上线后手敲 QQ 验证。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILE = ROOT / "main.py"


def _install_astrbot_stubs() -> None:
    """构造 astrbot.api 的最小可用替身。已安装真框架时直接跳过。"""
    if "astrbot.api" in sys.modules:
        return

    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401 - stub
        pass

    class _EMT:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"

    def _passthrough(*args, **kwargs):
        def deco(func):
            return func

        return deco

    class Filter:
        EventMessageType = _EMT
        command = staticmethod(_passthrough)
        event_message_type = staticmethod(_passthrough)
        command_group = staticmethod(_passthrough)
        regex = staticmethod(_passthrough)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = Filter
    api.event = event_mod

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:  # noqa: D401 - stub
        pass

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.register = register
    api.star = star_mod

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


def _load_plugin():
    _install_astrbot_stubs()
    spec = importlib.util.spec_from_file_location("plugin_main_under_test", PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeResult:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    def __init__(self, text, private=True, sender="10001"):
        self.message_str = text
        self._private = private
        self._sender = sender
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return FakeResult(text)

    def stop_event(self):
        self.stopped = True


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()
        cls.plugin = cls.mod.NewApiQQBindPlugin(context=None, config={"api_base": "http://127.0.0.1:3000"})

    def test_slash_with_space(self):
        self.assertEqual(self.plugin._parse_command("/绑定 12345678"), "12345678")

    def test_bare_without_slash(self):
        self.assertEqual(self.plugin._parse_command("绑定 12345678"), "12345678")

    def test_tight_form(self):
        self.assertEqual(self.plugin._parse_command("/绑定12345678"), "12345678")

    def test_fullwidth_slash(self):
        self.assertEqual(self.plugin._parse_command("／绑定 12345678"), "12345678")

    def test_english_alias(self):
        self.assertEqual(self.plugin._parse_command("/bind 12345678"), "12345678")

    def test_at_mention(self):
        self.assertEqual(self.plugin._parse_command("@bot /绑定 12345678"), "12345678")

    def test_colon_separator(self):
        self.assertEqual(self.plugin._parse_command("绑定：12345678"), "12345678")

    def test_noise_is_ignored(self):
        for text in ("你好", "绑定是什么意思", "群主把绑定开了", "/help"):
            self.assertIsNone(self.plugin._parse_command(text), text)

    def test_private_bare_command_shows_usage(self):
        self.assertEqual(self.plugin._parse_command("绑定"), "")

    def test_custom_command_replaces_defaults(self):
        plugin = self.mod.NewApiQQBindPlugin(context=None, config={"bind_command": "绑定号"})
        self.assertEqual(plugin._parse_command("绑定号 12345678"), "12345678")
        self.assertIsNone(plugin._parse_command("/绑定 12345678"))

    def test_group_usage_suppressed(self):
        event = FakeEvent("绑定", private=False)
        self.assertIsNone(self.plugin._parse_command("绑定", event))
        # 群里带上参数仍然要能用
        self.assertEqual(self.plugin._parse_command("绑定 12345678", event), "12345678")


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def run_dispatch(self, plugin, event, code):
        async def collect():
            out = []
            async for result in plugin._dispatch(event, code):
                out.append(result)
            return out

        return asyncio.run(collect())

    def test_handled_only_once(self):
        plugin = self.mod.NewApiQQBindPlugin(
            context=None, config={"api_base": "http://127.0.0.1:3000", "agent_token": "x" * 32}
        )

        async def fake_call(method, path, payload=None):
            return 200, {"success": True, "data": {"username": "tester", "qq": "10001"}}

        plugin._call = fake_call  # type: ignore[assignment]
        event = FakeEvent("/绑定 12345678")
        first = self.run_dispatch(plugin, event, "12345678")
        second = self.run_dispatch(plugin, event, "12345678")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertTrue(event.stopped)

    def test_missing_token_is_explained(self):
        plugin = self.mod.NewApiQQBindPlugin(context=None, config={"api_base": "http://127.0.0.1:3000"})
        event = FakeEvent("绑定 12345678")
        out = self.run_dispatch(plugin, event, "12345678")
        self.assertEqual(len(out), 1)
        self.assertIn("服务令牌", out[0].text)

    def test_diagnostic_reports_version(self):
        plugin = self.mod.NewApiQQBindPlugin(
            context=None, config={"api_base": "http://127.0.0.1:3000", "agent_token": "abcd1234"}
        )

        async def fake_call(method, path, payload=None):
            return 200, {"success": True, "data": {"require_qq_free": True}}

        plugin._call = fake_call  # type: ignore[assignment]
        event = FakeEvent("绑定 诊断")
        out = self.run_dispatch(plugin, event, "诊断")
        self.assertEqual(len(out), 1)
        self.assertIn(self.mod.PLUGIN_VERSION, out[0].text)
        self.assertIn("1234", out[0].text)
        self.assertIn("HTTP 200", out[0].text)


if __name__ == "__main__":
    unittest.main()
