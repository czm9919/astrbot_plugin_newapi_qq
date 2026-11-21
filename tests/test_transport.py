"""网络连接加固（v1.0.5）的离线测试。

背景：「插件连不上 new-api」是最高频的报障，但真正的原因分散在
容器回环地址、代理环境变量、自签证书、瞬时抖动四件事上，光看
「站点连接失败」五个字完全没法排查。这里把分类、重试边界和
文案钉死，避免下次改代码又退化成一句含糊的报错。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_command_parse import _load_plugin  # noqa: E402


class _PingHandler(BaseHTTPRequestHandler):
    """最小的 ping 端点，只回 200 + success，用来验证代理/证书行为。"""

    def _reply(self):
        body = b'{"success": true, "data": {"ok": true}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args):
        pass


class FakeResponse:
    def __init__(self, status=200, body='{"success": true}'):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ScriptedOpener:
    """按脚本依次抛异常或返回响应，用来验证重试次数与重试条件。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def build_plugin(mod, **config):
    base = {"api_base": "https://api.example.com", "agent_token": "t" * 32}
    base.update(config)
    return mod.NewApiQQBindPlugin(context=None, config=base)


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def test_maps_socket_errors(self):
        self.assertEqual(self.mod._classify(socket.timeout()), "timeout")
        self.assertEqual(self.mod._classify(TimeoutError()), "timeout")
        self.assertEqual(self.mod._classify(ConnectionRefusedError()), "refused")
        self.assertEqual(self.mod._classify(ConnectionResetError()), "reset")
        self.assertEqual(self.mod._classify(ssl.SSLError("bad")), "tls")
        self.assertEqual(self.mod._classify(socket.gaierror()), "dns")

    def test_maps_string_fallbacks(self):
        # urllib 有时只把 reason 包成字符串，分类不能因此失手
        self.assertEqual(self.mod._classify("timed out"), "timeout")
        self.assertEqual(self.mod._classify("Connection refused"), "refused")
        self.assertEqual(self.mod._classify("certificate verify failed"), "tls")
        self.assertEqual(self.mod._classify("weird"), "unknown")


class LoopbackTrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def test_detects_loopback_with_and_without_container(self):
        plugin = build_plugin(self.mod, api_base="http://127.0.0.1:3000")
        self.assertTrue(plugin._is_loopback())
        original = self.mod._inside_container
        try:
            self.mod._inside_container = lambda: True
            self.assertTrue(plugin._loopback_trap())
            self.mod._inside_container = lambda: False
            self.assertFalse(plugin._loopback_trap())
        finally:
            self.mod._inside_container = original

    def test_public_host_is_not_loopback(self):
        plugin = build_plugin(self.mod, api_base="https://api.example.com")
        self.assertFalse(plugin._is_loopback())
        self.assertFalse(plugin._loopback_trap())

    def test_trap_text_names_a_working_alternative(self):
        plugin = build_plugin(self.mod, api_base="http://127.0.0.1:3000")
        original = self.mod._inside_container
        try:
            self.mod._inside_container = lambda: True
            text = plugin._error_text(0, {"error_kind": "refused"})
        finally:
            self.mod._inside_container = original
        self.assertIn("127.0.0.1", text)
        self.assertIn("容器", text)
        self.assertIn("172.17.0.1", text)


class FlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def test_flag_accepts_bool_and_strings(self):
        for raw, expected in (
            (True, True),
            (False, False),
            ("true", True),
            ("False", False),
            ("1", True),
            ("0", False),
            (1, True),
            (0, False),
            ("", False),
        ):
            plugin = build_plugin(self.mod, bypass_proxy=raw)
            self.assertEqual(plugin._flag("bypass_proxy", True), expected, raw)

    def test_attempt_budget_is_clamped(self):
        for raw, expected in ((-5, 1), (0, 1), (1, 2), (4, 5), (99, 5), ("x", 2)):
            plugin = build_plugin(self.mod, retry_attempts=raw)
            self.assertEqual(plugin._attempt_budget(), expected, raw)


class RetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()

    def setUp(self):
        self._sleep = self.mod.time.sleep
        self.mod.time.sleep = lambda *_: None  # 重试退避不打真实等待

    def tearDown(self):
        self.mod.time.sleep = self._sleep

    def test_retries_refused_then_succeeds(self):
        plugin = build_plugin(self.mod, retry_attempts=2)
        opener = ScriptedOpener(
            [
                urllib.error.URLError(ConnectionRefusedError("refused")),
                FakeResponse(200, '{"success": true, "data": {"ok": 1}}'),
            ]
        )
        plugin._opener = lambda: opener
        status, body = plugin._request("GET", "/api/qq/agent/ping")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("success"))
        self.assertEqual(opener.calls, 2)

    def test_does_not_retry_timeouts(self):
        # 超时可能已经把请求送到了服务端：重试会让一次性绑定码被消费两次
        plugin = build_plugin(self.mod, retry_attempts=3)
        opener = ScriptedOpener([urllib.error.URLError(socket.timeout())] * 4)
        plugin._opener = lambda: opener
        status, body = plugin._request(
            "POST", "/api/qq/agent/verify", {"code": "x", "qq": "1"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(body.get("error_kind"), "timeout")
        self.assertEqual(opener.calls, 1)

    def test_gives_up_after_budget(self):
        plugin = build_plugin(self.mod, retry_attempts=2)
        opener = ScriptedOpener(
            [urllib.error.URLError(ConnectionRefusedError("refused"))] * 5
        )
        plugin._opener = lambda: opener
        status, body = plugin._request("GET", "/api/qq/agent/ping")
        self.assertEqual(status, 0)
        self.assertEqual(body.get("error_kind"), "refused")
        self.assertEqual(opener.calls, 3)  # 1 次 + 2 次重试

    def test_http_error_is_not_retried(self):
        plugin = build_plugin(self.mod, retry_attempts=3)

        class HttpFailOpener:
            calls = 0

            def open(self, request, timeout=None):
                HttpFailOpener.calls += 1
                raise urllib.error.HTTPError(
                    "https://api.example.com/x",
                    503,
                    "unavailable",
                    {},
                    None,
                )

        plugin._opener = lambda: HttpFailOpener()
        status, _ = plugin._request("GET", "/api/qq/agent/ping")
        self.assertEqual(status, 503)
        self.assertEqual(HttpFailOpener.calls, 1)


class ProxyBypassTests(unittest.TestCase):
    """用真实本地 HTTP 服务验证「设了 HTTP_PROXY 也能连上」。

    CPython 的空 ProxyHandler 有个坑：proxies 为空时实例上不会生成任何
    `*_open` 方法，于是 add_handler 什么都不注册。效果确实是「不用代理」，
    但靠它推断行为太脆，所以这里直接打一次真实请求。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_request_succeeds_while_http_proxy_is_set(self):
        # 指向一个必定没人监听的端口：真被代理劫持就一定失败
        env_backup = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy")}
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        try:
            plugin = build_plugin(
                self.mod,
                api_base=f"http://127.0.0.1:{self.port}",
                retry_attempts=0,
                startup_check=False,
            )
            status, body = plugin._request("GET", "/api/qq/agent/ping")
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(status, 200)
        self.assertTrue(body.get("success"))

    def test_insecure_tls_adds_https_handler(self):
        plugin = build_plugin(self.mod, insecure_skip_verify=True, startup_check=False)
        handlers = plugin._opener().handlers
        self.assertTrue(
            any(isinstance(h, urllib.request.HTTPSHandler) for h in handlers)
        )


if __name__ == "__main__":
    unittest.main()
