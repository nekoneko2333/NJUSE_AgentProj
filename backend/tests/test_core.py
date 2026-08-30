import asyncio
import unittest

from app.agents.orchestrator import Orchestrator
from app.core.settings import settings
from app.runtime.workspace import Workspace
from app.tools.registry import ToolRegistry


class FakeClient:
    async def complete(self, messages, tools):
        return {"role": "assistant", "content": "已完成"}


class CoreTests(unittest.TestCase):
    def test_risky_command_is_blocked(self):
        tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
        self.assertEqual(tools.run_command("rm -rf temp").code, "command_requires_approval")

    def test_orchestrator_runs_all_roles(self):
        async def exercise():
            events = []

            async def publish(event):
                events.append(event)

            tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
            await Orchestrator("test", "检查项目", tools, FakeClient(), settings, publish).run()
            return events

        events = asyncio.run(exercise())
        self.assertEqual(len(events), 9)
        self.assertEqual(events[-1].type, "task_finished")


if __name__ == "__main__":
    unittest.main()
