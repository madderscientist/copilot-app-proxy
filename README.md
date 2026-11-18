# Copilot App 本机代理

把 Microsoft Copilot 桌面应用（下称 Copilot App）能用的模型，以本地 API 的形式提供给 VS Code、Codex CLI 等程序：Claude 系列走 Messages 协议，GPT 系列走 Responses 协议。代理只转发请求和流式响应；工具调用、确认和对话管理仍由调用方（如 VS Code）负责。

> 仅限组织允许的本人账号使用。本项目不是微软官方集成，不提供账号、订阅或令牌；能否使用取决于你的账号、组织和 Copilot 版本。API 的调查过程与已验证范围见 [API-DISCOVERY.md](API-DISCOVERY.md)。

## 工作原理

```text
Copilot App ──HTTPS──▶ 本机 mitmproxy ──▶ 模型 API
                            │ 截取访问令牌
                            ▼
VS Code ──────────▶ 本机网关（附上令牌）──▶ 模型 API
```

1. `start` 让 Copilot App 经本机代理联网。App 用你的账号正常请求模型 API 时，代理截取其中的访问令牌交给网关。
2. VS Code 等程序向本机网关发请求，网关附上该令牌转发给模型 API，并原样返回（含 SSE 流）。
3. 令牌约 1–1.5 小时过期。自动续期进程在快到期时重启 Copilot App 并打开 Code 页，让 App 自己取得新令牌。

代理不读取 App 的登录缓存，也不自行刷新令牌；登录和续签始终由 Copilot App 完成。

## 快速开始

### 1. 环境要求

- Windows 10/11（依赖 DPAPI 和 UI 自动化，不支持 macOS/Linux），PowerShell 5.1 或更高版本；不需要管理员权限。
- Python 3.12 或更高版本。
- 已安装、已登录、能正常使用 Code 页的 Copilot App（GitHub Copilot 插件或网页版不能替代）。
- 能访问 PyPI（或组织提供的包源）和模型服务。

### 2. 安装

```powershell
git clone https://github.com/madderscientist/copilot-app-proxy.git
Set-Location copilot-app-proxy
.\install.ps1
```

脚本会找到 Python 3.12+，在项目内创建 `.venv/` 并安装依赖。它不会改动证书、代理设置或 Copilot。

- 找不到 Python 时指定完整路径（不能用 Windows 应用商店的占位入口）：`.\install.ps1 -PythonExecutable 'D:\Python\python.exe'`。
- 使用组织镜像：`.\install.ps1 -PythonIndexUrl <pip simple 地址>`。不要在地址中写入密码或令牌。
- PowerShell 禁止运行脚本时：`py -3 -m copilot_app_proxy.cli install`（镜像参数为 `--index-url`）。

之后的命令都通过 `.\.venv\Scripts\copilot-proxy.exe` 执行，下文简写为 `copilot-proxy`；`copilot-proxy --help` 可列出全部命令。

### 3. 启动

先结束 Copilot App 中正在进行的任务，然后运行：

```powershell
.\.venv\Scripts\copilot-proxy.exe start
```

`start` 依次完成：

1. 启动网关（`127.0.0.1:19324`）和 HTTPS 代理（`127.0.0.1:19323`）。
2. 把本次生成的临时 CA 加入**当前用户**的受信任根证书。Windows 可能弹出确认，需要你点确定。
3. 重启 Copilot App，仅让这个 App 进程走代理；系统代理不变。
4. 自动打开 App 的 Code 页，App 随即请求模型列表，网关拿到令牌（不消耗额度），终端显示 `Token: ready`。
5. 启动自动续期，然后留在终端：上方滚动显示日志，空一行后是固定在底部的状态栏（窗口标题同步显示令牌剩余时间）：

```text
15:42:10 [gateway] POST /v1/messages model=claude-opus-5.5 stream=true -> 200 headers=1.20s total=8.40s
15:44:43 [keepalive] Token has 240s left; relaunching Copilot App (attempt 1).
15:45:03 [gateway] Token updated: exp=2026-11-18T07:45:58Z

 Token 61 min left (expires 16:45, renews ~16:41)  keep-alive on | Ctrl+C to stop
```

日志每行依次为时间、来源（`gateway` 网关、`mitm` 代理、`keepalive` 自动续期）和内容。网关为每个 API 请求记一行：方法、路径、模型、是否流式、状态码、响应头和总耗时，不含令牌、提示词或回复。

终端里会用颜色区分：来源各有颜色；状态码 2xx/3xx 为绿色、4xx 为黄色、5xx 为红色；令牌更新为绿色，重试、沿用旧令牌等提示为黄色，错误为红色。状态栏第一块显示令牌状态，底色为绿色（正常）、黄色（即将续期）或红色（没有可用令牌或网关无响应）；后面是自动续期状态（停止时显示黄色的 `keep-alive STOPPED`）和停止提示，用 `|` 分隔。请求是否成功直接看上方日志里状态码的颜色。设置环境变量 `NO_COLOR` 可关闭颜色。

按 **Ctrl+C** 停止：结束所有进程、移除临时 CA（Windows 会弹出删除确认），并让 Copilot App 恢复直连。

### 4. 接入 VS Code

在 VS Code 聊天的模型管理中选择 **Add Models → Custom Endpoint**，填入以下命令输出的 JSON：

```powershell
.\.venv\Scripts\copilot-proxy.exe vscode-config
```

模型配置的细节见[客户端配置](#客户端配置)。

## 日常使用

### 命令一览

| 命令 | 作用 |
| --- | --- |
| `start` | 启动代理并留在终端显示日志，Ctrl+C 停止 |
| `stop` | 停止代理、移除临时 CA、恢复 Copilot App；也用于清理异常退出后的残留 |
| `status` | 查看网关、代理、自动续期、临时 CA 和令牌的状态 |
| `refresh` | 立即重启 Copilot App 并打开 Code 页，尝试取得新令牌 |
| `vscode-config` | 生成 VS Code 的 Custom Endpoint 配置 |
| `doctor` | 检查 Python、mitmdump、Copilot App 路径和端口占用 |
| `keepalive` | 在当前终端运行自动续期；仅在以 `--no-keepalive` 启动后需要 |

### `start` 的选项

| 选项 | 作用 |
| --- | --- |
| `--detach` | 启动完成后立即返回，代理在后台运行，之后用 `stop` 停止 |
| `--no-keepalive` | 不启动自动续期 |
| `--observe` | 调查用：把 Copilot App 发往模型服务的请求元数据写入 `.local/logs/observe.jsonl`（不显示在终端），见[日志](#日志) |
| `--gateway-port` / `--proxy-port` | 临时改用其他端口，见[端口与路径](#端口与路径) |
| `--copilot-path` / `--mitmproxy-path` | 指定 `copilotapp.exe` 或 `mitmdump.exe` 的位置 |

网关、代理和自动续期都是独立的后台进程，前台的 `start` 只负责显示它们的日志和在退出时清理。因此 `--detach` 与默认方式功能相同，区别只是终端不会留下来。

### 令牌续期

自动续期默认开启。它每分钟查看一次令牌剩余时间，不足 240 秒时重启 Copilot App 并打开 Code 页。

- **为什么要等快到期**：App 会一直复用缓存中的令牌，只在剩余约 5 分钟以内才换新（实测，见 API-DISCOVERY 第九节）。过早重启只会拿回旧令牌。
- **只等着会续期吗**：本次闲置到过期的实验未观察到 App 发出新令牌请求，网关最终返回 `401`，因此目前不能依赖闲置自动续期。
- **失败怎么办**：没拿到新令牌时每 60 秒重试，连续 5 次失败后停止续期，网关届时返回 `401`。可手动运行 `refresh`，或在 Code 页发一条消息。
- **代价**：每次续期都会重启 Copilot App，App 里正在进行的任务会中断；VS Code 的对话不受影响。
- **限制**：锁屏、休眠或 App 需要重新登录时无法续期。
- **不会残留**：续期进程连续约 30 秒连不上网关就自行退出，不会在代理停止后继续重启 App。

`refresh` 在令牌还有较长有效期时会显示 `reused cached token` 并返回 1，这是正常现象。

### 停止与异常退出

```powershell
.\.venv\Scripts\copilot-proxy.exe stop
```

在前台按 Ctrl+C 等同于 `stop`。移除临时 CA 时 Windows 会弹出确认；若取消，`stop` 会报错并保留记录，可再次执行。停止后会保留加密的令牌缓存 `.local/token.dpapi`，供下次启动使用。

| 情况 | 结果与处理 |
| --- | --- |
| 直接关闭终端窗口 | 程序会结束后台进程，并让 Copilot App 恢复直连；但来不及移除临时 CA。之后 `status` 会提示运行 `stop` |
| 注销或关机 | 进程都会结束，下次登录 App 正常直连；临时 CA 仍在，运行 `stop` 清理 |
| 后台进程被强行结束 | Copilot App 会因连不上代理而断网。运行 `stop`，或完全退出 Copilot App 后重新打开 |

临时 CA 留在受信任列表里不影响使用，但其私钥（`.local/mitm-ca/`）一旦泄露，就可能被用来伪造网站证书，所以应及时用 `stop` 清理。

同一项目的启动和停止不能同时进行；提示“Another start or stop is in progress”时，稍后重试即可。

### 状态检查

```powershell
.\.venv\Scripts\copilot-proxy.exe status   # 各进程、临时 CA 和令牌是否就绪
.\.venv\Scripts\copilot-proxy.exe doctor   # 启动失败时检查环境与端口
```

### 日志

各进程的日志写入 `.local/logs/`：`gateway.log`、`mitm.log`、`keepalive.log`，以及记录异常输出的对应 `.err.log`。每次 `start` 会清空它们；`gateway.log` 在运行中超过 5 MB 时转存为 `gateway.log.1`（只保留这一个备份），因此长期运行也不超过约 10 MB。使用 `--detach` 时在这里查看。

日志文件本身不含颜色代码。终端被重定向（例如输出到文件）时不显示状态栏和颜色，只输出纯文本日志。

`--observe` 写入的 `observe.jsonl` 每行记录 Copilot App 的一个请求：时间、方法、路径（去掉查询参数）、状态码、令牌 SHA-256 的前 12 位，以及 JWT 的 `iat`/`nbf`/`exp`。不包含令牌本身、其他声明或请求内容。主要用于研究 App 何时换新令牌。实时查看：

```powershell
Get-Content .local\logs\observe.jsonl -Wait -Tail 0
```

### 端口与路径

长期改用其他端口时，编辑项目根目录的 [config.json](./config.json)（`gateway_port`、`proxy_port`）。端口的优先级为：命令行参数 > 正在运行的代理所记录的端口（仅 `status`、`vscode-config` 等查询命令）> `config.json` > 默认值 `19324` / `19323`。

`start` 优先使用正在运行的 Copilot App 的程序路径，否则查找标准安装位置；都找不到时用 `--copilot-path` 指定。

### 更新与卸载

```powershell
.\.venv\Scripts\copilot-proxy.exe stop
git pull --ff-only
.\install.ps1
```

卸载时先运行 `stop`，确认临时 CA 已移除、Copilot App 已恢复直连，再删除整个项目目录。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 Python | 安装 Python 3.12+ 后重新打开终端，或用 `.\install.ps1 -PythonExecutable <完整路径>` |
| 找不到 mitmdump.exe | 先运行 `.\install.ps1` |
| 找不到 Copilot App | 先打开 Copilot App 再启动，或用 `--copilot-path` |
| 端口被占用 | 停止旧代理，或换用未占用的端口（两个端口不能相同） |
| 未自动打开 Code 页 | 手动点开 Code 页即可取得令牌 |
| 网关返回 `401`（没有令牌或已过期） | 运行 `refresh`，或在 Code 页发一条消息 |
| Copilot App 无法联网 | 运行 `stop`；或完全退出 App 后重新打开 |
| 出现证书错误 | 不要跳过证书校验；运行 `stop` 后重新 `start` |
| 模型不可用、版本头或服务地址变化 | 账号或 App 版本可能不适用，参考 API-DISCOVERY |

## 客户端配置

### VS Code

VS Code 的 Custom Endpoint 不会自己查询模型列表，需要逐个列出模型。`vscode-config` 通过网关读取模型列表，按 `supported_endpoints` 分配接口：Claude 系列用 `/v1/messages`（`apiType: messages`），GPT 系列用 `/v1/responses`（`apiType: responses`）；名称、上下文窗口和思考强度都取自上游。网关未运行或令牌已过期时，会提示并输出内置的模型列表。

默认端口下的输出示例（每个系列只列一个模型）：

```json
[
  {
    "name": "本机 Copilot App 代理",
    "vendor": "customendpoint",
    "apiKey": "local",
    "models": [
      {
        "id": "claude-opus-5.5",
        "name": "Claude Opus 5.5 (本机代理)",
        "url": "http://127.0.0.1:19324/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": false,
        "thinking": true,
        "adaptiveThinking": true,
        "contextWindow": 1000000,
        "maxOutputTokens": 128000,
        "supportsReasoningEffort": ["low", "medium", "high", "xhigh", "max"]
      },
      {
        "id": "gpt-5.6-sol",
        "name": "GPT-5.6 Sol (本机代理)",
        "url": "http://127.0.0.1:19324/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": false,
        "contextWindow": 1050000,
        "maxOutputTokens": 128000,
        "supportsReasoningEffort": ["none", "low", "medium", "high", "xhigh", "max"]
      }
    ]
  }
]
```

`apiKey` 可以填任意非空值，网关不校验，也不会转发。

**思考强度与上限**

- 在 VS Code 模型选择器的 **Effort Level** 中切换强度；传入模型不支持的档位会返回 400。
- **Claude**：以 `"output_config": {"effort": "high"}` 发送，可选 `low` 到 `max`。上游只接受 `"thinking": {"type": "adaptive"}`，`enabled`（带 `budget_tokens`）和 `disabled` 都会返回 400；不传 `thinking` 时同样会自适应思考。
- **GPT**：以 `"reasoning": {"effort": "high"}` 发送，多数模型还支持 `none`（`gpt-61-sol` 不支持）。
- 上下文窗口：Claude 为 1000000，GPT 为 1050000。模型列表不返回最大输出长度：Claude 的 `max_tokens` 超过 128000 会被拒绝；GPT 的 `max_output_tokens` 上游不校验，128000 只是沿用的值，并非已验证的上限。

### Codex CLI

Codex 只支持 Responses 协议，因此只能使用 GPT 模型。在 `~/.codex/config.toml` 中配置（未在本机实测）：

```toml
model = "gpt-5.6-sol"
model_provider = "copilot-app-proxy"

[model_providers.copilot-app-proxy]
name = "Copilot App 本机代理"
base_url = "http://127.0.0.1:19324/v1"
wire_api = "responses"
env_key = "COPILOT_APP_PROXY_KEY"
```

`COPILOT_APP_PROXY_KEY` 环境变量填任意非空值即可。Codex 不查询 `/v1/models`，模型信息来自其内置列表；匹配不到的模型按 272000 的上下文窗口处理。

### 本地接口

| 接口 | 用途 |
| --- | --- |
| `GET /healthz` | 网关状态与令牌到期时间（不返回令牌） |
| `GET /v1/models` | 原样返回上游模型列表 |
| `POST /v1/messages` | Claude 系列，支持 SSE 流式 |
| `POST /v1/responses` | GPT 系列，支持 SSE 流式 |

- `/v1/models` 是 Copilot 自己的格式（`data[].id`、`name`、`supported_endpoints`、`capabilities`），不是 Anthropic 或 OpenAI 的格式；只读取 `data[].id` 的客户端可以直接使用。
- 上游没有 Chat Completions 和 `count_tokens` 接口。GPT 只能走 Responses，`store` 须为 `false`，多轮对话由客户端回传完整上下文；流式响应中会多一个自定义事件 `llmapi.call_cost`，标准客户端会忽略。
- `/healthz` 中的 `upstreamAuthenticated: true` 只表示缓存的令牌按 `exp` 尚未过期，能否调用以模型接口的实际响应为准。
- 上游固定为 `https://loki.delve.office.com/api/v1/llmapi/`。网关会补齐客户端、会话和 API 版本请求头，API 版本默认 `2026-10-21`；上游更新后可能需要调整。

## 安全说明

- **只对本机开放**：网关和代理只监听 `127.0.0.1`。模型接口不需要密钥，但会拒绝带浏览器 `Origin` 头、非本机 `Host` 或带查询参数的请求，网页无法借用。
- **多用户电脑不适用**：同一台电脑上**其他 Windows 用户**的程序也能访问 `127.0.0.1`，从而使用你的模型额度。请勿在多人共用的电脑或服务器上运行。
- **令牌的去向**：只截取发往 `loki.delve.office.com/api/v1/llmapi/` 的请求里的访问令牌，不收集 Cookie 或刷新令牌。令牌只存在于网关内存和 `.local/token.dpapi`（用当前 Windows 用户的 DPAPI 加密，换用户或换电脑无法解密），不会通过本地接口返回，也不写入日志。
- **调用方凭据不外传**：调用方附带的 `Authorization`、`x-api-key`、Cookie 等不会转发给上游；上游只收到中继的令牌和必要的协议头。
- **无法注入伪造令牌**：网关接收令牌的内部接口使用每次启动随机生成的密钥，只有本次运行的代理插件持有，且不写入磁盘。
- **不要分享 `.local/`**：其中包含临时 CA 私钥、令牌缓存和日志；`.gitignore` 已将其排除。

## 开发

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check copilot_app_proxy tests
```

测试只使用模拟请求和临时缓存，不需要真实账号、令牌或模型额度。

### 架构

```mermaid
flowchart LR
  App["Copilot App<br/>本人登录与令牌续签"]
  Client["VS Code / 本机程序"]
  Upstream["模型 API<br/>loki.delve.office.com"]

  subgraph Local["本机（copilot-proxy start）"]
    Relay["mitmproxy :19323<br/>relay_addon.py"]
    subgraph Gateway["网关 :19324 · gateway.py"]
      Ingest["POST /_internal/token<br/>本次运行的内部密钥"]
      API["/v1/messages · /v1/responses · /v1/models"]
      Store["RelayTokenStore<br/>内存 + DPAPI 缓存"]
    end
    Keepalive["自动续期<br/>keepalive.py"]
  end

  App -->|"HTTPS 请求"| Relay -->|"原样转发"| Upstream
  Relay -.->|"访问令牌"| Ingest --> Store
  Client --> API -->|"令牌 + 协议头"| Upstream
  Store -.-> API
  Keepalive -.->|"查询到期时间"| Gateway
  Keepalive -.->|"快到期时重启并打开 Code 页"| App
```

- **启动**：`start` 以独立后台进程启动网关、mitmproxy 和自动续期，输出写入 `.local/logs/`，运行记录写入 `.local/pids.json`，供 `stop` 和 `status` 使用。
- **令牌**：插件把令牌推送到网关的内部接口；推送失败时不标记已送达，下一个请求会重试。网关按 JWT 的 `exp` 判断是否过期，缺失或过期时返回 `401`。
- **转发**：请求体和工具定义不做协议转换；响应用 `aiohttp` 流式转发，保留 SSE 和背压，客户端断开时同时关闭上游请求。

| 模块 | 职责 |
| --- | --- |
| `copilot_app_proxy/gateway.py` | 本机请求校验、路由、请求头组装、JSON/SSE 转发、错误映射与可选的请求日志 |
| `copilot_app_proxy/relay_store.py` | 令牌缓存、到期判断与 DPAPI 持久化 |
| `copilot_app_proxy/relay_addon.py` | mitmproxy 插件：截取令牌推送给网关；可选的观察日志 |
| `copilot_app_proxy/dpapi.py` | 调用 Windows DPAPI 加解密 |
| `cli/__main__.py` | 命令行参数与分发 |
| `cli/launcher.py` | `start` / `stop`：进程编排、证书信任、Copilot App 重启与清理 |
| `cli/foreground.py` | 前台视图：汇总各进程日志并着色、底部状态栏与窗口标题、关闭窗口时的快速恢复 |
| `cli/ansi.py` | 终端颜色：检测是否支持 ANSI、遵循 `NO_COLOR` |
| `cli/keepalive.py` | `refresh` / `keepalive`：重启 App 并打开 Code 页以取得新令牌 |
| `cli/windows.py` | Windows 进程查询、证书操作、启停锁与 Code 页的 UI 自动化 |
| `cli/diagnostics.py` | `status` / `doctor` / `vscode-config` |
| `cli/server.py` | 网关进程入口（内部命令 `serve`），写入并轮转 `gateway.log` |
| `cli/config.py` / `cli/bootstrap.py` | 路径、端口与运行记录；`install` |

核心库（`copilot_app_proxy/` 下除 `cli/` 外的模块）不依赖 `cli`。其他程序可以用 `create_app(relay_store=..., relay_ingest_secret=...)` 和 `listen(app, port)` 嵌入网关；单独加载 mitmproxy 插件时，需要设置 `RELAY_INGEST_URL` 和 `RELAY_INGEST_SECRET` 环境变量，否则插件不会推送令牌。
