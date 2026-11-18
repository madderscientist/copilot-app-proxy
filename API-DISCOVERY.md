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
| 模型请求 | Claude：`POST /api/v1/llmapi/v1/messages`；GPT：`POST /api/v1/llmapi/responses` |
| 实测模型 ID | `claude-opus-5.5` 等 Claude 系列；`gpt-5.6-sol` 等 GPT 系列 |
| 请求格式 | Anthropic Messages 风格；OpenAI Responses 风格 |
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

令牌过期时，需要 Copilot App 取得新令牌，并通过后续请求让中继更新缓存。中继并不保证应用关闭后还能无限续期。如何在不读取应用凭据的前提下让应用自己续期，见第九节。

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
| `anthropic-version: 2023-06-01` | 原 Messages 请求中观察到，代理作为默认值沿用；随 Responses 请求一起发送时未见影响 |
| `anthropic-beta` | 原请求中观察到；代理仅在调用方提供时转发，不固定启用实验功能 |

这些头部说明的是当时的协议要求，不代替 Bearer 令牌，也不会赋予额外权限。

## 九、令牌续期调查

2026-11-18 调查。目标是不读取应用的凭据缓存或进程内存，只通过 UI 操作让 Copilot App 自己发出带新令牌的请求。

### 参考方案与不可行的路线

[caozhiyuan/copilot-api](https://github.com/caozhiyuan/copilot-api) 能自行续期，是因为它持有长期的 GitHub OAuth token，可以按 `refresh_in` 反复调用 `copilot_internal/v2/token` 换取短期令牌。本项目只截获到短期的 Bearer 访问令牌；刷新凭据在 Copilot App 或 Windows WAM/MSAL 手中。自行刷新就必须读取这些缓存，或冒用第一方应用的 client_id，越过了第七节的凭据边界，因此不采用。

### 观察方法

`copilot-proxy start --observe` 让插件把发往 `loki.delve.office.com` 的请求元数据追加到 `.local/logs/observe.jsonl`：时间、方法、路径（去掉查询参数）、状态码、令牌 SHA-256 的前 12 位，以及 JWT 的 `iat`/`nbf`/`exp`。不记录令牌、其他声明或正文。UI 操作通过 Windows UI Automation 完成：Code 页是 `TabItem`，支持 `SelectionItemPattern`；模型选择器等按钮支持 `ExpandCollapsePattern`。

### 哪些操作会带出模型 API 令牌

同一主机上有两类令牌：`/api/v2/configuration` 使用的令牌与 `/api/v1/llmapi/` 使用的令牌不同（`tokenId` 和 `exp` 都不同），前者不能用于模型 API。

| 操作 | 是否请求 `llmapi` |
| --- | --- |
| 应用启动（默认停在 Chat 页） | 否，只请求 `configuration` |
| 启动后第一次选中 Code 页 | 是，`GET /api/v1/llmapi/models`，约 2–10 秒内连续 3 次 |
| Code 页发送消息 | 是（最初截获令牌的方式；消耗额度并留下会话） |
| 再次切换 Chat / Code 页 | 否 |
| 展开模型选择器 | 否 |
| 新建会话、新建项目会话、切换运行位置 | 否 |
| 结束 `copilot.exe` 运行时，再进入 Code 页（运行时被重新拉起） | 否 |
| `F5`、`Ctrl+R` | 否，页面不重新加载 |
| 应用闲置 28 分钟（14:16:42–14:44:51，停在 Code 页） | 否，主机上没有任何请求 |
| 应用闲置到模型令牌过期（14:50–15:49） | 否，见下文“闲置到期实验” |
| 选中 Cowork 页 | 否（15:51，仅 `--proxy-server` 启动，观察链路不完整，结论待复核） |

未测试：打开已有历史会话、实际切换模型（只展开过模型菜单）。

由此可得不消耗额度的触发方式：在代理下重启 Copilot App，再选中 Code 页。

### 应用何时换新令牌

应用会复用缓存中的令牌，只在快到期时换新：

| 时间 | 令牌 | 剩余有效期 | 结果 |
| --- | --- | --- | --- |
| 14:13 | `configuration`，`exp` 14:19:57 | 约 7 分钟 | 沿用 |
| 14:16:34 | 同上 | 约 3.5 分钟 | 换新，`exp` 15:34:04 |
| 14:04–14:17 多次 | `llmapi`，`exp` 14:48:44 | 30–44 分钟 | 均沿用 |
| 14:44:43 | 同上 | 240 秒 | 换新，`exp` 15:45:58；20 秒后网关完成更新 |

两类令牌行为一致，换新区间约为到期前 5 分钟，与 MSAL 的默认刷新提前量相符。新令牌的 `iat` 比实际签发时间早约 5 分钟，有效期约 66–92 分钟（`exp` 减 `iat`）。

### 实现与验证

据此实现了 `copilot-proxy refresh` 和 `copilot-proxy keepalive`（[keepalive.py](copilot_app_proxy/cli/keepalive.py)）。`keepalive` 在剩余不足 240 秒时重启应用并选中 Code 页，以网关 `/healthz` 中的 `tokenExpiresOn` 是否变化判断成功；未拿到新令牌时每 60 秒重试，最多 5 次。14:44:43 的实测在到期前完成续期，续期后 `/v1/models` 返回 200。

调查中发现的问题：

- 第一版读取了 `/healthz` 中不存在的 `hasToken` 字段（实际为 `upstreamAuthenticated`），导致在令牌还剩 44 分钟时连续重启；已修正并补充测试。
- 一次重启后 60 秒内没有找到 Code 页，下一次重试恢复。

### 闲置到期实验（2026-11-18）

14:50 停止 `keepalive`，观察日志已有 56 行，模型令牌于 15:45:58 到期。15:49 检查时，日志没有新增请求，网关 `/healthz` 返回 `upstreamAuthenticated: false`、`expiresInSeconds: 0`，到期时间仍为原值。

因此，本次实验没有观察到 App 在闲置到期时主动发送可供中继更新令牌的请求。不能由此断言 App 内部没有刷新凭据，也不能推广为所有版本都不会自动刷新。

随后一次恢复命令在中止前关闭了 App，但尚未重新启动，原有的“不重启”实验现场已丢失。

### `llmapi` 请求不是网页发出的

15:50 只给 `copilotapp.exe` 传入 `--proxy-server`（未设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量）重新启动：页面的 `configuration` 请求经过了代理，Code 页也正常显示了模型列表，但中继没有捕获任何 `llmapi` 请求。16:05 改用 `copilot-proxy start`（同时设置命令行参数和环境变量）启动，进入 Code 页后立即捕获到 3 次 `GET /api/v1/llmapi/models`，令牌恢复可用。

由此推断，模型 API 请求由读取环境变量代理设置的子进程（`copilotapphost.exe` 或 `copilot.exe`）发出，Chromium 的 `--proxy-server` 只影响页面。启动器必须同时设置两者；只走 `--proxy-server` 的观察链路不完整，在这种链路下看不到请求不能说明 UI 操作没有触发请求。

### 运行方式的取舍

续期最终做成与网关、mitmproxy 并列的独立后台进程，由 `start` 启动、`stop` 结束；`start` 默认留在前台显示三者的日志和一行状态栏，`--detach` 时启动后即返回，两种方式运行的内容相同。为避免清理不完整时残留的续期进程反复重启 App，续期进程连续约 30 秒连不上网关就自行退出。

`stop` 只关闭由 `start` 打开的 Copilot App，不再以直连方式重新打开：该进程的代理设置在启动时固定，代理停止后无法继续使用；而在本项目里 App 主要是令牌来源，停止后通常不再需要。这样也省去了查找程序路径、重新打开失败时保留记录重试等逻辑。

前台运行时直接关闭终端窗口，Windows 只给约 5 秒：程序先结束续期进程（避免它重启 App），再关闭 Copilot App，最后结束网关和代理；临时 CA 的删除需要确认，留给之后的 `stop`。

已知限制：续期会重启应用，应用内正在进行的任务会中断；锁屏、休眠或需要重新登录时无法自动续期。

### 待验证

- 在模型令牌已过期、App 未重启且完整观察链路仍有效时，打开历史会话、实际切换模型等操作是否触发新令牌请求。仅展开模型菜单与实际切换模型不能混为一谈。
- 独立续期进程能否在到期前完成续期，尚未端到端实测。

## 十、验证范围与未完成事项

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
- 对 `claude-opus-5`、`claude-sonnet-5.5`、`claude-sonnet-5`、`claude-haiku-5.5` 重复了 adaptive 思考加 `effort=max` 请求，以及 `max_tokens` 128000/128001 边界测试，结果与 `claude-opus-5.5` 一致。模型列表中这些模型的上下文窗口和强度档位与 `claude-opus-5.5` 相同。
- GPT 系列（`gpt-5.6-sol`、`gpt-5.6-luna`、`gpt-6-luna`、`gpt-61-sol`）的 `supported_endpoints` 只有 `/responses`，使用同一令牌和请求头：
  - 上游路径为 `responses`（不带 `v1/`）。`v1/responses` 和 `v1/chat/completions` 返回 404；`chat/completions` 返回 400 `endpoint_model_mismatch`。网关把本地 `/v1/responses` 映射到上游 `responses`。
  - 上下文窗口为 1050000；强度档位为 `none` 至 `max`，`gpt-61-sol` 没有 `none`。四个模型在 `reasoning.effort=max`、`max_output_tokens=128000` 下均返回 200，档位不支持时返回 400。
  - `max_output_tokens` 为 128001、200000、1050001、5000000 时仍返回 200，上游不校验该字段，真实输出上限未知。
  - SSE 流式正常，事件序列为标准 Responses 事件，另含自定义事件 `llmapi.call_cost`。
  - 经本机网关验证了 VS Code 形态的请求（`store:false`、`include:["reasoning.encrypted_content"]`、`truncation`、`context_management` 压缩、`text.verbosity`、`prompt_cache_options`）和 Codex 形态的请求（`instructions`、`parallel_tool_calls`、`reasoning.summary`、`prompt_cache_key`）；包含工具调用和回传 `function_call_output` 的两轮对话均成功。

未作保证：

- 所有账号、组织、设备或未来应用版本都支持同样的调用方式。
- API 是公开、稳定或不受客户端与组织政策限制的服务。
- VS Code 的完整 agent 工作流、所有工具和图片输入都已端到端验证；Codex CLI 未在本机实测。
- 上述上限与推理选项同样适用于未来的模型或版本。
- 健康检查显示缓存令牌未过期，就意味着上游一定接受它。

## 十一、当前代码与后续维护

当前架构和日常启动、停止、VS Code 配置见 [README.md](README.md)。主要实现位于：

- [relay_addon.py](copilot_app_proxy/relay_addon.py)：定向提取访问令牌并推送到本机内部接口，成功后才标记去重。
- [gateway.py](copilot_app_proxy/gateway.py) 与 [relay_store.py](copilot_app_proxy/relay_store.py)：模型请求入口、令牌缓存、必要协议头和 JSON/SSE 转发。
- [dpapi.py](copilot_app_proxy/dpapi.py)：Windows 当前用户范围的加密读写。
- [launcher.py](copilot_app_proxy/cli/launcher.py) 与 [windows.py](copilot_app_proxy/cli/windows.py)：进程级代理、临时证书和应用启动方式的管理。
- [keepalive.py](copilot_app_proxy/cli/keepalive.py)：`refresh` / `keepalive`，通过重启应用并选中 Code 页让应用自己续期。
- [foreground.py](copilot_app_proxy/cli/foreground.py)：`start` 的前台视图，汇总各进程日志并显示状态栏。

后续排查应先区分三层：本机代理是否运行、目标令牌是否仍可用、服务协议是否变化。不要仅凭健康检查或某个静态字符串就认定模型调用已经成功，也不要把临时 API 错误直接归因于账号无权限。