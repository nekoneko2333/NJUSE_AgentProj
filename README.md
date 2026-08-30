# MossCode

默认中文、可切换中英文的多智能体编程助手。项目独立实现模型调用、工具执行、上下文传递、状态机与终止控制，不依赖 agent 框架。

## 运行

1. 复制 `.env.example` 为 `.env`，填写 DeepSeek API key。
2. 后端：`python -m pip install -r backend/requirements.txt`，然后在 PowerShell 项目根目录执行 `$env:PYTHONPATH='backend'; python -m uvicorn app.main:app --reload --port 8000`。
3. 前端：`cd frontend && pnpm install && pnpm dev`，访问 `http://localhost:5173`。

输入任务与工作区后，Planner、Explorer、Coder、Reviewer 依次执行。真实运行会调用 `.env` 中指定模型；所有文件操作受工作区边界限制，高风险命令被拦截，重复工具调用会自动终止对应角色。真实 key 仅存于未提交的 `.env`。

## 验证

`$env:PYTHONPATH='backend'; python -m unittest discover -s backend/tests -v`
`cd frontend && pnpm build`
