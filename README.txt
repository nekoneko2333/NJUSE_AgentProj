MossCode - 软件工程专业推免编程智能体

Git 仓库：https://github.com/nekoneko2333/NJUSE_AgentProj

运行：准备 Python/Conda 环境 web3d-backend 与 Node.js。在根目录创建不入库的 .env，配置 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL、APP_USERNAME、APP_PASSWORD。后端执行：cd backend；conda run -n web3d-backend python -m uvicorn app.main:app --port 8001。前端执行：cd frontend；npm install；npm run dev。浏览器访问 http://127.0.0.1:5173。

特色：项目未使用 LangChain、AutoGen、OpenAI Agents SDK 等 Agent 框架。对话上下文、原生 tool calling 解析、本地文件与命令工具、四角色编排、单 Agent 对照、循环终止、错误恢复和权限审批均自行实现。系统支持 SQLite 持久会话、后台执行、取消与重试、跨对话记忆、文件树与全文搜索、Markdown、多标签编辑、新旧 Diff、检查点恢复、终端授权、AGENTS.md、Hooks、本地 MCP、中英文和双主题。

质量：后端 38 项测试、前端组件测试、TypeScript、生产构建和 Playwright 多尺寸流程均通过。真实模型对照实验中，单 Agent 与四 Agent 各 3 次均通过独立测试；简单明确任务推荐单 Agent，复杂或高风险任务使用四角色独立探索与复核。

注意：提交前需将仓库设为公开；API Key 不得出现在仓库、README 或视频中。
