# 原生搜索与媒体接入

施工与核查日期：2026 年 10 月 14 日，北京时间。

本改动直接使用官方 Codex、Grok Build 和 MiniMax Code worker。没有 DSH
执行依赖，也没有增加供应商专属 MCP、通用媒体框架或任意 URL 下载器。
交付目标是同机原生证据和本次文件。用户已授权各一次图片、5 秒动画和搜索；
用户随后确认账号均已登录、使用订阅、不再要求费用确认；已通过现有 Grok
登录与 CLI 默认模型完成三项各一次实测。详见末尾账号 C 级记录。

## 基线与版本

- 官方 Agent Bridge：从 `https://github.com/Fei2411/Agent-Bridge.git`
  fetch `main`，实际 HEAD 为 `5560e065f5aa7d7d5155cd17a28baa231208b7ed`，
  包版本 `0.1.0`，与施工说明的 Bridge 基线相同。
- 本地起点：`214cc6f781be0dd737fda3ef90ba7bac8cbb571e`。保留 fork
  的 TaskNode 归属、诊断、transcript、files_changed provenance 等功能。
  未整体合并上游其他重构；对照官方实现补齐本次需要的 `config_overrides`。
- 仓库根和已获取的官方树未提供 `AGENTS.md`；遵循用户当次提供的全局指导。
  既有未跟踪 `.worktrees/` 保留。
- Codex PATH 版本：`0.162.1`；本次 Bridge 的命令解析实际优先选择 Desktop `codex.exe` `0.162.0-alpha.17.2`。核对官方当前源码
  `ca2b076fffbc24ad5fa57258ece8a8277b326138` 和对应 release
  `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`。
- Grok 本机版本：`1.0.50 (c58f321264ba)`。
- MiniMax 官方 npm 包与 `mcode --version`：`0.6.9`。
  PATH 上的桌面 `mcode-tools --version` 是 `0.0.6`，这不是 npm 包版本。
- ACP Python 依赖核查版本：`agent-client-protocol 0.12.1`。
  使用其原生 stream observer 保留 schema 会丢弃的 vendor 工具身份字段。

另外从 Bridge 实际解析到的 Desktop `0.162.0-alpha.17.2` 直接运行
`app-server generate-json-schema`，没有启动服务或调用模型；
initialize response、thread/start、turn/start 和 imageGeneration completion
的代表 payload 均通过该 schema 验证。这是目标版本的 A 级 schema 证据，
不是 native turn、账号条件、落盘路径或图片成功的 C 级证据。
核查 executable SHA-256：
`D13914CED6C7AF174D8D638DB938284231227312104B06A25E9608C632A7DAEC`。

上述均为当日核查结果，不能用作“永远最新”或账号能力证明。未读取、复制、
导出凭据，未登录、修改全局 feature、调用真实模型或进行付费生成。
参考插件采用 SATA 2.1；未复制其实现代码，协议依据优先来自官方源码、
安装包及原生文档。插件 fixtures 仅作为明确注明的历史形状参考。

## 使用方式

普通编程继续使用 `dispatch_task`；其原有文本搜索路径保持兼容。
能力扩展提供直接 MCP 入口，不要求创建 Codex 聊天或管理 Bridge 线程：

- `call_capability`：传入供应商、`search/image/video`、提示词、工作目录及首次请求 UUID，
  立即返回 `call_id`。
- `get_capability_result`：用同一 `call_id` 读取结果；`wait_sec` 为 0 至 60。
  `pending=true` 表示仍在运行，继续读取，不重新提交。
- `cancel_capability`：用同一 `call_id` 取消本机调用。
  远端是否完成或计费仍以原生证据为准。

例如一次 5 秒动画：

```json
{
  "agent": "grok",
  "capability": "video",
  "prompt": "Animate the explicitly authorized test image with a slow camera orbit.",
  "cwd": "<absolute-authorized-workspace>",
  "duration_sec": 5,
  "request_id": "<fresh-UUID-used-for-all-retries>",
  "user_requested": true
}
```

此参数传给 `call_capability`。支持的接入组合为 Codex 搜索/图片、
Grok 搜索/图片/视频、MiniMax 搜索；接入存在不代表当前账号已成功实测。
调用方不传 `session_id`，返回也不要求操作 Bridge session。
每次新能力调用使用一个内部 session；`list_sessions` 默认隐藏它们。
诊断调用可传 `include_internal=true`，返回里才包含 `internal` 字段。
旧状态没有该标记时，只有该 session 上保留的任务全部都是能力任务才会补标；
混有普通任务的 session 保持普通。公开 `dispatch_task` 不能把内部 session
继续成编程会话。进程仍在调用结束后立即关闭，原生 ID 保留以便后续恢复。

显式继续传 `previous_call_id`。它必须指向同一供应商、同一工作目录、
已完成且结果已完全结束的能力调用，并且是该 session 的最新任务。
失败、取消、不确定、仍在运行或已被裁剪的调用不能恢复。
Grok 的图片到图片、图片到视频，以及 MiniMax/Codex 的搜索到搜索，复用同一内部 session。
Codex 图片继续使用新的一次性 thread，并把校验后的本地图片作为 `localImage` 输入。
视频到视频、搜索到图片，以及跨供应商继续都会拒绝。会话正忙或上一次不是最新任务时拒绝，
不另开分支。原生恢复失败时直接报错，不为这次继续新建会话。

`input_images` 最多四张，必须是工作目录内的 png、jpeg 或 webp，
相对路径会规范化后保存；拒绝链接、junction、穿越和 URL。
搜索不接受图片。未显式提供时，媒体继续使用上一次成功图片输出，并在提交前及执行前再次校验。
搜索继续不需要文件。

图片和视频要求恰好一个原生成功文件。搜索要求至少一次 `web_search`，
可以同时使用 `web_fetch`，来源必须指向这些原生 ID，且不能伴随媒体或 job。
结果附 `native_calls` 的工具类型与 call ID；一次搜索可以返回多条来源，
只抓取页面不算搜索，观察到两次媒体操作不能验收为一次成功。
视频从 MP4 `moov/mvhd` 读取电影时间线，允许与请求时长相差最多 0.05 秒；
不匹配会报告失败并保留已生成文件，不自动重投。
搜索提示词禁止生成媒体、委派、改源码和脚本代替原生检索。
图片和视频提示词禁止额外生成、搜索、自动重试、委派及脚本代替原生工具。
这是文本驱动约束及结果验收，不能冒充供应商端的硬调用预算。
**接口不是供应商执行前的硬次数限额；不能声称提供永久严格 exactly-once 保证。**
本次受控实测已经完成，实际原生操作数为搜索、生图、视频各一次，没有重投。
Grok 1.0.50 官方权限文档表明 `PreToolUse` 可以显式拒绝，但 hook 超时、
崩溃或格式错误是 fail-open；`--max-turns` 限制模型回合而非原生工具调用次数。
因此本次没有将提示词、事后计数或 fail-open hook 描述成严格执行前限额。
视频若需要参考图，只能使用已经明确授权传给供应商的测试文件；
没有输入时应报告缺口，不能顺便再生成一张图。

没有原生结果时，公共返回省略 `sources`、`output_files` 和 `job`。

Codex 的 `call_capability(capability="search")` 仅在该调用中配置实时搜索，
不修改 worker 或全局配置。旧 `dispatch_task` 路径也可以通过项目/worker 配置显式传入：

```toml
[agents.codex]
config_overrides = ['web_search="live"']
```

这是配置片段；先对齐已有配置文件中的 Codex section，不覆盖其他字段。
此次施工没有修改本机全局配置。未传覆盖值时，exec 的搜索默认行为保持原样。
每任务 model/effort 的参数排在 worker overrides 之后，保留原来的优先级。

Codex 图片推荐 `call_capability(capability="image")`。
为兼容既有任务调用，仍保留 `dispatch_task` 的窄操作：

```json
{
  "agent": "codex",
  "message": "Use the native image generation tool to generate one small illustration.",
  "cwd": "<absolute-authorized-workspace>",
  "operation": "image",
  "request_id": "<fresh-UUID-used-for-all-retries>"
}
```

只允许 Codex，且要求 fresh Bridge session；不允许携带 `session_id` 恢复图片
操作。普通 exec/resume 不经过这个分支。模型、账号及可能的生成费用必须得到
用户明确授权；worker 的自动批准模式和 `user_requested` 不等于付费授权。
提示词仍是文本驱动；不保证运行时会使用图片工具，也不保证一个 turn 内只调用
一次生成。没有原生成功文件时返回失败，不把模型声称的成功提升为成功。

图片通过官方 `app-server`，不是 exec JSONL。`app-server` 不支持
`--ignore-user-config`，会按其官方规则解析现有配置；本分支支持 `-c` overrides，
不自动更换 provider、登录方式、model 或 feature。官方默认开启的
`image_generation` 仍受账号套餐、provider actor authentication 和 model
能力条件限制。未满足条件时报告缺口，不自动绕过。

Grok 搜索和媒体仍走 ACP；没有新增一次性 CLI fallback，因尚无账号证据证明
ACP 缺失这些能力。MiniMax 继续 `mcode acp`；不会把图片/audio prompt block
冒充为 ACP 0.6.9 支持的输入。

## 返回与文件安全

```json
{
  "sources": [
    {"title": "Native result", "url": "https://example.com/doc", "tool_call_id": "call_1"}
  ],
  "output_files": [
    {
      "path": ".agent-bridge/outputs/task_fixture/<unique-name>.png",
      "mime": "image/png",
      "bytes": 69,
      "sha256": "<SHA-256-of-delivered-bytes>",
      "tool_call_id": "call_2"
    }
  ],
  "job": {
    "provider": "codex",
    "native_session_id": "<native-thread-id>",
    "native_turn_id": "<native-turn-id>",
    "status": "completed",
    "remote_cancel_supported": false
  }
}
```

这是结构示例，不是一次真实生成的验收产物。所有路径相对 `task.cwd`，
客户端在同机以 `cwd / path` 打开。文件 MIME 与后缀取自实际内容，不信任供应商
传入后缀。单文件上限 256 MiB，单任务最多 16 个文件，来源最多 64 条；
来源的 URL、标题、snippet 和关联 ID 均有长度限制。

- Codex 仅接受匹配 thread、turn、item 的 `item/completed`，
  `imageGeneration.status == "completed"` 和非空 `savedPath`。
  `initialize.codexHome` 必须匹配实际 worker home，保存路径必须精确等于
  `generated_images/<本次 thread>/<官方 sanitize(item-id)>.png`。
- Grok 通过 `toolName` 识别工具，MiniMax 通过 `name`；不使用自由文本 title。
  只有本次 native session 的成功工具结果参与提取，历史 replay 不激活收集器。
- Grok 媒体仅接受明确原生变体、匹配的 filename/session_folder 和本次
  session 的 `images`/`videos` 直属文件。长 cwd 分组必须通过官方 `.cwd`
  marker 绑定；不能从工具给出的任意外部路径扩大读取范围。
- 拒绝路径穿越、越界、符号链接、Windows junction/reparse point、
  非普通文件、空文件、超限文件，以及 turn 开始前已有的文件名，即使旧文件
  后来被覆盖。预扫描是拒绝旧文件的 deny-set，不是成功发现机制。
- 检查文件头/容器基本结构、可读性、大小及 SHA-256；复制后重新核对。
  PNG/JPEG/WebP/MP4 的检查不是完整图像解码或视频播放验收。
  文件名碰撞不会覆盖或删除已有交付；复制失败只清理本次创建的文件。
- base64 不进入媒体 checkpoint、state 或 transcript。Codex 原生图片的
  `result` 在读取边界丢弃；成功 ACP 工具只保存白名单元数据，未知原文不入库。
  不导出 token、认证头、凭据路径或临时签名 URL。

搜索 URL 来自原生结果或原生 open/find/fetch 动作目标；后者只能证明工具
访问目标，不能证明其内容准确。最终模型文字中的链接不会生成 `sources`。
Codex 当前 `results` 可选且内容开放；只提取已认识的 `text_result`。
Grok citation 的字符串和 `{title,url}` 形状均有离线防御性测试，不将 fixture
形状等同于本账号的实际 wire capture。
MiniMax 的官方 ACP `rawOutput` 等于运行时 `tool.output`；官方 consumer 接受
results 中的 `url`/`href`/`link`。兼容已知 JSON body 和 `content[].text`
封装，但该搜索响应封装尚未经过当前账号实测。

## 中断、重启和恢复

`BridgeHome/native-results/<task_id>.json` 保存有界、无凭据的本次 checkpoint。
写入与读取共享 2 MiB 总字节上限，避免多次搜索累计的合法 Unicode 来源
在恢复时被另一个更小的上限拒绝。
带 request_id 或图片 operation 的任务在进入 worker 前确认 task/fingerprint
已写入 state；写入失败不发送 prompt。checkpoint 对 PermissionError 有三次
有界元数据写入尝试，不涉及 provider 生成重试。持续写入失败明确记录
checkpoint_error/uncertain，不用旧 journal 静默覆盖内存已观察结果。
Codex 在发 `turn/start` 前记录运行状态，取得 turn ID 后记录关联；
Grok 记录本次媒体工具开始和明确完成。原生完成后文件结果立即持久化，
无需等待最终 turn 文本。
多个媒体调用逐文件 checkpoint，总状态汇总所有已观察失败与未完成调用；
部分成功文件可以保留，但不把混合失败的总任务标为成功。

进程断线、本地取消或重启且缺少原生完成时，记录 `uncertain`。已有明确完成
的文件可以保留，即使之后的 turn response 丢失；Bridge task 本身仍报告
传输失败。文件校验/复制失败为交付失败，不自动再次生成。
`remote_cancel_supported` 在当前 Codex/Grok 路径始终是 false；
本地 `turn/interrupt` 或 process reap 不证明远端生成终止、不会计费。

启动只恢复 metadata 并重新验证已交付文件；不调用生成接口，不扫描任意
目录来补造完成证据。Grok/Codex 没有已确证的远端媒体 job query API，
因此 uncertain 需要用户对已有 native session 作人工决定。

新派发的 `request_id` fingerprint 随保留任务进入 state。重启后相同 ID 和
原始参数复用同一任务，包括 uncertain 任务；不同参数被拒绝。旧任务若没有
fingerprint、任务已经被清理、或其他正在运行的 Bridge 实例，不在这个保证内。
这不是供应商幂等，也不能阻止 agent 在同一 turn 内重复生成。

MiniMax 的 `submit_video_generation/query_video_generation` connector helper
没有在本次实现：官方 skill/Bash 路径仍受 product capability、
`beta.mcodeTools` 和官方 launcher/broker 就绪条件限制，本次未验证其可用性。
没有真实 taskId 时不构造远端 job 或 polling API，不自动启用 beta。
未来若授权并确认可靠恢复需求，应先核对官方 schema，再实现提交 ID 持久化、
同 ID 查询与受限下载；下载失败只能重试下载/查询，不能重投生成。

## 能力证据

A 是源码/安装包证据，B 是离线 fixture/模拟进程与文件测试，C 是授权账号实测。
**Grok 已有 C 级证据：原生搜索、生图、5 秒视频各一次，来源、文件与视频时长
通过独立校验。Codex 与 MiniMax 保持 A/B；没有扩大测试矩阵。**

| 能力 | 本次 A/B 范围 | 未验收条件 |
| --- | --- | --- |
| Codex 搜索 | 官方 exec results/action；原生 URL 与 call ID 提取、兼容回归 | 当前账号真实搜索与来源质量 |
| Codex 图片 | 官方 app-server schema/path；模拟完整 turn、关联校验、文件交付和故障恢复 | 账号/provider/model/套餐与真实生图 |
| Grok 搜索 | A/B 加本次账号一次搜索，citations 字符串形状现场确认 | 更多查询形状与长期运行 |
| Grok 图片/编辑/视频 | A/B 加本次一次生图及一次 5 秒视频；同 session 文件回收与解码 | 编辑、其他时长/输入组合 |
| Grok web_fetch | 历史 Content fixture 的防御性兼容 | 1.0.50 二进制未完整确证 Content 形状；本机 feature 未明确启用 |
| MiniMax 搜索 | 官方 ACP serializer + 官方 URL consumer；results/content fixtures | 当前账号 wire response 和真实搜索 |
| MiniMax 媒体 | 官方 skill/Bash/launcher/broker 的条件证据 | broker/skill 实际就绪、真实任务与 connector 恢复均未验收 |

现有测试加上新 tests 是离线证据，不代表本机已安装的 Agent Bridge 已升级。
本次没有替换共享安装。源码实例的 MCP 握手已检查：
三个能力入口可见，首次 UUID 为必填参数，不支持的组合在创建内部 session 前拒绝。
该隔离实例读取当前源码，已安装实例仍加载旧包；不应混为“已安装生效”。

## 离线复现

使用项目 dev 环境，不需要供应商凭据或连接 DSH：

```powershell
$env:PYTHONUTF8 = "1"
Remove-Item Env:CODEX_CLI_PATH -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path .test-tmp -Force | Out-Null
$env:TEMP = (Resolve-Path .test-tmp).Path
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = (Join-Path (Get-Location) .uv-cache)
.venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp\native-check
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src/agent_bridge
```

环境变量仅用于当前验证进程。`--basetemp` 必须是专门的测试目录，
pytest 会管理该目录，不能传入用户工作目录或已有重要文件。

新增测试涵盖原生来源、无证据不伪造、体积限制、原有文本/resume/usage/
错误/取消兼容、文件安全、复制失败、文件名碰撞、重复事件、thread/turn 不匹配、
提交前拒绝、提交后断线、本地取消、重启和 request retry 不重投。
MiniMax 查询/下载失败测试随未实现的 connector helper 后置，不能记作已经通过。
Windows 符号链接测试在无创建权限时明确 skip；实际 junction 有独立检查。

### 本次最终验收记录

- 最终冻结状态的全量离线回归：`749 passed, 4 skipped`，115.08 秒，exit 0。
  定向回归 `test_native_failures.py + test_codex_image.py`：
  `49 passed`，17.03 秒，exit 0。当前收集总数为 753 项。
- 34 项独立故障注入测试全部通过；最终主审重新运行对应 49 项。
- Ruff：`All checks passed`；mypy：42 个源文件无错误；
  `git diff --check` 无 whitespace error。
- 4 项 skip 是缺少 Windows symbolic-link 创建权限；
  真实 Windows directory junction 拒绝检查在沙箱外运行通过。
- Astra 独立审查最终结论：READY，严格限于源码与离线 A/B 范围。
  审查发现的持久化、部分成功与聚合状态、嵌套媒体过滤、输出预算等问题
  均已修正并通过对应故障测试，不代表真实账号验收。
- 模拟 app-server 交付的 PNG 是 1x1、69 bytes，SHA-256：
  `fce481932ea5d07a91c7991c09fdadb4bf78f9b4cc8f927188384231f9d12679`。
  它只是可复现的测试文件，不是供应商生成产物。
- 此阶段未执行共享安装替换或账号调用；后续账号实测见 C 级记录。
  仍未执行编辑、MiniMax connector、commit、push、release 或 PR。

### 直接能力入口续验

- 新增能力入口首轮相关回归：100 项通过，15.51 秒；包括同 ID 重放、重启后结果复用、
  无来源拒绝、取消与内部 worker 关闭、视频时长检查和 Codex 搜索逐调用配置。
- 独立源码 MCP 实例实际暴露 `call_capability`、`get_capability_result`、
  `cancel_capability`；不支持的 Codex 视频请求被拒绝，内部 session 数仍为零。
- 该阶段真实测试尚未执行；用户随后确认已有登录订阅并授权各一次，
  后续实际为图片 1/1、5 秒动画 1/1、搜索 1/1，未改全局权限/feature。
- 独立复核发现宿主退出打断自动关闭和原生调用计数缺口。已保留 adapter/live
  直到清理结束、保护关闭过程免受宿主取消打断，并新增原生操作类型/次数的
  持久化及验收反例。该软件不保证供应商执行前硬次数限额；后续 C 级记录
  仅证明这一次受控运行实际各调用一次。
- 当前冻结源码的定向回归：132 项通过，43.71 秒；Ruff 全量通过，mypy
  43 个源文件无错误，diff whitespace 检查通过。
- 最新全量：782 项通过、4 项 skip、2 项失败，220.05 秒。
  两项失败在未修改的 probes 调度 tick 及 quota HTTP 子进程超时检查；
  独立复跑相关 probes/全部 quota HTTP 用例 11 项通过，9.11 秒。
  该结果是一次全量加定向复验，不写成“最后全量无失败”。
- 新能力入口增加之前一轮全量为 778 通过、4 skip；最初 749/4 是更早阶段。
  它们不覆盖最新原生调用计数/关闭修正，不能混用验收范围。
- 冻结源码的实际 MCP 指纹：
  `c200791dccc3a9f8cf0a613936726f726c11c3cbf6eb4e83118288c296e15c2b`；
  实际加载目录为本仓库 `src/agent_bridge`，不是共享安装目录。
- Astra 续审接受已修正的关闭取消及原生操作证据问题，独立定向 116 项通过；
  当时结论只覆盖 A/B，不能替代后续账号实测记录或硬次数限额保证。

### 账号实测 C 级记录

- Grok `1.0.50 (c58f321264ba)`，现有已登录订阅账号，CLI 默认模型未覆盖；
  现场 sampler 为 `group/auto-grok-4-7`，effort `high`。
- 搜索：原生 `web_search` 一次，来源为
  [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio-task.html)。
  Bridge call ID：`task_8614820857`。
- 图片：原生 `image_gen` 一次，JPEG 1024x1024，312,629 bytes。
  路径：`.agent-bridge/outputs/task_144e448631/48c9fa2f226b42179a6546c2548cbf3f.jpg`。
  SHA-256：`e34919fb0f2c565ef7be8d627aea11f64727553c1cf283d917078baccdc1e2dc`。
- 视频：原生 `reference_to_video` 一次，输入为上述唯一生图结果。
  MP4 H.264/AAC、544x544、24 fps，1,758,028 bytes；视频 `5.041667` 秒，
  音频 `5.000000` 秒，在 0.05 秒容差内。完整解码 exit 0；首尾帧视觉检查通过。
  路径：`.agent-bridge/outputs/task_7e6632b308/0535d206686b4c65840bd209c6b6a076.mp4`。
  SHA-256：`4b7df2b2b18c54ba6c806dfa849c8b6df63d216c7b7981db99f14c8cbfee9a8c`。
- 首轮 ACP 解析未识别当前 Grok `_meta.x.ai/tool.name`/`rawInput.variant`，
  已执行成功的搜索/图片被错误标记失败。已补协议兼容，用原来的原生 session
  完成事件做本地安全回收，**没有重新搜索或生图**；保留初始错误记录。
  视频随后通过修正后的 Bridge 直接完成。
- 新增匿名协议 fixtures，相关定向回归最终 99 项通过，12.29 秒；Ruff 通过、
  mypy 43 源文件通过。没有声称这次小修重新跑了全量。
- 在新源码 MCP 实例读取三个原 call ID，全部完成、每项原生操作数为 1，
  内部 worker 全部 dead。复验新增供应商调用 0；源码指纹：
  `5a3ce7d1933e9249f67c4f262574afd60e154af4b3b803b36f2311eddc7e69b9`。
  随后将 variant 兼容明确限制为 Grok，MiniMax 无 `name` 的输出仍被拒绝；
  最终源码指纹 `05d76647d16af9e6e24ebbf113e0bd050fcfb41d1e2b3cd1507a43f142e15cbd`。
  该末次范围修正仅跑离线回归，没有增加供应商调用。
- 验收原始记录在本机 `.agent-bridge/acceptance/2026-10-14/verification.json`；
  未提交私人原生会话。仓库内历史 lab 路径 ACL 使 workspace snapshot 不可用，
  该警告不影响原生完成、来源、文件哈希及媒体解码证据。
- 共享安装仍旧，未改全局配置、导出凭据、自动登录或进行 Git/发布操作。

## 范围外

Codex 视频、跨机器传输、对象存储、通用媒体类型体系、统一计费、
未经证实的远端取消 API、自动登录、权限/feature 自动放宽、未授权的费用及测试矩阵、
安装替换、Git commit/push、发布与 PR。

官方依据：`Fei2411/Agent-Bridge`；`openai/codex` 的 app-server protocol
schema、image-generation artifact/tool、exec_events 与 JSONL processor；
Grok 1.0.50 自带 `14-headless-mode.md`、`17-sessions.md` 与安装包 ToolOutput；
MiniMax 0.6.9 的 ACP serializer、官方 URL consumer 和
`assets/skills/mcode-tools-master/SKILL.md`。
