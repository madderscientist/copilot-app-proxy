# Copilot App 模型 API 发现记录

本文记录本项目的本机调查过程。目标是在保留 VS Code 自身 agent 和工具流程的前提下，使用本人 Copilot App Code 功能已开放的模型。

结果只描述调查时的版本、本机账号和组织允许的实验范围，不代表公开 API 承诺，也不代表其他账号、组织或版本具备相同授权。本文不保存访问令牌、Cookie、密钥、账号或租户身份信息。

## 一、结论

| 项目 | 实测结果 |
| --- | --- |
| 服务 | Microsoft FX Gateway，主机 `loki.delve.office.com`，基础路径 `/api/v1/llmapi/` |
| 模型列表 | `GET /api/v1/llmapi/models` |
| 模型请求 | Claude：`POST .../v1/messages`（Anthropic Messages 风格）；GPT：`POST .../responses`（OpenAI Responses 风格） |
| 响应 | JSON，或 `text/event-stream` SSE |
| 认证 | Microsoft Entra Bearer 访问令牌 |
| API 版本 | `2026-10-21`（实测可用） |

通过本地网关请求模型列表和独立的 Messages 请求均返回 `200`，说明中继能够执行独立推理，而不只是观察 Copilot App 的输出。

## 二、定位接口

**运行时**：只读检查进程后发现，桌面应用 `copilotapp.exe` 下有 `copilotapphost.exe`，再下是 `copilot.exe`。后者位于 `%LOCALAPPDATA%\Microsoft\Copilot\User Data\CopilotCodeTabRuntime\<版本号>\`，产品信息显示为 GitHub Copilot CLI，以 `--server --stdio --no-auto-update` 启动。这只说明 App 复用了 CLI 引擎，不说明它与 GitHub Copilot 共用登录、模型目录或订阅。

**排除的入口**：

- 宿主有若干动态的本机监听端口，但对根路径、`/v1/models` 和 Messages 的探测只返回 `404`/`405`，不是可调用的模型接口。
- 用普通 Chromium 远程调试参数重启应用，没有得到可访问的调试端口。
- 公开的 `copilot login` 走 GitHub OAuth，SDK 也不能复用 Microsoft 账号登录；manifest 里的 `minimum_host_external_cli_helper_protocol_version` 只是协议兼容线索。

**静态线索**：安装包字符串中出现 `CodeTabRuntime`、`codetabProxy`、宿主 `TokenProvider`、`https://loki.delve.office.com/api/v1/llmapi` 和 `/v1/messages` 等路径，提示 Code 引擎由宿主提供认证和模型路由，但不能单独证明实际使用了哪个接口。

**抓包确认**：经用户许可，用 mitmproxy 只对本次启动的 Copilot App 进程做 HTTPS 观察（代理只监听 `127.0.0.1`，临时 CA 只加入当前用户证书库，不改系统代理）。在 Code 页发送一条小消息后捕获到：

```text
POST https://loki.delve.office.com/api/v1/llmapi/v1/messages
model: claude-opus-5.5
Authorization: Bearer <不记录>
响应：200，text/event-stream
```

请求体包含 `model`、`messages`、`tools`、`stream`、`max_tokens` 和 `system`，可以直接转发 Messages 协议并保留 VS Code 的工具流程。

抓包时要确认流量真的经过代理：

- 一次 App 正常回复、代理却没收到请求，原因是实际运行的实例没有带上代理参数。App 能回复、启动命令成功，都不能证明流量经过了代理。
- 只给 `copilotapp.exe` 传 `--proxy-server` 时，页面请求经过代理，但 `llmapi` 请求不经过；同时设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量后才能截到。可见模型 API 请求由读取环境变量的子进程（`copilotapphost.exe` 或 `copilot.exe`）发出，启动器必须两者都设置。

## 三、授权路线

令牌由 Microsoft Entra 签发，目标资源 `42c8f7bd-c7a8-4ba9-bc1c-91edfb716247`，即 **Microsoft FX Gateway**，公布的委托权限为 `LLM.Read`（`User` 类型）。这不代表任意新客户端都能取得权限。

- **Azure CLI**：为该资源获取令牌返回 `AADSTS65002`，即 Azure CLI 这个第一方客户端未获预授权。不能据此认定用户没有模型权限。
- **独立应用注册**：创建只声明 `LLM.Read` 的公共客户端被拒，要求提供 `ServiceManagementReference`。未创建成功，按用户要求停止。

## 四、令牌中继

用户确认符合组织授权后，实现了本人会话的令牌中继：

```text
Copilot App → 本机 HTTPS 代理 → 模型 API
                    ↓ 只取目标 API 的访问令牌
               本机网关内部接口 → 内存与 DPAPI 加密缓存

VS Code → 本机网关 → 附上当前令牌 → 模型 API
```

- 登录和取得新令牌始终由 Copilot App 完成。插件只取目标 API 请求中的访问令牌，不收集 Cookie 或刷新令牌，也不以应用身份自行刷新。
- 网关只接受本机、不带浏览器 `Origin` 的请求；令牌接收接口使用每次启动随机生成的内部密钥。
- 令牌只存在于内存和当前 Windows 用户范围的 DPAPI 缓存，不明文落盘、不写日志，也不交给 VS Code。

## 五、协议条件

取得令牌后，按服务端错误逐步补齐请求头：

| 请求 | 响应 | 结论 |
| --- | --- | --- |
| 不带凭据 | `400 ClientTypeMissing` | 服务先检查客户端类型头 |
| 带客户端头、不带令牌 | `401 AuthTokenMissing` | 需要目标资源的 Bearer 令牌 |
| 带令牌的最小请求 | `400 missing_api_version` | 需要 `X-Llm-Api-Version` |
| 版本 `2024-06-01` | `400 unsupported_api_version` | 服务返回可用版本：`2026-09-22`、`2026-09-28`、`2026-10-16`、`2026-10-21` |
| 版本 `2026-10-21` | 模型列表 `200`，Messages `missing_conversation_id` | 还需要 `X-ConversationId` |
| 补齐会话 ID | 均为 `200` | 调用链路打通 |

网关使用的请求头：

| 请求头 | 依据 |
| --- | --- |
| `Authorization: Bearer <不记录>` | 原请求中观察到；缺失时 `AuthTokenMissing` |
| `X-ClientType: Copilot` | 缺失时 `ClientTypeMissing` |
| `X-ClientScenario: Code` | 原请求中观察到，未单独验证是否必需 |
| `X-Llm-Api-Version: 2026-10-21` | 按服务提示选择并实测 |
| `X-ConversationId` | 缺失时被拒；默认生成 UUID，也接受调用方传入 |
| `X-ClientCallId` | 原请求中观察到，按请求生成 UUID；未验证是否必需 |
| `anthropic-version: 2023-06-01` | 原 Messages 请求中观察到；随 Responses 请求发送无影响 |
| `anthropic-beta` | 只在调用方提供时转发 |

这些请求头只满足协议要求，不代替 Bearer 令牌，也不赋予额外权限。

## 六、令牌续期

目标是不读取应用凭据，只通过 UI 操作让 Copilot App 自己发出带新令牌的请求。

**为什么不自行刷新**：[caozhiyuan/copilot-api](https://github.com/caozhiyuan/copilot-api) 能自行续期，是因为它持有长期的 GitHub OAuth 凭据，可以反复换取短期令牌。本项目只截获到短期的 Bearer 访问令牌，刷新凭据在 Copilot App 或 Windows WAM/MSAL 手中；自行刷新就必须读取这些缓存或冒用第一方应用的 client_id，越过了第四节的凭据边界。

**观察方法**：`start --observe` 把 App 发往 `loki.delve.office.com` 的请求元数据写入 `.local/logs/observe.jsonl`，包括路径、状态码、令牌哈希前缀和 JWT 的 `iat`/`nbf`/`exp`，不含令牌本身。UI 操作用 Windows UI Automation 完成。

### 哪些操作会带出模型 API 令牌

同一主机上有两类令牌：`/api/v2/configuration` 用的令牌与 `/api/v1/llmapi/` 用的不同，前者不能调用模型 API。

| 操作 | 是否发出 `llmapi` 请求 |
| --- | --- |
| 启动应用（默认停在 Chat 页） | 否，只请求 `configuration` |
| **启动后首次选中 Code 页** | **是**，`GET llmapi/models` 连续 3 次 |
| 在 Code 页发送消息 | 是，但消耗额度并留下会话 |
| **本次运行中首次打开某个历史会话** | **是**，`POST llmapi/tools/web/mcp` 2 次 |
| 再次切换 Chat / Code 页、切到 Cowork 页 | 否 |
| 展开模型菜单、实际切换模型 | 否 |
| 新建会话、切换运行位置、再次打开已打开过的会话 | 否 |
| 结束 `copilot.exe` 后再进入 Code 页或打开会话 | 否，查看时不会重新拉起运行时 |
| `F5`、`Ctrl+R` | 否，页面不重新加载 |
| 闲置半小时，或闲置直到令牌过期 | 否，主机上没有任何请求 |

打开历史会话虽然不需要重启 App，但每个会话在每次 App 运行中只触发一次（每个项目对应一个 `copilot.exe` 运行时，首次打开会话时初始化网页工具的 MCP 连接），无法保证到期时还有没打开过的会话，因此没有采用。最终方案是在代理下重启 Copilot App，再选中 Code 页。

### 应用何时换新令牌

App 会一直复用缓存中的令牌，只在快到期时换新：

| 令牌 | 剩余有效期 | 结果 |
| --- | --- | --- |
| `configuration` | 约 7 分钟 | 沿用 |
| `configuration` | 约 3.5 分钟 | 换新 |
| `llmapi` | 30–44 分钟 | 沿用 |
| `llmapi` | 240 秒 | 换新，约 20 秒后网关完成更新 |

两类令牌行为一致：换新区间约为到期前 5 分钟，与 MSAL 的默认提前量相符。新令牌有效期约 66–92 分钟。

闲置到令牌过期的实验中，App 没有发出任何请求，网关最终返回 `401`。不能据此断言 App 内部没有刷新凭据，但可以确定不能靠闲置让中继拿到新令牌。

### 方案与参数

- **续期进程**：与网关、mitmproxy 并列的独立后台进程，由 `start` 启动、`stop` 结束。它每次检查都读取网关当前的到期时间，不预定刷新时刻；期间用户正常使用 App 带出新令牌时，续期自动顺延。连续约 30 秒连不上网关就自行退出，避免残留进程反复重启 App。
- **提前量 240 秒**：一次续期最慢约 2 分钟（重启 App、找 Code 页最多 60 秒、等新令牌最多 45 秒），曾遇到一次 60 秒内没找到 Code 页、靠重试恢复；240 秒能在到期前容纳一次重试，也落在 App 约 5 分钟的换新区间内。
- **`stop` 只关闭 App**：`start` 打开的 App 代理设置在启动时固定，代理停止后无法继续使用，所以要关闭；但 App 在本项目里主要是令牌来源，不再自动以直连方式重新打开。
- **关闭终端窗口**：Windows 只给约 5 秒，依次结束续期进程（避免它重启 App）、Copilot App、网关和代理；删除临时 CA 需要确认，留给之后的 `stop`。

实测结果：

- 续期进程在剩余 240 秒时重启 App，约 14 秒后网关拿到新令牌；续期前后经网关的请求都返回 `200`，对话没有中断。
- 前台视图（日志着色、状态栏、窗口标题）显示正常；Ctrl+C 结束各进程、关闭 App 且不重新打开，并弹出删除临时 CA 的确认。
- 直接关闭终端窗口后各进程均已结束，临时 CA 仍受信任；`status` 提示运行 `stop`，`stop` 确认后完成清理。

已知限制：续期会重启 App，App 内正在进行的任务会中断；锁屏、休眠或需要重新登录时无法自动续期。

## 七、验证范围

已验证：

- Copilot App 的真实模型请求使用上述主机、路径和模型 ID，并返回 SSE。
- 本人会话的访问令牌可用于本机网关发起的独立模型列表和推理请求。
- 自动化测试覆盖请求体和工具定义原样转发、SSE 首段及时到达、认证隔离、错误透传、DPAPI 缓存和中继失败后的重试。
- 图片输入：模型列表不声明视觉能力，但经网关发送 base64 PNG 实测可用。Claude 用 Messages 的 `image` 块（`source.type: base64`），GPT 用 Responses 的 `input_image`（`data:` URL），两者均正确识别图片内容。
- **Claude**（`claude-opus-5.5`、`claude-opus-5`、`claude-sonnet-5.5`、`claude-sonnet-5`、`claude-haiku-5.5` 结果一致）：
  - 模型列表返回上下文窗口 1000000 和强度档位 `low`–`max`，不返回最大输出。
  - `max_tokens` 128000 成功，128001 起返回 400 `invalid_body`。
  - `output_config.effort` 只接受上述五档；`thinking` 只接受 `{"type": "adaptive"}`，`enabled` 加 `budget_tokens` 与 `disabled` 均返回 400，省略时仍有思考块。
  - `v1/messages/count_tokens` 返回 404 `NonExistentApi`。
- **GPT**（`gpt-5.6-sol`、`gpt-5.6-luna`、`gpt-6-luna`、`gpt-61-sol`）：
  - `supported_endpoints` 只有 `/responses`，上游路径为 `responses`（不带 `v1/`）；`v1/responses`、`v1/chat/completions` 返回 404，`chat/completions` 返回 400 `endpoint_model_mismatch`。
  - 上下文窗口 1050000；强度档位 `none`–`max`，`gpt-61-sol` 没有 `none`。
  - `max_output_tokens` 到 5000000 仍返回 200，上游不校验，真实上限未知。
  - SSE 为标准 Responses 事件，另有自定义事件 `llmapi.call_cost`。
  - VS Code 形态（`store:false`、`include:["reasoning.encrypted_content"]`、`truncation`、`context_management`、`text.verbosity`、`prompt_cache_options`）和 Codex 形态（`instructions`、`parallel_tool_calls`、`reasoning.summary`、`prompt_cache_key`）的请求，以及带工具调用和 `function_call_output` 的两轮对话均成功。

不作保证：

- 其他账号、组织、设备或未来版本支持同样的调用方式。
- 这是公开、稳定、不受客户端与组织政策限制的服务。
- VS Code 的完整 agent 工作流和所有工具都已端到端验证；图片只验证了 base64 PNG，未覆盖 URL 引用、其他格式和大图；Codex CLI 未在本机实测。
- 上述上限与推理选项适用于未来的模型或版本。
- 健康检查显示令牌未过期，上游就一定接受它。

## 八、Chat 页的模型（未接入）

Chat 页的模型菜单里有 Code 页没有的模型，调查时有 Fable 5.1（Anthropic）和 Grok 4.6（xAI）。结论是这些模型不能用本项目的转发方式接入。

**调查方法**：

- 用 UI 自动化读取模型菜单；解码 App 的 HTTP 缓存，查看页面脚本和菜单数据。
- 用缓存令牌直接请求 `llmapi`。
- 抓包：临时扩大 `--observe` 的范围，记录 App 发往所有主机的请求和 WebSocket 帧，只保留主机、路径和 `tone`、`target` 等标识字段，不含消息正文和令牌。然后在 Chat 页分别用 Fable 5.1 和 Grok 4.6 各发一条消息，结束后插件已还原。

| 项目 | 实测结果 |
| --- | --- |
| 服务 | M365 Copilot 的 Sydney 会话服务：WebSocket `wss://substrate.svc.cloud.microsoft/m365Copilot/Chathub/<用户ID>@<租户ID>`（SignalR 协议，发消息时调用 `chat`，服务端用 `update` 推送回复） |
| 是否经过 `llmapi` | 否。两次发送期间没有任何发往 `loki.delve.office.com/api/v1/llmapi/` 的请求 |
| 模型标识 | 不传模型 ID，在 `chat` 调用的 `tone` 字段里填菜单项 ID：Fable 5.1 为 `Claude_Fable`，Grok 4.6 为 `Grok_Reasoning`。菜单数据中还有 `Claude_Container`（Opus 5.5）、`Gpt_61_Sol_Reasoning` 等。同一调用还带有约 40 个 `optionsSets` 功能开关（网页、工作数据检索、代码解释器、记忆等） |
| 计费 | 菜单数据标有 `usageBasedBillingEnabled` 和 `serviceMeterName: PremiumModelsInM365Chat`，界面也提示会消耗额度 |
| `llmapi` 模型列表 | `X-ClientScenario` 改为 `Chat`、`Cowork` 等，返回的列表不变；可用的 API 版本也仍是第五节列出的四个 |
| 直接调用 `llmapi` | `claude-fable-5.1` 返回 403 `model_not_allowed`（"not available on the coding-agent gateway"）；`fable-5.1`、`grok-4.6` 返回 400 `invalid_model_family` |

可见 `llmapi` 是 Code 页专用的网关，模型有白名单。Chat 页的 Sonnet 5.5、Opus 5.5、GPT-5.6 Sol、GPT-6.1 Sol 在 `llmapi` 中有对应模型，可以直接使用。

不另做 Sydney 转换器的原因：

- **协议不对应**：Sydney 是会话接口，不接受调用方定义的工具，也不能替换系统提示。VS Code agent 依赖的工具调用无法实现，只能做有损的纯文本转换。
- **不是模型原始输出**：回复会经过微软的系统提示、Work IQ 检索和引用处理。
- **维护成本更高**：需要截取另一种令牌，并模拟网页客户端的私有协议，比现有中继更容易随版本失效。

## 九、代码与维护

架构、日常使用和客户端配置见 [README.md](README.md)。主要实现：

- [relay_addon.py](copilot_app_proxy/relay_addon.py)：截取访问令牌并推送到网关内部接口；可选的观察日志。
- [gateway.py](copilot_app_proxy/gateway.py)、[relay_store.py](copilot_app_proxy/relay_store.py)：模型请求入口、令牌缓存、协议头和 JSON/SSE 转发。
- [dpapi.py](copilot_app_proxy/dpapi.py)：当前 Windows 用户范围的加密读写。
- [launcher.py](copilot_app_proxy/cli/launcher.py)、[windows.py](copilot_app_proxy/cli/windows.py)：进程编排、临时证书、App 启动方式与 UI 自动化。
- [keepalive.py](copilot_app_proxy/cli/keepalive.py)：`refresh` / `keepalive`。
- [foreground.py](copilot_app_proxy/cli/foreground.py)：`start` 的前台视图。

排查时先区分三层：本机代理是否运行、令牌是否仍可用、服务协议是否变化。不要仅凭健康检查或某个静态字符串认定调用会成功，也不要把临时 API 错误直接归因于账号无权限。
