from app.core.models import AgentRole

ROLE_POLICY = {
    AgentRole.PLANNER: {"tools": {"list_files", "read_file", "search_text"}, "goal": "分析任务并给出可验证计划。"},
    AgentRole.EXPLORER: {"tools": {"list_files", "read_file", "search_text"}, "goal": "只读检索仓库，提供最小必要上下文。"},
    AgentRole.CODER: {"tools": {"list_files", "read_file", "search_text", "write_file", "run_command"}, "goal": "实现改动并运行验证。"},
    AgentRole.REVIEWER: {"tools": {"list_files", "read_file", "search_text", "run_command"}, "goal": "独立检查改动和测试结果。"},
}
