from app.core.models import AgentRole

ROLE_POLICY = {
    AgentRole.SINGLE: {"tools": {"list_files", "read_file", "search_text", "write_file", "run_command"}, "goal": "独立完成分析、实现与验证，给出一个完整结论。"},
    AgentRole.PLANNER: {"tools": set(), "goal": "仅整理用户明确提出的验收目标，不新增可选规范或隐含要求。"},
    AgentRole.EXPLORER: {"tools": {"list_files", "read_file", "search_text"}, "goal": "只读检索仓库并报告事实，为实现者提供最小必要上下文，不作最终验收。"},
    AgentRole.CODER: {"tools": {"list_files", "read_file", "search_text", "write_file", "run_command"}, "goal": "实现改动并运行验证。"},
    AgentRole.REVIEWER: {"tools": {"list_files", "read_file", "search_text", "run_command"}, "goal": "作为主编排器的只读顾问，依据冻结验收范围和运行证据报告最少、可操作的剩余问题。"},
}
