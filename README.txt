MossCode - 软件工程专业推免编程智能体

Git 仓库：https://github.com/nekoneko2333/NJUSE_AgentProj

项目说明：MossCode 是一个可在用户指定工作区中自主读写文件、执行命令并验证结果的本地编程智能体。项目未使用 LangChain、AutoGen、OpenAI Agents SDK 等 Agent 框架；对话上下文、原生 tool calling 解析、本地工具、安全边界、循环终止、错误恢复和 Agent 编排均自行实现，模型通过 OpenAI-compatible API 接入。

运行方法：安装 Python 3.11+、Node.js 与 pnpm。复制 .env.example 为不入库的 .env，配置 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL、APP_USERNAME、APP_PASSWORD。后端运行：pip install -r backend/requirements.txt；cd backend；python -m uvicorn app.main:app --port 8001。另开终端运行：cd frontend；pnpm install；pnpm dev。浏览器访问 http://127.0.0.1:5173，并使用 .env 中的本机账号登录。

特色功能：支持单 Agent、固定有界的四 Agent 和自适应三种模式。四 Agent 依次规划、只读探索、实施验证、结构化审查，最多定向返工一次；角色轮次、工具调用、交接上下文和证据大小均有硬上限，避免无限循环。系统还提供 SQLite 持久会话、SSE 实时进度、消息排队、停止与重试、跨对话记忆、命令审批、工作区路径隔离、文件树与全文搜索、多标签编辑、Markdown、Diff、检查点恢复、AGENTS.md、Hooks、本地 MCP、中英文和双主题。

质量说明：当前后端 76 项单元测试、前端 10 项组件测试全部通过，TypeScript 检查和生产构建通过；测试使用本地临时数据，不需要真实 API Key。API Key 等凭据仅通过环境变量或未入库的 .env 提供，不得写入仓库、README 或演示视频。
