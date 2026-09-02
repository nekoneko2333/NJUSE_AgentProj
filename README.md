# MossCode

MossCode 是一个面向本地软件工程任务的多智能体编程工作台。它以中文为默认语言，提供可切换的中英文界面，将任务规划、代码探索、文件修改、命令验证和结果审查串成一个可恢复、可审计的完整闭环。

项目没有依赖现成 Agent 框架：对话历史与上下文管理、模型工具调用解析、本地工具执行、角色编排、循环终止、错误恢复、权限审批和完成判定均由项目自行实现。当前默认接入 DeepSeek，也兼容采用 OpenAI Chat Completions 协议的模型服务。

## 当前完成度

目前已经实现可实际运行的前后端系统，而非静态界面或一次性模型演示：

- 三种 Agent 模式：固定有界的四角色流程、单 Agent 对照基线，以及按任务复杂度选择实际流程的自适应模式。
- 多 Agent 的角色顺序、轮次、工具调用、交接上下文与返工次数由后端统一约束；设置页仅允许补充角色关注点，旧配置不能关闭必要角色。
- SQLite 持久化会话、轮次、事件、后台执行、审批和检查点索引，刷新或重启后可恢复。
- 后台 Execution 状态机支持排队、执行、等待审批、取消、失败、重试和中断恢复。
- Agent 可在用户指定的本机工作区中搜索、读取、修改文件并运行安全命令。
- GUI 提供聊天、文件树、全文搜索、多标签编辑、Diff、检查点恢复、终端和设置面板。
- 支持项目规则、生命周期 Hooks、本地 stdio MCP、模型参数和命令权限配置。
- 默认中文，文案通过 i18n 资源管理，可切换英文；提供 light 与 dusk 两套主题。
- 已建立后端单元测试、前端组件测试、TypeScript 检查、生产构建和 Playwright 全栈冒烟测试。

## 主要特性

### 多 Agent 编排

设置页可选择“四 Agent / 单 Agent / 自适应”。单 Agent 与四 Agent 共用模型、工具和安全边界，方便进行公平 A/B 对照；自适应模式会根据任务语义选择实际流程并记录在执行事件中。多 Agent 始终运行固定状态机：`规划 → 探索 → 实施 → 审查 → 可选的一次返工 → 收尾`，旧会话中遗留的角色开关或轮次值不会裁掉流程节点。

| 角色 | 职责 | 默认工具能力 |
| --- | --- | --- |
| Planner | 整理用户明确提出的验收条件，不扩张范围 | 不调用本地工具 |
| Explorer | 调查项目结构并向实现者报告事实 | 文件列表、读取、全文搜索 |
| Coder | 实施修改并运行必要验证 | 列表、读取、搜索、写入、命令执行 |
| Reviewer | 通过结构化审查 API 核对冻结验收项与证据账本 | 不重复操作工作区 |

角色的中间 JSON、工具参数和内部讨论不会直接倾倒给用户。聊天区以自然语言显示阶段性进度、已用时间和最终 Markdown 总结；详细工具记录保留在可折叠区域中，兼顾可读性与可追溯性。

编排器还包含以下工程约束：

- 解析原生 tool call 与兼容格式，统一校验工具参数。
- 固定预算：Planner 1 轮；Explorer 4 轮、4 次工具；Coder 8 轮、10 次工具且最多 4 次探查；Reviewer 1 次；返工最多 1 次、5 轮、6 次工具。
- 单次模型响应产生超额工具调用时，只保留预算内部分，其余整体截断并记入事件，避免事件风暴和长时间空转。
- 交接上下文最多 3,000 字符，结构化审查证据最多 4,000 字符，状态机最多经过 7 个非终止节点。
- 在模型请求、工具调用和角色切换前检查取消信号。
- 主编排器持有任务结果与最终状态，Reviewer 只是受约束的评估节点。验收范围冻结为用户原始要求；Planner、Explorer 和 Reviewer 都不能新增可选规范。
- 生产流程只发起一次结构化审查；若存在明确缺口，只把未闭环验收项交回 Coder 定向返工一次，返工后由新的类型化工具证据确定性收口，不再启动无限复审。
- 文件写入、命令和最终回复形成有界证据账本；只有耗尽唯一一次返工且权威证据仍不完整时，才向用户报告任务失败。
- 兜底完成会记录 `completion_source=structured_evidence`，不会伪装成 Reviewer 正常通过。

### 持久对话与长上下文

- 会话、用户轮次、Agent 事件和执行状态写入 `data/mosscode.db`。
- 可从历史会话继续追问，而不是每次启动一个无记忆的任务。
- 上下文采用“较早轮次持久摘要 + 最近若干轮 + 当前任务”的有界窗口；失败、取消和中断轮次同样保留原始需求，因而“重试”“继续上面的要求”等短指令能够找到正确指代。
- 默认上下文预算约 12,000 字符，并在各角色工具循环中再次裁剪。
- Agent 思考期间输入框仍可使用；新消息会持久化排队，在当前一轮边界处插入后续执行。

### 后台执行、取消与重试

创建任务后，后端立即返回 `execution_id`，实际执行由后台线程继续完成。Execution 使用统一状态：

`queued → running → waiting_approval → succeeded / failed / cancelled / interrupted`

前端通过 SSE 接收进度，刷新页面后会从数据库恢复最新状态。用户可以取消运行中的任务，失败轮次也可以从原任务重试；重试会保留当前文件，并创建新的改动边界。续跑时允许先验证已经保留的正确实现，不会为了满足“本轮必须写入”而无意义重写文件；若验证仍失败，则进入 Reviewer→Coder 有限返工。

### 文件工作台

- 递归文件树会展示工作区内的目录与文件。
- 支持全文搜索、结果分组、行号定位和忽略目录。
- 文件以多标签形式预览，支持代码、文本、Markdown 和 JSON。
- 可直接编辑文本文件；保存前展示 unified diff。
- 保存接口使用 `expected_sha256` 乐观锁，防止覆盖 Agent 或外部程序产生的新修改。
- 大文件采用只读与截断提示，避免阻塞浏览器。
- 变更按文件折叠，可复制、审阅并从检查点恢复。

### 独立检查点

每轮第一次修改某个文件前，系统会保存该文件的原始内容、存在状态和哈希。同轮后续写入不会覆盖原始快照。

- 快照位于 Git 忽略的 `data/checkpoints`，数据库仅保存索引和元数据。
- 恢复前比较当前哈希与 Agent 写入后的哈希。
- 检测到用户或外部程序的后续修改时返回冲突，不强制覆盖。
- 恢复必须由用户显式确认，并记录 `checkpoint_restored` 审计事件。

### 本地工具与权限

命令执行支持三种模式：

- 自动执行安全命令。
- 每次询问并等待单次授权。
- 禁止执行命令。

所有文件路径都必须落在选定工作区中；高风险命令由底层规则直接拦截，不会因为用户批准而绕过。取消执行时，后端会终止对应命令的精确子进程树。

### 项目规则、Hooks 与 MCP

- 自动加载工作区根目录 `AGENTS.md` 和 `.mosscode/rules/*.md`，按文件名稳定排序并限制总长度。
- GUI 可查看本轮实际加载的规则来源；默认不允许 Agent 修改规则文件。
- `.mosscode/hooks.json` 可配置 `before_tool`、`after_tool`、`after_write`、`before_finish` 生命周期命令，默认关闭，启用后仍受工作区、超时和审批规则约束。
- `.mosscode/mcp.json` 支持本地 stdio JSON-RPC、`tools/list` 和 `tools/call`。
- MCP 工具采用 `server/tool` 命名空间，可按角色配置白名单；写入型 MCP 工具必须审批。

当前 MCP 首版聚焦可信的本地进程，尚未支持远程 OAuth MCP。

### GUI 与交互

- 默认中文，基于 i18next 管理中英文资源，业务组件不依赖整段硬编码文案。
- light 与 dusk 主题共用语义化 design token；颜色、边框、阴影、圆角、动效和滚动条均集中定义，便于增加自定义主题。
- 视觉遵循 `style/prompt.xml` 的有机、克制风格，该文件本身保持不变。
- Markdown 使用 GFM 渲染，支持标题、列表、表格、代码块和链接。
- 聊天、文件树、Diff、终端与编辑器使用统一的极简滚动条，并移除 Windows 原生箭头按钮。
- 对话右侧提供类似 Gemini 的用户发言导航轨道，可点击标记跳转到指定问题，并突出当前阅读轮次。
- 仅在用户处于底部时自动跟随新消息；向上阅读时保留位置并提示“有新进度”。
- 布局针对 1440×900、900×800 和 390×844 三类尺寸适配，窄屏下工作台可独立切换。
- 本机单用户登录使用 HttpOnly Cookie；支持登录、退出和主题切换。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | React、TypeScript、Vite |
| UI 与内容 | CSS Design Tokens、Lucide React、react-markdown、remark-gfm |
| 国际化 | i18next、react-i18next |
| 后端 | Python、FastAPI、Uvicorn、HTTPX |
| 模型接入 | OpenAI-compatible Chat Completions / tool calling，默认 DeepSeek |
| 实时通信 | Server-Sent Events（SSE） |
| 持久化 | SQLite（Python 标准库），文件系统检查点 |
| 前端测试 | Vitest、Testing Library、User Event、jsdom |
| 全栈测试 | Playwright、内置 demo 模型，不访问真实 API Key |
| 后端测试 | Python unittest |

## 系统架构

```text
React GUI
├── 对话与 Markdown / 进度 / 审批
├── 文件树 / 搜索 / 编辑 / Diff / 检查点
└── 会话 / 设置 / 规则 / 主题 / i18n
                 │ REST + SSE
                 ▼
FastAPI Backend
├── Auth 与 Session API
├── SQLite Store 与 Context Manager
├── Background Execution 状态机
├── Bounded Multi-Agent State Graph
│   ├── Planner（1 轮）
│   ├── Explorer（4 轮 / 4 次工具）
│   ├── Coder（8 轮 / 10 次工具）
│   ├── Reviewer（1 次结构化审查）
│   └── Focused Repair（最多 1 次）
├── Tool Registry / Approval / Hooks / MCP
└── Workspace Guard / Checkpoint Manager
                 │
                 ├── OpenAI-compatible LLM（DeepSeek）
                 └── 用户选择的本机工作区
```

一次任务的主要流程为：

1. 用户选择工作区并发送任务，后端创建持久化 Execution。
2. 上下文管理器组合会话摘要、近期轮次、项目规则和当前输入。
3. 固定状态机依次进入 Planner、Explorer、Coder 和 Reviewer；工具调用先经过角色白名单、路径边界、命令安全检查和角色硬预算。
4. 文件首次写入前创建检查点，写入和命令结果持续记录为事件并通过 SSE 推送。
5. Reviewer 通过一次结构化调用核对冻结验收项；若存在明确缺口，主编排器允许 Coder 定向返工一次，并根据返工后的新证据确定性终止流程。
6. 前端输出一条统一的 Markdown 总结，并提供 Diff、日志和恢复入口。

## 目录结构

```text
NJUSE_AgentProj/
├── backend/
│   ├── app/
│   │   ├── agents/          # 四角色定义与编排器
│   │   ├── core/            # 认证、上下文、检查点、扩展与配置
│   │   ├── llm/             # OpenAI-compatible 模型客户端
│   │   ├── storage/         # SQLite 持久化
│   │   ├── tools/           # 本地文件、搜索、写入和命令工具
│   │   └── main.py          # FastAPI 接口与后台执行
│   └── tests/
├── frontend/
│   ├── src/                 # React 界面、工作台、设置与国际化资源
│   ├── e2e/                 # Playwright 冒烟测试
│   └── playwright.config.ts
├── docs/                    # 项目计划书与总结报告
├── style/prompt.xml         # 原始视觉规范，仅作为设计依据
├── data/                    # 本地数据库与检查点，Git 忽略
└── .env.example             # 无密钥的配置模板
```

## 本地运行

### 1. 环境要求

- Windows 10/11（当前主要测试平台）
- Conda 环境 `web3d-backend`，建议 Python 3.11+
- Node.js 与 pnpm
- DeepSeek 或其他 OpenAI-compatible 服务的 API Key

### 2. 配置后端

在项目根目录复制 `.env.example` 为 `.env`，填写本机配置：

```dotenv
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

APP_USERNAME=moss
APP_PASSWORD=请修改为自己的密码
APPROVAL_TIMEOUT_SECONDS=180

# 留空时在 GUI 中为每个任务选择工作区
DEFAULT_WORKSPACE=
```

`.env` 已被 Git 忽略。API Key 只由本机后端读取，设置接口仅返回“是否已配置”，不会返回密钥原文，也不会把登录密码加入模型上下文。

### 3. 启动后端

```powershell
conda activate web3d-backend
pip install -r backend/requirements.txt
cd backend
python -m uvicorn app.main:app --reload --port 8001
```

### 4. 启动前端

另开终端：

```powershell
cd frontend
pnpm install
pnpm dev
```

访问 `http://127.0.0.1:5173`。前后端应使用相同的主机名形式（都使用 `127.0.0.1` 或都使用 `localhost`），以保证本机登录 Cookie 正常工作。

登录账号和密码来自 `.env` 的 `APP_USERNAME` 与 `APP_PASSWORD`。服务重启后内存中的登录会话失效，需要重新登录。

## 关键接口

| 能力 | 接口示例 |
| --- | --- |
| 创建后台任务 | `POST /api/sessions/{id}/executions` |
| 查询与取消执行 | `GET /api/executions/{id}`、`POST /api/executions/{id}/cancel` |
| 重试轮次 | `POST /api/turns/{id}/retry` |
| 读取与安全保存文件 | `GET /api/sessions/{id}/files/content`、`PUT /api/sessions/{id}/files/content` |
| 工作区全文搜索 | `GET /api/sessions/{id}/search` |
| 查询与恢复检查点 | `GET /api/sessions/{id}/checkpoints`、`POST /api/checkpoints/{id}/restore` |
| 实时事件 | Session / Execution SSE 事件流 |

## 测试与验收

后端测试：

```powershell
cd backend
conda run -n web3d-backend python -m unittest discover -s tests -v
```

前端组件测试、类型检查和生产构建：

```powershell
cd frontend
pnpm test
pnpm lint
pnpm build
```

全栈冒烟测试：

```powershell
cd frontend
pnpm test:e2e
```

当前验收基线：后端 76 项测试、前端 3 个组件测试文件共 10 项测试、1 条 Playwright 多尺寸全栈流程，以及 TypeScript 检查和生产构建全部通过。自动化测试使用临时数据库与 demo 模型，不读取真实 API Key，不修改 `style/prompt.xml`，也不会操作测试工作区以外的文件。

单 Agent / 四 Agent 的真实模型对照可运行：

```powershell
conda run -n web3d-backend python scripts/benchmark_agent_modes.py --repeats 3
```

2026-09-01 使用 `deepseek-v4-flash` 的三次对照中，两种模式均 3/3 通过外部 6 项测试且未修改测试文件；单 Agent 平均 13.14 秒、13,150 tokens，四 Agent 平均 51.46 秒、36,983 tokens。完整方法、原始指标解释和适用边界见 `docs/单Agent与多Agent性能对比.md`。

## 安全边界

- 工作区路径必须由用户选择或由 `DEFAULT_WORKSPACE` 明确配置。
- 文件工具拒绝越过工作区边界的路径。
- 高风险命令保持硬拦截，审批只适用于允许询问的命令。
- 直接编辑采用内容哈希并发保护，恢复检查点同样进行冲突检测。
- `.env`、数据库、检查点、测试产物和本机缓存均不提交到 Git。
- Hooks 与 MCP 默认关闭，开启后仍经过角色权限和执行安全层。

## 后续方向

当前版本已经形成完整的本地 AgentCode 闭环，后续可继续向以下竞品级能力演进：

1. 使用 Git worktree 隔离并行任务，并支持多方案对比。
2. 按角色选择模型、Git worktree 隔离，以及可证明安全的只读并行 Explorer。
3. Skills / 插件包、MCP 管理界面和本地工具市场。
4. GitHub Issue、Pull Request、CI、CodeQL 与 secret scanning 集成。
5. 云端后台执行、移动端查看和本地/云端任务交接。
6. Token、费用、上下文压缩率和 Agent 质量评测面板。

## 文档

- [项目计划书](docs/项目计划书.md)
- [总结报告](docs/总结报告.md)
- [面试答辩问答与要求核对](docs/面试答辩问答.md)
- [多 Agent 设计、工具机制与评测方案](docs/多Agent设计与评测.md)

本项目用于南京大学软件学院 2027 年预推免项目作业展示。
