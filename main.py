"""
AstrBot 插件：把 QQ 号绑定到 New API 面板账号。

设计要点：
  * 站点不接触 QQ 协议，只信任「谁在发消息」这一个事实；
  * 绑定凭据是面板生成的一次性码（8 位、10 分钟、用过即废）；
  * 本插件用管理员下发的服务令牌调用站点 /api/qq/agent/verify。

除 AstrBot 自带依赖外只用标准库（urllib），避免额外安装 requirements。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_newapi_qq"

# 站点错误码 → 给用户看的中文提示
ERROR_HINTS = {
    "QQ_BIND_CODE_INVALID": "绑定码无效或已过期，请回面板重新生成（10 分钟内有效）。",
    "QQ_ALREADY_BOUND": "这个 QQ 已经绑定到别的账号了，请先在原账号解绑。",
    "QQ_ACCOUNT_ALREADY_BOUND": "你的账号已经绑定了别的 QQ，请先在面板解绑。",
    "QQ_AGENT_UNAUTHORIZED": "机器人令牌无效，请联系管理员检查配置。",
    "QQ_AGENT_UNAVAILABLE": "站点尚未开启机器人接入（未配置服务令牌），请联系管理员。",
    "INVALID_REQUEST": "请求格式不对，请检查绑定码。",
}


@register(
    PLUGIN_NAME,
    "new-api-operator",
    "把 QQ 号绑定到 New API 面板账号，解锁幸运大转盘与免费模型",
    "1.0.0",
)
class NewApiQQBindPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ------------------------------------------------------------------ utils

    def _cfg(self, key: str, default=""):
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip()
        return value if value is not None else default

    def _base_url(self) -> str:
        base = self._cfg("api_base", "http://127.0.0.1:3000").rstrip("/")
        # 容错：用户可能填了 .../api
        if base.endswith("/api"):
            base = base[: -len("/api")]
        return base

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        """同步 HTTP 调用，放到线程池里执行。返回 (status, body_json)。"""
        url = self._base_url() + path
        data = None
        headers = {"Accept": "application/json"}
        token = self._cfg("agent_token")
        if token:
            headers["X-QQ-Agent-Token"] = token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        timeout = float(self._cfg("timeout_seconds", 10) or 10)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.status, _safe_json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, _safe_json(raw)
        except Exception as exc:  # 网络层失败
            logger.warning(f"[{PLUGIN_NAME}] request failed: {exc}")
            return 0, {"message": str(exc)}

    async def _call(self, method: str, path: str, payload: dict | None = None):
        return await asyncio.to_thread(self._request, method, path, payload)

    def _sender_qq(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter())
            except Exception:  # pragma: no cover - 框架差异
                pass
        return ""

    # ---------------------------------------------------------------- commands

    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent, code: str | None = None):
        """绑定个人 QQ 到 New API 面板账号：/绑定 <绑定码>"""
        async for result in self._handle_bind(event, code):
            yield result

    @filter.command("bind")
    async def bind_en(self, event: AstrMessageEvent, code: str | None = None):
        """/bind <code> — English alias."""
        async for result in self._handle_bind(event, code):
            yield result

    async def _handle_bind(self, event: AstrMessageEvent, code: str | None):
        code = (code or "").strip()
        qq = self._sender_qq(event)

        if not qq:
            yield event.plain_result("拿不到你的 QQ 号，请私聊机器人使用本命令。")
            return

        if not self._cfg("agent_token"):
            yield event.plain_result(
                "机器人还没配置服务令牌，请联系站点管理员在面板填写（qq_agent_token）。"
            )
            return

        # 自检：/绑定 测试
        if code in ("测试", "test", "ping"):
            status, body = await self._call("GET", "/api/qq/agent/ping")
            if status == 200 and body.get("success"):
                data = body.get("data") or {}
                yield event.plain_result(
                    "站点连接正常。免费模型绑定要求："
                    + ("开启" if data.get("require_qq_free") else "关闭")
                )
            else:
                yield event.plain_result(self._error_text(status, body))
            return

        if not code:
            yield event.plain_result(
                "用法：/绑定 <绑定码>\n"
                "绑定码请到面板「幸运大转盘 → QQ 绑定」生成，10 分钟内有效。"
            )
            return

        status, body = await self._call(
            "POST", "/api/qq/agent/verify", {"code": code, "qq": qq}
        )
        if status == 200 and body.get("success"):
            data = body.get("data") or {}
            username = data.get("username") or "未知"
            yield event.plain_result(
                f"绑定成功 ✅\n站点账号：{username}\nQQ：{data.get('qq', qq)}\n"
                "现在可以回面板抽奖、使用免费模型了。"
            )
            return

        yield event.plain_result(self._error_text(status, body))

    # 自定义命令名（配置项 bind_command）：只在名字不是默认的两个时才接管
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def alias_router(self, event: AstrMessageEvent):
        custom = self._cfg("bind_command", "绑定")
        if custom in ("", "绑定", "bind"):
            return
        text = (event.message_str or "").strip()
        for prefix in (f"/{custom}", custom):
            if text.startswith(prefix):
                code = text[len(prefix):].strip()
                # 去掉可能的分隔符
                code = code.lstrip(" :：").strip()
                async for result in self._handle_bind(event, code):
                    yield result
                event.stop_event()
                return

    # ------------------------------------------------------------------ helpers

    def _error_text(self, status: int, body: dict) -> str:
        if status == 0:
            return (
                "站点连接失败，请稍后重试。\n"
                f"（管理员检查 api_base 是否可达：{self._base_url()}）"
            )
        code = str(body.get("code") or "")
        if code in ERROR_HINTS:
            return ERROR_HINTS[code]
        message = body.get("message") or body.get("msg") or ""
        if status == 503:
            return ERROR_HINTS["QQ_AGENT_UNAVAILABLE"]
        if status == 401:
            return ERROR_HINTS["QQ_AGENT_UNAUTHORIZED"]
        return f"绑定失败（HTTP {status}）{('：' + message) if message else ''}"


def _safe_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except Exception:
        return {"message": raw[:200]}
