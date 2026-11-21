"""
AstrBot 插件：把 QQ 号绑定到 New API 面板账号。

设计要点：
  * 站点不接触 QQ 协议，只信任「谁在发消息」这一个事实；
  * 绑定凭据是面板生成的一次性码（8 位、10 分钟、用过即废）；
  * 本插件用管理员下发的服务令牌调用站点 /api/qq/agent/verify。

除 AstrBot 自带依赖外只用标准库（urllib），避免额外安装 requirements。

v1.0.5 起把「连不上站点」当成一等公民处理：
  * 默认绕过 HTTP_PROXY/HTTPS_PROXY —— 容器里这两个变量会把回环和内网请求
    一起送去代理，表现为 502 或莫名超时，是最常见的假故障；
  * 只对「连接根本没建立」的错误自动重试，超时/证书错误一律不重试
    （避免一次绑定码被消费两次）；
  * 每种失败都翻译成能照着做的中文，尤其是「机器人跑在容器里却填了
    127.0.0.1」这个新手必踩的坑。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_newapi_qq"
PLUGIN_VERSION = "1.0.5"

# 回环地址在「机器人跑容器里」时等于容器自己，永远连不到宿主机上的站点。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]")
# 只有这些异常说明「请求压根没发出去」，重试才是安全的。
# 超时不在其中：包可能已经到达服务端，重试会让一次性绑定码被消费两次。
RETRIABLE_ERRORS = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.gaierror,
)

# 裸命令（不带斜杠）也接受的中文/英文命令名。斜杠形式仍由 @filter.command 处理，
# 两条路都走 _dispatch，靠事件标记去重，不会回两次。
DEFAULT_COMMAND_NAMES = ("绑定", "bind")
# 命令名与参数之间允许的分隔符（含全角）
SEPARATORS = (" ", "\u3000", ":", "：", ",", "，")

# 自助查询命令（v1.0.4）：命令名 -> 站点侧动作。
# 这些命令不接受参数，群聊里必须带 / 或 @机器人 才触发，避免把
# 「我余额不够了」这类日常聊天当成指令。
SELF_COMMANDS = {
    "余额": "me",
    "查询": "me",
    "me": "me",
    "签到": "checkin",
    "checkin": "checkin",
    "排行": "rank",
    "榜单": "rank",
    "rank": "rank",
}
# 自助命令在群聊里的冷却：只防连点刷屏，不影响正常使用
SELF_COOLDOWN_DEFAULT = 3

# 本地冷却分段口径：
#   * 成功锁 30 秒——绑定不可重入，连点只会白打请求；
#   * 失败锁 3 秒——打错一位的用户要能立刻重试，但连点要被挡下。
# 只锁成功挡不住拿错误码刷接口的人，只锁失败又把正常重试变成折磨，所以分开计。
# 两个数值都能在插件配置里覆盖（cooldown_seconds / retry_cooldown_seconds）。
COOLDOWN_OK_DEFAULT = 30
COOLDOWN_FAIL_DEFAULT = 3
COOLDOWN_STATE_SWEEP_THRESHOLD = 2048

# 站点错误码 → 给用户看的中文提示
ERROR_HINTS = {
    "QQ_BIND_CODE_INVALID": "绑定码无效或已过期，请回面板重新生成（10 分钟内有效）。",
    "QQ_ALREADY_BOUND": "这个 QQ 已经绑定到别的账号了，请先在原账号解绑。",
    "QQ_ACCOUNT_ALREADY_BOUND": "你的账号已经绑定了别的 QQ，请先在面板解绑。",
    "QQ_AGENT_UNAUTHORIZED": "机器人令牌无效，请联系管理员检查配置。",
    "QQ_AGENT_UNAVAILABLE": "站点尚未开启机器人接入（未配置服务令牌），请联系管理员。",
    "QQ_AGENT_RATE_LIMITED": "操作太频繁了，请过一会儿再试。",
    "QQ_AGENT_ACCOUNT_DISABLED": "你的站点账号已被禁用，请联系管理员。",
    "QQ_AGENT_CHECKIN_DISABLED": "站点暂未开启签到功能。",
    "INVALID_REQUEST": "请求格式不对，请检查绑定码。",
}


@register(
    PLUGIN_NAME,
    "new-api-operator",
    "把 QQ 号绑定到 New API 面板账号，解锁幸运大转盘与免费模型",
    PLUGIN_VERSION,
)
class NewApiQQBindPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        # 纯内存冷却状态：进程重启即清零，不落库，与站点侧限流互为备份
        self._last_attempt: dict[str, float] = {}
        self._last_success: dict[str, float] = {}
        self._last_self: dict[str, float] = {}
        # 加载一行日志：命令没反应时，先看 AstrBot 日志里有没有这一行，
        # 没有就是插件根本没加载（目录层级、语法错误、版本不兼容）。
        try:
            logger.info(
                f"[{PLUGIN_NAME}] loaded v{PLUGIN_VERSION} "
                f"api_base={self._base_url()} "
                f"token={'set' if self._cfg('agent_token') else 'MISSING'} "
                f"commands={self._command_names()}"
            )
        except Exception:
            pass
        # 启动自检：异步探一次连通性，把结果写进日志。失败不阻塞加载，
        # 也不改变任何命令行为——只是让「装好了却连不上」在日志里有据可查。
        self._startup_probe_ok: bool | None = None
        self._probe_started = False
        if self._flag("startup_check", True):
            self._schedule_startup_probe()

    def _schedule_startup_probe(self) -> None:
        """有运行中的事件循环就排一个后台自检；没有就交给 initialize()。

        这里必须用 get_running_loop 而不是 get_event_loop：同步加载（含单测）
        时后者会返回一个没人跑的 loop，create_task 出来的任务永远不会执行，
        只在解释器退出时刷一行 "Task was destroyed but it is pending"。
        """
        if self._probe_started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._probe_started = True
        try:
            loop.create_task(self._startup_probe())
        except Exception:  # pragma: no cover - 极端情况下的框架差异
            pass

    async def initialize(self):
        """AstrBot 的异步加载钩子：同步构造时没能排上的自检在这里补上。"""
        if self._probe_started or not self._flag("startup_check", True):
            return
        self._probe_started = True
        await self._startup_probe()

    async def _startup_probe(self) -> None:
        try:
            await asyncio.sleep(3)  # 让 AstrBot 先把其它插件加载完
            status, body = await self._call("GET", "/api/qq/agent/ping")
            ok = status == 200 and bool(body.get("success"))
            self._startup_probe_ok = ok
            if ok:
                logger.info(f"[{PLUGIN_NAME}] startup check OK — {self._base_url()}")
                return
            if status == 0:
                logger.error(
                    f"[{PLUGIN_NAME}] startup check FAILED — "
                    f"{self._network_error_text(body).replace(chr(10), ' | ')}"
                )
                return
            logger.warning(
                f"[{PLUGIN_NAME}] startup check got HTTP {status} — "
                f"{self._error_text(status, body).replace(chr(10), ' | ')}"
            )
        except Exception as exc:  # pragma: no cover - 自检绝不影响主流程
            logger.debug(f"[{PLUGIN_NAME}] startup probe skipped: {exc!r}")

    # ------------------------------------------------------------------ utils

    def _cfg(self, key: str, default=""):
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip()
        return value if value is not None else default

    def _flag(self, key: str, default: bool = False) -> bool:
        """读开关。配置面板可能给 bool，json 手改可能是 "true"/"false"，都要认。"""
        raw = self.config.get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return default

    def _base_url(self) -> str:
        base = self._cfg("api_base", "http://127.0.0.1:3000").rstrip("/")
        # 容错：用户可能填了 .../api
        if base.endswith("/api"):
            base = base[: -len("/api")]
        return base

    def _base_host(self) -> str:
        """取出 api_base 里的主机名，用于判断是不是回环地址。"""
        try:
            return (urllib.parse.urlsplit(self._base_url()).hostname or "").lower()
        except Exception:  # pragma: no cover - 配置极脏时
            return ""

    def _is_loopback(self) -> bool:
        return self._base_host() in LOOPBACK_HOSTS

    def _inside_container(self) -> bool:
        """AstrBot 常见部署是 Docker；容器里的 127.0.0.1 不是宿主机。"""
        return _inside_container()

    def _loopback_trap(self) -> bool:
        """回环地址 + 容器 = 必定连不上，值得单独说清楚。"""
        return self._is_loopback() and self._inside_container()

    def _opener(self) -> urllib.request.OpenerDirector:
        """按配置构造 opener。

        绕代理是默认行为，原因见模块 docstring：容器里残留的 HTTP_PROXY
        会让「连不上站点」看起来像站点挂了，实际是代理把回环请求吞了。
        """
        handlers: list = []
        if self._flag("bypass_proxy", True):
            # 空 ProxyHandler = 不读任何 *_proxy 环境变量
            handlers.append(urllib.request.ProxyHandler({}))
        if self._flag("insecure_skip_verify", False):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _attempt_budget(self) -> int:
        """总尝试次数 = 1 + 重试次数，夹在 1~5 之间防止把配置写成死循环。"""
        try:
            retries = int(self._cfg("retry_attempts", 1) or 0)
        except (TypeError, ValueError):
            retries = 1
        return max(1, min(5, retries + 1))

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        """同步 HTTP 调用，放到线程池里执行。返回 (status, body_json)。

        status == 0 表示「没拿到 HTTP 响应」，body 里带 `error_kind` 供上层
        翻译成人话；这是与「服务端返回 4xx/5xx」完全不同的两类失败。
        """
        url = self._base_url() + path
        data = None
        headers = {"Accept": "application/json"}
        token = self._cfg("agent_token")
        if token:
            headers["X-QQ-Agent-Token"] = token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        timeout = float(self._cfg("timeout_seconds", 10) or 10)
        opener = self._opener()
        attempts = self._attempt_budget()
        last: tuple[int, dict] = (0, {"error_kind": "unknown"})

        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with opener.open(request, timeout=timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    return response.status, _safe_json(raw)
            except urllib.error.HTTPError as exc:
                # 服务端明确表态了（401/429/503…），重试没有意义
                raw = exc.read().decode("utf-8", errors="replace")
                return exc.code, _safe_json(raw)
            except urllib.error.URLError as exc:
                reason = exc.reason
                kind = _classify(reason)
                last = (0, {"message": str(reason), "error_kind": kind})
                # 只有「连接没建立」才重试；超时/证书错误重试会带来副作用
                if kind not in ("refused", "dns") or attempt + 1 >= attempts:
                    break
                logger.warning(
                    f"[{PLUGIN_NAME}] {kind} on {url}, retry "
                    f"{attempt + 1}/{attempts - 1}"
                )
                time.sleep(min(2.0, 0.3 * (2**attempt)))
            except RETRIABLE_ERRORS as exc:
                last = (0, {"message": str(exc), "error_kind": _classify(exc)})
                if attempt + 1 >= attempts:
                    break
                logger.warning(
                    f"[{PLUGIN_NAME}] {type(exc).__name__} on {url}, retry "
                    f"{attempt + 1}/{attempts - 1}"
                )
                time.sleep(min(2.0, 0.3 * (2**attempt)))
            except Exception as exc:  # 其它网络层失败：不重试，如实上报
                last = (0, {"message": str(exc), "error_kind": _classify(exc)})
                break

        logger.warning(f"[{PLUGIN_NAME}] request failed: {last[1]}")
        return last

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

    # --------------------------------------------------------------- cooldown

    def _window(self, key: str, default: int) -> float:
        """读冷却秒数；配置写错（负数/字符串/None）一律回落默认值，绝不抛。"""
        raw = self._cfg(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return float(default)
        return value if value >= 0 else float(default)

    def _cooldown_remaining(self, qq: str) -> int:
        """还需等待的秒数；0 表示现在可以执行。"""
        now = time.time()
        wait = 0.0
        ok_at = self._last_success.get(qq)
        if ok_at is not None:
            wait = max(wait, ok_at + self._window("cooldown_seconds", COOLDOWN_OK_DEFAULT) - now)
        fail_at = self._last_attempt.get(qq)
        if fail_at is not None:
            wait = max(wait, fail_at + self._window("retry_cooldown_seconds", COOLDOWN_FAIL_DEFAULT) - now)
        return int(wait) + 1 if wait > 0 else 0

    def _mark_attempt(self, qq: str, success: bool) -> None:
        now = time.time()
        self._last_attempt[qq] = now
        if success:
            self._last_success[qq] = now
        self._sweep_state()

    def _sweep_state(self) -> None:
        """字典只增不减会在长跑机器人上攒垃圾，超阈值时清掉一小时前的条目。"""
        if len(self._last_attempt) <= COOLDOWN_STATE_SWEEP_THRESHOLD:
            return
        cutoff = time.time() - 3600
        self._last_attempt = {k: v for k, v in self._last_attempt.items() if v >= cutoff}
        self._last_success = {k: v for k, v in self._last_success.items() if v >= cutoff}

    async def terminate(self):
        """插件卸载/重载时清空内存态，避免热重载后残留冷却。"""
        self._last_attempt.clear()
        self._last_success.clear()
        last_self = getattr(self, "_last_self", None)
        if last_self is not None:
            last_self.clear()
        handled = getattr(self, "_handled_ids", None)
        if handled is not None:
            handled.clear()

    # ---------------------------------------------------------------- commands

    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent, code: str = ""):
        """绑定个人 QQ 到 New API 面板账号：/绑定 <绑定码>"""
        async for result in self._dispatch(event, code):
            yield result

    @filter.command("bind")
    async def bind_en(self, event: AstrMessageEvent, code: str = ""):
        """/bind <code> — English alias."""
        async for result in self._dispatch(event, code):
            yield result

    # 兜底路由：@filter.command 只吃「/命令名」这一种形式。实际用起来，很多人
    # 直接发「绑定 12345678」，或者客户端把开头的斜杠吞掉，这时命令看起来就是
    # 「完全没反应」。这个 handler 接住裸命令、全角斜杠、@机器人 前缀、
    # 以及 /绑定12345678 这种紧贴写法。
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def command_router(self, event: AstrMessageEvent):
        parsed = self._parse_any(getattr(event, "message_str", "") or "", event)
        if parsed is None:
            return
        command, arg = parsed
        if command == "bind":
            async for result in self._dispatch(event, arg):
                yield result
        else:
            async for result in self._dispatch_self(event, command):
                yield result

    async def _dispatch(self, event: AstrMessageEvent, code: str):
        """所有入口的唯一执行点。

        AstrBot 会把同一个事件依次交给多个 handler：`@filter.command` 命中时，
        `event_message_type(ALL)` 也会收到同一条消息。所以在事件对象上打标记，
        保证只处理一次——否则用户会收到两条一样的回复。
        """
        if self._already_handled(event):
            return
        self._mark_handled(event)
        try:
            async for result in self._handle_bind(event, code):
                yield result
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    async def _dispatch_self(self, event: AstrMessageEvent, command: str):
        """自助查询的第二条执行路径，同样走一次性标记。"""
        if self._already_handled(event):
            return
        self._mark_handled(event)
        try:
            async for result in self._handle_self(event, command):
                yield result
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    # ---- 事件去重标记 -------------------------------------------------
    # 优先用 AstrBot 自己的 set_extra/get_extra；老版本没有这两个方法时退回
    # 属性赋值；两种都不行（事件对象带 __slots__）时用 id 集合兜底。
    # 任何一步都不允许抛异常——这里抛了，用户看到的就是「命令没反应」。
    _HANDLED_KEY = "newapi_qq_handled"

    def _already_handled(self, event: AstrMessageEvent) -> bool:
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                return bool(getter(self._HANDLED_KEY))
            except Exception:
                pass
        if getattr(event, "_newapi_qq_handled", False):
            return True
        return id(event) in getattr(self, "_handled_ids", ())

    def _mark_handled(self, event: AstrMessageEvent) -> None:
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            try:
                setter(self._HANDLED_KEY, True)
                return
            except Exception:
                pass
        try:
            setattr(event, "_newapi_qq_handled", True)
            return
        except Exception:
            pass
        handled = getattr(self, "_handled_ids", None)
        if handled is None:
            handled = self._handled_ids = set()
        handled.add(id(event))
        if len(handled) > 512:
            handled.clear()

    def _command_names(self) -> list:
        """当前生效的命令名。配置了自定义名就只认自定义名。"""
        custom = self._cfg("bind_command", "绑定")
        if custom and custom not in ("",) + DEFAULT_COMMAND_NAMES:
            return [custom]
        return list(DEFAULT_COMMAND_NAMES)

    def _is_private(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        return False

    def _strip_prefix(self, raw: str) -> str:
        """去掉指令前缀（半角/全角斜杠、感叹号）。"""
        for prefix in ("/", "／", "!", "！"):
            if raw.startswith(prefix):
                return raw[1:].lstrip()
        return raw

    def _normalize(self, text: str):
        """去掉前缀与 @机器人，返回 (正文, 是否带过前缀)。"""
        raw = (text or "").strip()
        prefixed = False
        stripped = self._strip_prefix(raw)
        if stripped != raw:
            prefixed = True
            raw = stripped
        # 去掉开头的 @机器人，再去一次前缀（微信/QQ 常见顺序是「@bot /命令」）
        while raw.startswith("@"):
            parts = raw.split(None, 1)
            if len(parts) < 2:
                return "", prefixed
            prefixed = True
            raw = self._strip_prefix(parts[1].lstrip())
        return raw, prefixed

    def _match_name(self, raw: str, name: str, allow_glued: bool):
        """raw 以 name 开头时返回剩余参数；不是该命令返回 None。

        返回 "" 表示命令被识别但没带参数。
        """
        if not name or not raw.startswith(name):
            return None
        rest = raw[len(name):]
        if not rest:
            return ""
        if rest[0] in SEPARATORS:
            return rest[1:].strip()
        if allow_glued and (rest[0].isdigit() or (rest[0].isascii() and rest[0].isalpha())):
            # 紧贴写法：/绑定12345678。只认 ASCII 字母数字，
            # 否则「绑定是什么意思」会被当成绑定码 "是什么意思"。
            return rest.strip()
        return None

    def _parse_any(self, text: str, event=None):
        """解析任意本插件命令，返回 (动作, 参数)；都不匹配返回 None。

        动作取值：bind / me / checkin / rank。绑定之外的命令不接受参数，
        群聊里还必须带 / 或 @机器人 才触发——否则「我余额不够了」会被当成指令。
        """
        raw, prefixed = self._normalize(text)
        if not raw:
            return None

        for name in self._command_names():
            arg = self._match_name(raw, name, allow_glued=True)
            if arg is None:
                continue
            # 群聊里只发「绑定」两个字不插话，避免日常聊天被误触发。
            if arg == "" and event is not None and not self._is_private(event):
                return None
            return ("bind", arg)

        # 长名优先，避免「榜单」被「榜」提前截断
        for name in sorted(SELF_COMMANDS, key=len, reverse=True):
            arg = self._match_name(raw, name, allow_glued=False)
            if arg is None:
                continue
            if arg.strip():
                # 「余额怎么办」这类不算命令，当作聊天放过
                return None
            if event is not None and not self._is_private(event) and not prefixed:
                return None
            return (SELF_COMMANDS[name], "")

        return None

    def _parse_command(self, text: str, event=None):
        """兼容旧接口：只返回绑定参数，非绑定命令一律 None。"""
        parsed = self._parse_any(text, event)
        if parsed is None or parsed[0] != "bind":
            return None
        return parsed[1]

    async def _handle_bind(self, event: AstrMessageEvent, code: str = ""):
        """外层兜底。

        插件抛出的异常会被 AstrBot 的钩子链路放大——社区里有插件因为一个未捕获
        异常把整个 AstrBot 进程带崩。这里保证：内部怎么炸，用户至少收到一句可读
        的话，异常绝不向上冒泡。
        """
        try:
            async for result in self._handle_bind_inner(event, code):
                yield result
        except Exception as exc:  # pragma: no cover - 防御性
            logger.error(f"[{PLUGIN_NAME}] bind handler crashed: {exc!r}")
            try:
                yield event.plain_result("处理绑定时出错了，请稍后再试一次。")
            except Exception:
                pass

    async def _handle_bind_inner(self, event: AstrMessageEvent, code: str = ""):
        code = (code or "").strip()
        qq = self._sender_qq(event)

        if not qq:
            yield event.plain_result("拿不到你的 QQ 号，请私聊机器人使用本命令。")
            return

        # 诊断永远可用，不依赖是否已配令牌
        if code in ("诊断", "diag"):
            status, body = await self._call("GET", "/api/qq/agent/ping")
            yield event.plain_result(self._diagnostic_text(event, qq, status, body))
            return

        if not self._cfg("agent_token"):
            yield event.plain_result(
                "机器人还没配置服务令牌，请联系站点管理员在面板填写（qq_agent_token）。\n"
                "排查：发「绑定 诊断」可查看插件版本与站点连通性。"
            )
            return

        # 自检：/绑定 测试
        if code in ("测试", "test", "ping"):
            status, body = await self._call("GET", "/api/qq/agent/ping")
            ok = status == 200 and bool(body.get("success"))
            if ok:
                data = body.get("data") or {}
                yield event.plain_result(
                    "站点连接正常。免费模型绑定要求："
                    + ("开启" if data.get("require_qq_free") else "关闭")
                )
            else:
                yield event.plain_result(self._error_text(status, body))
            return

        remaining = self._cooldown_remaining(qq)
        if remaining > 0:
            yield event.plain_result(f"操作太快了，请 {remaining} 秒后再试。")
            return

        if not code:
            yield event.plain_result(
                "用法：/绑定 <绑定码>　（直接发「绑定 <绑定码>」也可以）\n"
                "绑定码请到面板「幸运大转盘 → QQ 绑定」生成，10 分钟内有效。\n"
                "自检：/绑定 测试　诊断：/绑定 诊断"
            )
            return

        status, body = await self._call(
            "POST", "/api/qq/agent/verify", {"code": code, "qq": qq}
        )
        if status == 200 and body.get("success"):
            data = body.get("data") or {}
            username = data.get("username") or "未知"
            self._mark_attempt(qq, True)
            yield event.plain_result(
                f"绑定成功 ✅\n站点账号：{username}\nQQ：{data.get('qq', qq)}\n"
                "现在可以回面板抽奖、使用免费模型了。"
            )
            return

        self._mark_attempt(qq, False)
        yield event.plain_result(self._error_text(status, body))

    # ------------------------------------------------------- self-service 命令

    def _self_cooldown_remaining(self, qq: str) -> int:
        last = self._last_self.get(qq)
        if last is None:
            return 0
        wait = last + self._window("self_cooldown_seconds", SELF_COOLDOWN_DEFAULT) - time.time()
        return int(wait) + 1 if wait > 0 else 0

    @staticmethod
    def _fmt_quota(value) -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "0"
        return f"{number:,}"

    @staticmethod
    def _fmt_usd(value) -> str:
        try:
            return f"${float(value):.4f}"
        except (TypeError, ValueError):
            return "$0.0000"

    async def _handle_self(self, event: AstrMessageEvent, command: str):
        """自助查询的外层兜底：任何异常都不许冒泡到 AstrBot。"""
        try:
            async for result in self._handle_self_inner(event, command):
                yield result
        except Exception as exc:  # pragma: no cover - 防御性
            logger.error(f"[{PLUGIN_NAME}] self handler crashed: {exc!r}")
            try:
                yield event.plain_result("查询出错了，请稍后再试一次。")
            except Exception:
                pass

    async def _handle_self_inner(self, event: AstrMessageEvent, command: str):
        # 防御：handler 只该由 command_router 调，但万一别处直接调进来，
        # 也要保证群聊里的裸「余额」不会触发（这里重新过一遍解析即可）。
        if self._parse_any(getattr(event, "message_str", "") or "", event) is None:
            return
        qq = self._sender_qq(event)
        if not qq:
            yield event.plain_result("拿不到你的 QQ 号，请私聊机器人使用本命令。")
            return
        if not self._cfg("agent_token"):
            yield event.plain_result(
                "机器人还没配置服务令牌，请联系站点管理员（qq_agent_token）。"
            )
            return

        remaining = self._self_cooldown_remaining(qq)
        if remaining > 0:
            yield event.plain_result(f"查得太勤了，{remaining} 秒后再试。")
            return
        self._last_self[qq] = time.time()

        if command == "rank":
            status, body = await self._call("GET", "/api/qq/agent/rank?limit=10")
            if status != 200 or not body.get("success"):
                yield event.plain_result(self._error_text(status, body))
                return
            data = body.get("data") or {}
            items = data.get("items") or []
            if not items:
                yield event.plain_result("今天还没有人用模型，榜是空的。")
                return
            lines = [f"今日用量榜（{data.get('date', '')}）"]
            for item in items:
                lines.append(
                    f"{item.get('rank')}. {item.get('username')}　"
                    f"{self._fmt_quota(item.get('quota'))}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if command == "checkin":
            status, body = await self._call(
                "POST", "/api/qq/agent/checkin", {"qq": qq}
            )
            if status != 200 or not body.get("success"):
                yield event.plain_result(self._error_text(status, body))
                return
            data = body.get("data") or {}
            if data.get("bound") is False:
                yield event.plain_result(
                    "这个 QQ 还没绑定账号。先到面板生成绑定码，再发「绑定 <码>」。"
                )
                return
            if data.get("already"):
                yield event.plain_result("今天已经签过了，明天再来。")
                return
            yield event.plain_result(
                f"签到成功 ✅ 获得 {self._fmt_quota(data.get('quota_awarded'))} 额度"
                f"（约 {self._fmt_usd(data.get('quota_usd'))}）\n"
                f"当前余额：{self._fmt_usd(data.get('quota_usd'))}"
                f"（{self._fmt_quota(data.get('quota'))} 额度）"
            )
            return

        # command == "me"
        status, body = await self._call("GET", f"/api/qq/agent/me?qq={qq}")
        if status != 200 or not body.get("success"):
            yield event.plain_result(self._error_text(status, body))
            return
        data = body.get("data") or {}
        if data.get("bound") is False:
            yield event.plain_result(
                "这个 QQ 还没绑定账号。先到面板生成绑定码，再发「绑定 <码>」。"
            )
            return

        limit = data.get("daily_limit") or 0
        used = data.get("used_today") or 0
        if limit:
            state = "已达上限，明天 0 点重置" if used >= limit else f"还剩 {self._fmt_quota(limit - used)}"
            usage_line = f"今日用量：{self._fmt_quota(used)} / {self._fmt_quota(limit)}（{state}）"
        else:
            usage_line = f"今日用量：{self._fmt_quota(used)} 额度（未设上限）"

        if not data.get("checkin_enabled"):
            checkin_line = "今日签到：站点未开启"
        elif data.get("checked_in_today"):
            checkin_line = "今日签到：已签到"
        else:
            checkin_line = (
                f"今日签到：未签到（可得 {self._fmt_quota(data.get('checkin_min'))}"
                f"~{self._fmt_quota(data.get('checkin_max'))}，发「签到」领取）"
            )

        yield event.plain_result(
            f"站点账号：{data.get('username')}\n"
            f"余额：{self._fmt_usd(data.get('quota_usd'))}"
            f"（{self._fmt_quota(data.get('quota'))} 额度）\n"
            f"{usage_line}\n"
            f"{checkin_line}"
        )

    def _diagnostic_text(self, event: AstrMessageEvent, qq: str, status: int, body: dict) -> str:
        """一条消息里给出排查「连不上 / 没反应」所需的全部事实。"""
        token = self._cfg("agent_token")
        if token:
            tail = token[-4:] if len(token) >= 4 else "****"
            token_text = f"已配置（尾号 {tail}）"
        else:
            token_text = "未配置"
        if status == 200 and body.get("success"):
            ping_text = "HTTP 200 success"
        else:
            ping_text = f"失败（{body.get('error_kind') or f'HTTP {status}'}）"
        env_bits = ["容器内" if self._inside_container() else "宿主机直跑"]
        env_bits.append("绕过代理" if self._flag("bypass_proxy", True) else "走系统代理")
        if self._flag("insecure_skip_verify", False):
            env_bits.append("跳过证书校验")
        if self._loopback_trap():
            env_bits.append("⚠ 回环地址在容器里不可用")
        return (
            f"插件 v{PLUGIN_VERSION} 在线\n"
            f"命令名：{' / '.join(self._command_names())}\n"
            f"站点：{self._base_url()}\n"
            f"令牌：{token_text}\n"
            f"连通：{ping_text}\n"
            f"环境：{'，'.join(env_bits)}\n"
            f"重试：最多 {self._attempt_budget()} 次　超时：{self._cfg('timeout_seconds', 10)}s\n"
            f"会话：{'私聊' if self._is_private(event) else '群聊'}　发送者 QQ：{qq}\n"
            + (
                ""
                if status == 200 and body.get("success")
                else self._error_text(status, body) + "\n"
            )
            + "若这条能收到，说明插件本身工作正常；命令没反应通常是没加 / 或插件未加载。"
        )

    # ------------------------------------------------------------------ helpers

    def _error_text(self, status: int, body: dict) -> str:
        if status == 0:
            return self._network_error_text(body)

        if status == 429:
            # 站点限流中间件只回状态码 + Retry-After，没有 JSON body
            return ERROR_HINTS["QQ_AGENT_RATE_LIMITED"]
        code = str(body.get("code") or "")
        if code in ERROR_HINTS:
            return ERROR_HINTS[code]
        message = body.get("message") or body.get("msg") or ""
        if status == 503:
            return ERROR_HINTS["QQ_AGENT_UNAVAILABLE"]
        if status == 401:
            return ERROR_HINTS["QQ_AGENT_UNAUTHORIZED"]
        return f"操作失败（HTTP {status}）{('：' + message) if message else ''}"

    def _network_error_text(self, body: dict) -> str:
        """把网络层失败翻译成用户/管理员能直接照着做的提示。

        用户看到第一行就够；管理员按后面几行动手。回环地址 + 容器的组合
        单独出一条，因为它是「明明站点活着却永远连不上」的头号原因。
        """
        kind = str(body.get("error_kind") or "unknown")
        raw = str(body.get("message") or "").strip()
        base = self._base_url()

        if self._loopback_trap():
            return (
                "站点连接失败：插件跑在容器里，127.0.0.1 指的是容器自己，不是宿主机。\n"
                f"请把 api_base 改成宿主机地址（当前：{base}），例如：\n"
                "  · 同一 compose 网络：http://new-api:3000（用服务名）\n"
                "  · Linux 宿主机网关：http://172.17.0.1:3000\n"
                "  · 或公网/内网真实域名：https://api.example.com"
            )

        hint = NETWORK_HINTS.get(kind, NETWORK_HINTS["unknown"])
        lines = [f"站点连接失败（{kind}）：{base}", hint]
        if raw:
            lines.append(f"底层错误：{raw[:160]}")
        lines.append("发「绑定 诊断」可查看完整连通性信息。")
        return "\n".join(lines)


def _safe_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except Exception:
        return {"message": raw[:200]}


def _inside_container() -> bool:
    """判断当前进程是否跑在容器里。

    两个探针都做，任命中即可：`/.dockerenv` 是 Docker 打的标记，
    cgroup 里出现 docker/kubepods 说明是 Docker/K8s 运行时。
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
        with open("/proc/self/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return any(mark in content for mark in ("docker", "kubepods", "containerd"))
    except Exception:  # pragma: no cover - 非 Linux 或权限受限
        return False


def _classify(exc) -> str:
    """把网络异常归到少数几类，供上层选文案。

    urllib 的异常层级很浅（URLError 只有一个 reason），所以这里既吃
    原始异常，也吃 `URLError.reason`。
    """
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in str(exc):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
        return "tls"
    if isinstance(exc, ConnectionResetError):
        return "reset"
    text = str(exc).lower()
    if "name or service not known" in text or "nodename" in text:
        return "dns"
    if "refused" in text:
        return "refused"
    if "certificate" in text:
        return "tls"
    return "unknown"


# 每类网络失败对应一句「照着改就行」的话。{base} 会替换成配置里的站点地址。
NETWORK_HINTS = {
    "refused": "站点地址可达但没人监听。检查 api_base 的端口是否正确、站点是否已启动。",
    "dns": "域名解析失败。检查 api_base 拼写、DNS 配置，或改用 IP 地址。",
    "timeout": "请求超时。站点可能在海外或负载过高，可在插件配置里调大 timeout_seconds。",
    "tls": "HTTPS 证书校验失败。自签证书可在插件配置里打开 insecure_skip_verify。",
    "reset": "连接被对端重置。多为反向代理/网关拦截，检查 Nginx 与防火墙。",
    "unknown": "网络层失败。先用 curl 在机器人所在环境里验证 api_base 是否可访问。",
}
