# MossCode 多 Agent 设计、工具机制与评测方案

## 1. 四角色设计从哪里来

MossCode 的 Planner → Explorer → Coder → Reviewer 不是照搬某个产品的固定队形，而是把软件工程中的“计划、调查、实现、独立验收”转成四个受权限约束的执行单元。

这个方向与主流产品的设计原则一致：Claude Code 的 subagent 支持独立提示词、上下文和工具权限，并提供只读 Explore / Plan 类型；GitHub Copilot 的 custom agent 也允许为不同 Agent 配置工具白名单；OpenAI 的 Multi-agent 文档强调由 root agent 给子 Agent 分配边界明确的任务，并由 root agent 综合结果和生成最终答复。

参考资料：

- [Claude Code：Create custom subagents](https://code.claude.com/docs/en/sub-agents)
- [GitHub Copilot：Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration)
- [OpenAI：Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)

MossCode 自己做出的关键取舍是：

1. 采用确定性的串行交接，不让模型随意决定跳过审查。
2. 权限由后端代码强制，而不是只在提示词中要求“请勿写入”。
3. 主编排器持有任务状态和最终决定；Reviewer 是只读顾问，不是可以扩张要求的终审者。
4. 将四角色内部输出封装，只向用户展示阶段进度和统一结论。

## 2. 当前四个 Agent 是如何运行的

| 角色 | 输入 | 权限 | 输出给下一角色的内容 |
| --- | --- | --- | --- |
| Planner | 用户任务、会话记忆、项目规则 | 无本地工具 | 整理用户明确提出的约束，不新增要求 |
| Explorer | 上述内容和 Planner 摘要 | 列表、读取、搜索 | 相关文件、现状和风险证据 |
| Coder | 前两者摘要 | 读取、搜索、写入、命令 | 实际改动和验证结果 |
| Reviewer | 冻结验收范围、交接摘要、运行证据 | 读取、搜索；必要时命令，不可写入 | `PASS`，或带 `BASIS/EVIDENCE/ACTION` 的一项待办 |

角色共享同一个模型服务并不意味着它们是同一个 Agent。区分点是每个角色拥有不同的系统指令、上下文窗口、工具 schema、工具白名单、轮次上限和任务目标。后端会拒绝越权工具调用。

当前使用串行而非并行，是因为四阶段存在明确数据依赖，且本地单工作区不适合多个写入者竞争。编排方式借鉴 root-agent 模式：主编排器负责持续收敛和最终答复，子角色只返回受限结果。Reviewer 不能直接终止任务；合法问题由主编排器交回 Coder 内部修复。未来可以并行运行多个只读 Explorer，但多个 Coder 应先通过 Git worktree 隔离。

## 3. 如何证明多 Agent 有效

“有四个角色”只能证明系统是多角色结构，不能证明效果优于单 Agent。最有说服力的证据是同模型、同工具、同任务集下的受控 A/B 对照。

### 对照组

- S：单 Agent，拥有读、写、搜索、命令工具，负责从理解到验收的全部过程。
- M：当前四 Agent 串行方案。
- M-R：去掉 Reviewer，用于测量独立审查的贡献。
- M-E：去掉 Explorer，让 Coder 自行探索，用于测量上下文分工的贡献。

单 Agent 不能获得更大的无限预算。为了公平，应控制为相同模型、相同模型参数、相同总模型轮次或近似 Token 上限、相同命令权限、相同初始仓库快照。

### 任务集

建议准备至少 20 个任务，每个配置重复 3 次：

- 5 个只读定位和解释任务。
- 5 个单文件小修复。
- 5 个跨文件功能实现。
- 3 个含失败测试的诊断修复。
- 2 个带安全诱导或路径越界要求的任务。

不要只选择 MossCode 擅长的任务。任务和验收脚本应在实验前固定，运行中不能人工修改评分标准。

### 指标

| 指标 | 定义 | 价值 |
| --- | --- | --- |
| 任务成功率 | 隐藏验收命令全部通过 | 最重要的结果指标 |
| 假成功率 | Agent 报成功但验收失败 | 验证 Reviewer 和证据终止是否有效 |
| 无关改动数 | 不在任务范围内的文件或行 | 衡量控制力 |
| 工具恢复率 | 工具失败后最终修复并通过 | 衡量循环韧性 |
| 高风险操作拦截率 | 危险请求是否被拒绝 | 衡量安全性 |
| 人工介入次数 | 批准之外的纠正消息数量 | 衡量自治程度 |
| 延迟、模型调用数、Token | 每个成功任务的成本 | 衡量多 Agent 额外开销 |

答辩时不要提前宣称“四 Agent 一定更好”。合理预期是：四 Agent 在跨文件和复杂任务上降低假成功率、提高可审查性，但在简单任务上会增加调用次数和延迟。若数据也呈现这个结论，反而说明实验可信。

### 结果报告格式

```text
配置 | 成功任务/总任务 | 假成功 | 平均模型调用 | 中位耗时 | 无关改动
单 Agent | ... | ... | ... | ... | ...
四 Agent | ... | ... | ... | ... | ...
无 Reviewer | ... | ... | ... | ... | ...
无 Explorer | ... | ... | ... | ... | ...
```

当前仓库已经提供 `scripts/benchmark_agent_modes.py`：它从同一初始代码创建隔离工作区，以相同模型、工具和任务分别运行单 Agent 与四 Agent，并在 Agent 结束后独立执行固定测试，记录角色、工具、耗时、模型请求、Token、改动文件和完成来源。2026-09-01 的三次真实 DeepSeek 对照中，两种模式均 3/3 通过；单 Agent 平均 13.14 秒、13,150 tokens，四 Agent 平均 51.46 秒、36,983 tokens。完整数据解释见 `docs/单Agent与多Agent性能对比.md`。

## 4. 可以做哪些创新

目前已有的差异化设计：

- 冻结验收与证据化复核：Reviewer 只能引用用户明确要求、真实失败验证或本轮回归；拒绝必须提供依据、证据和最小修复动作，避免每轮重新审计导致范围漂移。
- Root-owned completion：主编排器持有修复循环和最终状态；最后写入后的成功验证可以阻止无依据的 Reviewer 意见制造假失败，并单独记录完成来源。
- 权限型角色：Planner 无工具、Explorer 只读、Coder 写入、Reviewer 只验收，权限由后端强制。
- 可插入的持续对话：当前轮运行时补充消息进入持久 FIFO 队列，在安全边界后衔接。
- Git 无关的文件级检查点：适用于非 Git 工作区，并使用哈希保护人工后续修改。
- 可选跨对话项目记忆：只复用同一工作区的已完成摘要，默认关闭。
- Progressive disclosure：用户看到自然进度和最终答复，原始工具证据仍可在文件、Diff 和终端视图审阅。

本版已经加入第一阶段“自适应编排”：简单短任务进入单 Agent 基线，较长任务或包含架构、重构、迁移、测试等复杂信号时进入多 Agent。实际选择写入结构化执行事件，便于统计成本与效果。后续可把当前确定性路由升级为经过基准集校准的轻量分类器，并进一步支持 Explorer + Coder + Reviewer 等中间配置。

## 5. 工具调用是怎么实现的

MossCode 使用模型原生 tool calling，但模型只提出请求，真正执行权在本机后端。

```text
角色可用工具 schema
        ↓
模型返回 tool_calls
        ↓
解析函数名与 JSON arguments
        ↓
角色白名单 → 路径边界 → 权限/风险检查 → Hooks
        ↓
本机执行文件或命令工具
        ↓
结构化 ToolResult { ok, code, content, meta }
        ↓
作为 tool message 回传模型，并写入审计事件
```

内置工具位于 `backend/app/tools/registry.py`：

- `list_files`：列出目录直接子项。
- `read_file`：按行读取文本。
- `search_text`：工作区全文搜索。
- `write_file`：创建或覆盖文本，返回 unified diff、哈希和检查点 ID。
- `run_command`：在工作区启动本机子进程，捕获输出、退出码、超时和取消。

工具 schema 在 `backend/app/agents/orchestrator.py` 中定义。每个角色只会把获准 schema 发给模型；即使模型伪造其他工具名，后端仍返回 `tool_not_permitted`。写入前创建检查点，命令执行前应用自动、询问或禁止模式，高风险命令始终硬拦截。

## 6. Hooks 是什么

Hooks 是在 Agent 生命周期固定位置自动运行的本地命令，适合把确定性工程规则接入循环。

- `before_tool`：任意工具执行前，例如检查当前分支或阻止不允许的状态。
- `after_tool`：任意工具执行后，例如记录审计信息。
- `after_write`：文件写入成功后，例如运行 formatter 或快速 lint。
- `before_finish`：任务标记完成前，例如运行完整测试。

Hooks 与 Agent 的区别是：Hook 不做开放式推理，它是确定性自动化。比如“每次写文件后运行 Ruff”适合 Hook，“判断失败原因并修复代码”适合 Agent。

配置位于工作区 `.mosscode/hooks.json`。Hooks 默认关闭，命令继续受工作区、超时和审批规则限制。若关键 Hook 失败，系统不会把任务标记为成功。

## 7. MCP 是什么

MCP（Model Context Protocol）是把外部能力以标准工具 schema 暴露给模型的协议。它解决的是“如何接更多工具”，不是“如何编排多个 Agent”。

例如项目需要查询本地数据库：不必在 MossCode 核心中硬编码 `query_database`，可以连接一个数据库 MCP server。MossCode 通过 `tools/list` 发现工具，把它们以 `server/tool` 命名加入角色工具列表；模型调用后，MossCode 通过 `tools/call` 获取结果。

配置位于 `.mosscode/mcp.json`。当前版本支持本地 stdio JSON-RPC；可按角色限制工具，写入型 MCP 工具必须审批。尚不支持远程 OAuth MCP。

## 8. 为什么以前设置页看不到详细配置

早期设置页只读取 `AGENTS.md`、`.mosscode/hooks.json` 和 `.mosscode/mcp.json` 的状态，文件不存在时只显示“未配置”，没有提供创建入口，所以用户很难理解和启用。

现在设置页提供：

- 四 Agent、单 Agent 基线和自适应三种真实后端执行模式。
- Planner、Explorer、Reviewer 可独立关闭；Coder/单 Agent 作为执行者不可关闭。
- 每个角色的独立轮次上限和最多 2000 字附加指令；附加指令不会扩大工具权限。
- AGENTS.md 编辑器与实际加载来源。
- Hooks 生命周期说明、事件数量、JSON 模板和保存入口。
- MCP 作用说明、服务器/工具状态、JSON 模板和保存入口。
- 跨对话项目记忆开关。

配置保存使用内容哈希防止覆盖外部改动，并在写入前创建检查点。

## 9. 如何在界面中做单/多 Agent 对比

进入“设置 → Agent 编排与对比”，选择“单 Agent”或“四 Agent”并保存。两种模式都使用相同模型、工作区工具、终端权限、检查点和“写入后必须成功验证”的完成门槛；区别仅在角色分工与上下文交接，因此可以作为可解释的基线对照。

进行正式实验时，应从同一 Git 快照分别新建对话，固定模型参数与任务描述，并记录成功率、假成功率、耗时、模型调用、工具调用和无关改动。界面里的模式切换证明系统具备对照能力，但不能替代真实基准实验数据。
