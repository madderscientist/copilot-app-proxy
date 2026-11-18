# Copilot App 模型 API 发现记录

本文记录本项目在 2026-11-10 至 2026-11-17 的本机调查过程。目标是在保留 VS Code 自身 agent 和工具流程的情况下，使用本人 Copilot App Code 功能已开放的模型。

以下结果仅描述当时版本、本机账号和组织明确允许的实验范围，不代表公开 API 承诺，也不代表其他账号、组织或版本具备相同授权。本文不保存访问令牌、Cookie、网关密钥、账号或租户身份信息。

## 一、最终发现

| 项目 | 实测结果 |
| --- | --- |
| 服务 | Microsoft FX Gateway |
| 主机 | `loki.delve.office.com` |
| 基础路径 | `/api/v1/llmapi/` |
| 模型列表 | `GET /api/v1/llmapi/models` |
| 模型请求 | `POST /api/v1/llmapi/v1/messages` |
| 实测模型 ID | `claude-opus-5.5` |
| 请求格式 | Anthropic Messages 风格 |
| 响应 | JSON，或 `text/event-stream` SSE |
| 认证 | Microsoft Entra Bearer 访问令牌 |
| 已验证 API 版本 | `2026-10-21` |

2026-11-17，通过本地网关请求模型列表返回 `200`，随后提交独立的 `claude-opus-5.5` Messages 请求，返回 `200` 和文本 `OK.`。这证明当时的本地中继能够执行独立推理请求，而不只是观察 Copilot App 的输出。

## 二、识别真正的应用和运行时

最初只读检查了正在运行的进程、父子关系、程序版本、监听端口和活动连接，没有读取完整登录命令行、凭据缓存或进程内存。

发现的主要进程关系如下：

```text
copilotapp.exe
  copilotapphost.exe
    copilot.exe
```

初次调查时，桌面应用版本为 `158.0.4370.12`。Code 运行时位于以下版本化目录：

```text
%LOCALAPPDATA%\Microsoft\Copilot\User Data\CopilotCodeTabRuntime\<版本号>\
```

该目录包含 `copilot.exe`、`github_copilot.dll` 和 `manifest.json`。内置 CLI 的产品信息显示为 GitHub Copilot CLI，应用启动它时使用了 `--server --stdio --no-auto-update`。

这个发现只能说明应用复用了 CLI 引擎，不能说明它和 GitHub Copilot 使用同一登录入口、模型目录或订阅权益。

## 三、排除不能直接使用的入口

### 本地监听端口

宿主确实有若干仅本机可见的监听端口，但对根路径、`/v1/models` 和无效 Messages 请求的探测返回了 `404` 或 `405`，没有验证出可直接调用的标准模型接口。

结论：存在本地端口，不等于应用公开了模型 API。端口号也是动态的，不应写死成接口地址。

### Chromium 调试入口

曾按授权重启应用，尝试普通 Chromium 远程调试参数，并检查 `/json/version`。该次尝试没有形成可访问的调试端口，因此没有沿这条路径取得请求详情。

这只描述本机当时的结果，不能据此认定所有版本都禁止调试，也没有证据把它归因于特定组织策略或证书绑定。

### 公开 CLI 登录与 SDK

公开帮助中的 `copilot login` 使用 GitHub OAuth，`copilot app` 也指向 GitHub Copilot 应用。SDK 能通过 JSON-RPC 驱动 CLI，但这不等于可以复用 Microsoft Copilot App 的 Microsoft 账号登录。

manifest 中出现过 `minimum_host_external_cli_helper_protocol_version`，但没有找到可供外部程序使用的 Microsoft 账号登录调用方式。这个字段是协议兼容线索，不足以当作公开登录桥接接口。

## 四、从安装包中找到后端线索

对已安装程序做静态字符串检查，分别查看了普通字符串和 UTF-16 字符串，没有访问用户账户数据。

关键线索包括：

- `CodeTabRuntime`、`codetabProxy` 等 Code 功能标识。
- BYOK 推理后端、模型路由和宿主 `TokenProvider` 相关信息。
- `https://loki.delve.office.com/api/v1/llmapi` 等后端地址。
- `/v1/chat/completions`、`/v1/messages` 等协议路径。

这些线索表明，Code 引擎可能由桌面宿主提供认证和模型路由。但二进制中出现的路径可能是通用代码、备用后端或未启用功能，不能单独证明实际使用了哪个接口。

## 五、观察真实 HTTPS 请求

在得到用户许可后，使用独立 Python 环境中的 mitmproxy 做进程级 HTTPS 观察：

1. 代理只监听 `127.0.0.1:19323`。
2. 临时 CA 只加入当前用户根证书库，不修改系统代理。
3. 仅为本次启动的 Copilot App 进程树设置代理。
4. 先用不带账号信息的 HTTPS 请求验证链路，再由用户在 Code 页选择模型并发送很小的测试消息。

初始观察插件只记录域名、路径、模型 ID、认证类型和响应状态等脱敏元数据，没有保存完整流量或原始凭据。

### 遇到的一次误判

一次测试中，应用正常回复，但代理没有收到模型请求。检查实际进程后发现，真正运行的实例并未带上预期代理参数，仍在直连。

之后结束目标应用进程树，通过明确的子进程环境重新启动，再查看实际网络进程是否连接本地代理，才确认请求进入了观察链路。

这说明：应用能回复、启动命令执行成功、入口进程带参数，都不能单独证明模型流量已经经过代理。

### 首次捕获的成功调用

真实 Code 请求提供了以下证据：

```text
POST https://loki.delve.office.com/api/v1/llmapi/v1/messages
model: claude-opus-5.5
Authorization: Bearer <不记录>

响应：200
Content-Type: text/event-stream
```

请求体出现了 `model`、`messages`、`tools`、`stream`、`max_tokens` 和 `system`。这为直接转发 Messages 协议、保留 VS Code 自己的工具执行流程提供了依据，但还不能证明所有工具调用和完整 agent 工作流均兼容。

## 六、区分用户登录与独立客户端授权

通过白名单元数据确认，访问令牌由 Microsoft Entra 签发，目标资源为：

```text
42c8f7bd-c7a8-4ba9-bc1c-91edfb716247
```

只读目录查询进一步确认资源名称为 **Microsoft FX Gateway**，并公布了启用的 `LLM.Read` 委托权限，类型为 `User`。这不代表任意新客户端一定能取得权限，仍可能受到租户和服务策略限制。

### Azure CLI 路线

本机 Azure CLI 已登录，但为目标资源获取令牌时返回 `AADSTS65002`。官方错误说明表明，此请求中的第一方客户端缺少目标资源要求的预授权。

正确结论是：**Azure CLI 这条获取令牌的路径未获准**。不能因此认定用户没有模型权限，也不能推出所有独立集成方式都不可能。

### 独立应用注册路线

在用户明确同意后，尝试创建只声明 `LLM.Read` 的单租户公共客户端。创建请求被拒绝，错误为：

```text
ServiceManagementReference field is required for Create, but is missing in the request.
```

这表明当前租户创建应用还需要真实的服务归属信息。该尝试没有创建成功，也没有生成客户端密钥。后来用户要求暂停注册路线，因此未继续修改目录配置。

项目保留了可选 MSAL 登录代码和最小权限清单，但它们不是最终透明中继方案的前置步骤。

## 七、实现本人会话的本机令牌中继

用户明确确认符合组织授权后，才实现了当前令牌中继：

```text
Copilot App → 本机 HTTPS 代理 → 原模型 API
                    ↓ 仅目标 API 的访问令牌
               本机网关的内部接口
                    ↓
             内存缓存与 DPAPI 加密缓存

VS Code → 本机网关 → 附加当前令牌 → 原模型 API
```

Copilot App 仍负责本人账号登录和获取新令牌。插件只处理目标模型 API 请求中的访问令牌，不收集 Cookie 或刷新令牌，也不以应用身份自行执行刷新。

本机网关只接受来自 loopback 且不带浏览器 Origin 的请求，调用方无需密钥；令牌接收接口使用每次启动随机生成的内部密钥，只有本次运行的插件持有。VS Code 不接收云端访问令牌。访问令牌保存在内存和当前 Windows 用户范围的 DPAPI 加密缓存中，不以明文落盘或写入日志。

令牌过期时，需要 Copilot App 取得新令牌，并通过后续请求让中继更新缓存。中继并不保证应用关闭后还能无限续期。

## 八、按服务端错误补齐协议条件

取得令牌后，并没有立即获得成功响应。2026-11-17 的实际排查过程如下：

| 阶段 | 观察到的响应 | 得出的结论或下一步 |
| --- | --- | --- |
| 初期不带凭据直接探测 | `400 ClientTypeMissing` | 服务先检查客户端类型头，不能把这个 400 当成没有鉴权 |
| 带已观察到的客户端头，但不带令牌 | `401 AuthTokenMissing` | 仍需要有效的目标资源 Bearer 令牌 |
| 中继已取得令牌，提交最小请求 | `400 missing_api_version` | 需要 `X-Llm-Api-Version` |
| 使用未经验证的 `2024-06-01` | `400 unsupported_api_version` | 该值不可用；服务返回了当时支持的版本集合 |
| 使用 `2026-10-21` | 模型列表 `200`；Messages 返回 `missing_conversation_id` | 版本有效，但模型请求还需要 `X-ConversationId` |
| 补齐会话 ID 后再次请求 | 模型列表和 Messages 均为 `200` | 最小调用链路打通，模型回复 `OK.` |

服务当时给出的 API 版本集合为 `2026-09-22`、`2026-09-28`、`2026-10-16`、`2026-10-21`。项目选择了其中实测成功的 `2026-10-21`，不是从静态字符串中确认的默认版本。

最终代理使用的关键请求头如下。表中区分了服务明确报缺失的字段和随原请求观察到、但未逐一验证必要性的字段。

| 请求头 | 来源与证据 |
| --- | --- |
| `Authorization: Bearer <不记录>` | 原应用请求中观察到；匿名请求返回 `AuthTokenMissing` |
| `X-ClientType: Copilot` | 观察到的客户端标识；缺失时服务返回 `ClientTypeMissing` |
| `X-ClientScenario: Code` | 原应用中观察到，代理沿用；未单独验证是否必需 |
| `X-Llm-Api-Version: 2026-10-21` | 根据服务错误提示选择，并经模型列表和推理请求验证 |
| `X-ConversationId` | 模型请求缺失时被拒绝，代理默认生成 UUID，也允许调用方传入 |
| `X-ClientCallId` | 观察到请求标识，代理按请求生成 UUID；未单独验证是否必需 |
| `anthropic-version: 2023-06-01` | 原 Messages 请求中观察到，代理作为默认值沿用 |
| `anthropic-beta` | 原请求中观察到；代理仅在调用方提供时转发，不固定启用实验功能 |

这些头部说明的是当时的协议要求，不代替 Bearer 令牌，也不会赋予额外权限。

## 九、验证范围与未完成事项

已验证：

- Copilot App 的真实模型请求使用上述域名、路径和模型 ID，并返回成功的 SSE 响应。
- 本人会话的访问令牌在当时条件下可以用于本机网关发起的独立模型列表和最小 Messages 请求。
- 本地测试覆盖请求体和工具定义原样转发、SSE 首段及时到达、认证隔离、错误透传、DPAPI 缓存及中继失败后的重试。
- 经本机网关对 `claude-opus-5.5` 实测的推理选项和上限：
  - `GET models` 返回 `capabilities.limits.max_context_window_tokens`（1000000）和 `capabilities.supports.reasoning_effort`（`low`、`medium`、`high`、`xhigh`、`max`），不返回最大输出。
  - `max_tokens` 为 128000 时成功，128001 起返回 400 `invalid_body`。
  - `output_config.effort` 接受上述五档，其他值返回 400；`max` 档的思考 token 明显增多。
  - `thinking` 只接受 `{"type": "adaptive"}`，`enabled` 加 `budget_tokens` 与 `disabled` 均返回 400；省略时响应仍包含思考块。
  - `v1/messages/count_tokens` 返回 404 `NonExistentApi`，上游不提供该接口。

未作保证：

- 所有账号、组织、设备或未来应用版本都支持同样的调用方式。
- API 是公开、稳定或不受客户端与组织政策限制的服务。
- VS Code 的完整 agent 工作流、所有工具和图片输入都已端到端验证。
- 上述上限与推理选项同样适用于其他模型或未来版本。
- 健康检查显示缓存令牌未过期，就意味着上游一定接受它。

## 十、当前代码与后续维护

当前架构和日常启动、停止、VS Code 配置见 [README.md](README.md)。主要实现位于：

- [relay_addon.py](copilot_app_proxy/relay_addon.py)：定向提取访问令牌并推送到本机内部接口，成功后才标记去重。
- [gateway.py](copilot_app_proxy/gateway.py) 与 [relay_store.py](copilot_app_proxy/relay_store.py)：模型请求入口、令牌缓存、必要协议头和 JSON/SSE 转发。
- [dpapi.py](copilot_app_proxy/dpapi.py)：Windows 当前用户范围的加密读写。
- [launcher.py](copilot_app_proxy/cli/launcher.py) 与 [windows.py](copilot_app_proxy/cli/windows.py)：进程级代理、临时证书和应用启动方式的管理。

后续排查应先区分三层：本机代理是否运行、目标令牌是否仍可用、服务协议是否变化。不要仅凭健康检查或某个静态字符串就认定模型调用已经成功，也不要把临时 API 错误直接归因于账号无权限。