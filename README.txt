项目名称：MossCode 可视化多智能体编程助手
仓库地址：https://github.com/nekoneko2333/NJUSE_AgentProj

运行方法：复制 .env.example 为 .env，填写 DeepSeek 的 LLM_API_KEY，并修改 APP_USERNAME / APP_PASSWORD。激活 conda 环境 web3d-backend，进入 backend 执行 python -m uvicorn app.main:app --reload --port 8000；进入 frontend 执行 pnpm install、pnpm dev，浏览器访问 http://127.0.0.1:5173。

主要功能：用户输入本地工作区和开发任务后，Planner、Explorer、Coder、Reviewer 四个角色依次完成规划、只读检索、文件修改、命令验证和独立审查。系统独立实现模型调用、tool calling 解析、本地工具、循环终止和错误处理，不依赖 Agent 框架。SQLite 持久化会话、轮次和事件，支持重启恢复、连续追问及运行中补充消息排队；长上下文采用旧轮摘要、最近三轮和当前任务的有界窗口。GUI 支持本机登录、中英文、明暗主题，以及自动执行、逐条询问或禁用终端命令三种权限模式；内部协作结束后统一输出 Markdown 结果，并展示递归文件树、变更树、逐文件 Diff 与终端命令卡片。文件访问限制在所选工作区，高风险命令、越界路径、超时和重复调用均受控；API Key 仅由未提交的 .env 读取。

验证：后端进入 backend 执行 python -m unittest discover -s tests -v；前端进入 frontend 执行 pnpm build。
