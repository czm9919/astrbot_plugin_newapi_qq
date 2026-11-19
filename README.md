# astrbot_plugin_newapi_qq

把 QQ 号绑定到 New API 面板账号的 AstrBot 插件。

用户在面板生成一枚 8 位一次性绑定码，私聊机器人发 `/绑定 <码>`，机器人带着服务令牌调站点接口完成绑定。绑定之后，站点侧的「幸运大转盘抽奖」与「免费模型」两道门禁才放行 —— 这样每个免费额度的领取人都能追到具体 QQ。

- 站点不接触 QQ 协议，只信任机器人给的「谁在发消息」
- 绑定码一次性、10 分钟过期、重新生成作废旧码
- 一个 QQ 只绑一个账号，一个账号只绑一个 QQ
- 插件除 AstrBot 框架外只用标准库（`urllib` + `asyncio`），无第三方依赖

> 依赖站点侧接口 `/api/qq/agent/ping`、`/api/qq/agent/verify`。这两个接口属于配套的 New API 定制分支（`v1.0.0-rc.51+hc37` 起），上游 `QuantumNous/new-api` 没有它们。

## 安装

1. 下载本仓库，把整个目录放进 AstrBot 的 `data/plugins/`：

   ```
   data/plugins/astrbot_plugin_newapi_qq/
   ```

2. 重启 AstrBot（或在 WebUI 插件页点重载）。
3. 在 AstrBot WebUI → 插件 → 本插件 → 配置：

   | 配置项 | 说明 |
   | --- | --- |
   | `api_base` | 站点地址，例如 `https://api.example.com`，**不要**带 `/api` |
   | `agent_token` | 与服务令牌一致，见下方「站点侧准备」 |
   | `bind_command` | 绑定命令名，默认 `绑定`；改了就按 `/你改的名字 <码>` 使用 |
   | `timeout_seconds` | 请求站点超时，默认 10 秒 |

也可以跳过 WebUI 表单，直接写 `data/config/astrbot_plugin_newapi_qq_config.json`：

```json
{
  "api_base": "https://api.example.com",
  "agent_token": "<与服务令牌一致>",
  "bind_command": "绑定",
  "timeout_seconds": 10
}
```

## 站点侧准备

1. 管理员登录面板 → 「幸运大转盘」→「抽奖设置」→「QQ 机器人接入」。
2. 点「生成随机令牌」（或自己填 16–128 个可见 ASCII 字符），保存。
3. 把令牌填进上面插件的 `agent_token`。令牌只显示一次尾号，站点不会回显明文；丢了就轮换一枚。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/绑定 <码>` | 用面板生成的 8 位码完成绑定 |
| `/绑定 测试` | 连通性自检，返回站点是否要求免费模型绑定 QQ |
| `/bind <码>` | 英文别名 |
| `/绑定` | 不带码时回用法提示 |

绑定成功示例：

```
绑定成功 ✅
站点账号：alice
QQ：123****89
现在可以回面板抽奖、使用免费模型了。
```

## 用户流程

1. 面板 →「幸运大转盘」→「生成绑定码」→ 得到 8 位码（10 分钟内有效，一次性）
2. 私聊机器人发 `/绑定 ABCD2345`
3. 机器人回复绑定成功 + 面板用户名
4. 回面板刷新，「QQ 绑定」显示已绑定，可以抽奖 / 用免费模型

## 接口约定

自己对接别的机器人框架也看这里，全部是普通 HTTP：

```
GET  {base}/api/qq/agent/ping
      Header: X-QQ-Agent-Token: <token>
      → 200 {"success":true,"data":{"ok":true,"require_qq_free":true}}
      → 401 {"code":"QQ_AGENT_UNAUTHORIZED"}
      → 503 {"code":"QQ_AGENT_UNAVAILABLE"}   站点未配置令牌

POST {base}/api/qq/agent/verify
      Header: X-QQ-Agent-Token: <token>
      Body:   {"code":"ABCD2345","qq":"123456789"}
      → 200 {"success":true,"data":{"user_id":10,"username":"alice","qq":"123****89"}}
      → 400 {"code":"QQ_BIND_CODE_INVALID"}      码不存在/过期/已用
      → 400 {"code":"QQ_BIND_FAILED"}            QQ 号格式不对
      → 409 {"code":"QQ_ALREADY_BOUND"}          该 QQ 已绑别的账号
      → 409 {"code":"QQ_ACCOUNT_ALREADY_BOUND"}  该账号已绑别的 QQ
```

GET 形式同样支持（方便某些框架）：

```
GET {base}/api/qq/agent/verify?code=ABCD2345&qq=123456789&token=<token>
```

## 错误码 → 用户提示

插件内置映射，用户只会看到中文人话：

| 站点错误码 | 机器人回复 |
| --- | --- |
| `QQ_BIND_CODE_INVALID` | 绑定码无效或已过期，请回面板重新生成（10 分钟内有效）。 |
| `QQ_ALREADY_BOUND` | 这个 QQ 已经绑定到别的账号了，请先在原账号解绑。 |
| `QQ_ACCOUNT_ALREADY_BOUND` | 你的账号已经绑定了别的 QQ，请先在面板解绑。 |
| `QQ_AGENT_UNAUTHORIZED` | 机器人令牌无效，请联系管理员检查配置。 |
| `QQ_AGENT_UNAVAILABLE` | 站点尚未开启机器人接入（未配置服务令牌），请联系管理员。 |

## 安全说明

- 令牌只出现在机器人进程与站点之间，**不要发到群里**；泄露立刻在面板轮换
- 绑定码一次性、10 分钟过期，重新生成会作废旧码
- 建议把绑定命令限制为**私聊**处理，避免用户把码贴进群
- 插件只调用两个公开接口，不读写面板其他数据

## 不装 AstrBot 也能自检

插件本体只用标准库发 HTTP，可以用三个桩模块（`astrbot.api`、`astrbot.api.event`、`astrbot.api.star`）把它跑起来，直接对着站点验证命令解析、令牌校验、错误码映射：

```python
import sys, types, logging
api = types.ModuleType("astrbot.api"); api.logger = logging.getLogger("x")
ev = types.ModuleType("astrbot.api.event")
class _F:
    class EventMessageType: ALL = "all"
    def command(self, _n): return lambda fn: fn
    def event_message_type(self, _t): return lambda fn: fn
ev.filter = _F(); ev.AstrMessageEvent = object
star = types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self, context=None): pass
class Context: pass
star.Star, star.Context = Star, Context
star.register = lambda *a, **k: (lambda cls: cls)
sys.modules.update({"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                    "astrbot.api.event": ev, "astrbot.api.star": star})
# 之后按普通模块加载 main.py，构造 FakeEvent（提供 get_sender_id/plain_result），
# 调插件实例的 _handle_bind(event, code) 即可
```

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 回复「站点连接失败」 | `api_base` 不可达 | 检查地址、防火墙、HTTPS 证书 |
| 回复「还没配置服务令牌」 | 插件侧令牌为空 | 在 WebUI 填 `agent_token` |
| 回复「令牌无效」 | 两边不一致 / 站点轮换过 | 重新复制，注意首尾空格 |
| 回复「绑定码无效」 | 超过 10 分钟 / 已用过 / 重新生成过 | 回面板重新生成 |
| 站点侧生成码报 503 | 站点未配置服务令牌 | 管理员在面板填令牌并保存 |

## 开发

```bash
python -m py_compile main.py   # 语法检查
```

`main.py` 是单文件、无第三方依赖；加命令时沿用 `@filter.command` + `event.plain_result` 即可。

## License

MIT，见 [LICENSE](LICENSE)。
