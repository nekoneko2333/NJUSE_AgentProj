from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.agents.orchestrator import Orchestrator  # noqa: E402
from app.core.models import AgentEvent  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.llm.client import OpenAICompatibleClient  # noqa: E402
from app.runtime.workspace import Workspace  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402


TASK = """修复 inventory.py 的库存预订逻辑，不得修改 test_inventory.py：
1. add_stock 必须累加库存，并拒绝非正整数数量；
2. reserve 必须原子处理多个 SKU，库存不足时抛出 ValueError 且不能产生部分扣减；
3. 重复 order_id 必须拒绝；
4. snapshot 必须按 SKU 排序返回独立副本。
完成后运行当前 Python 解释器执行 `python -m unittest -v`，根据真实结果修复，最终简要说明验证证据。"""

INVENTORY_SOURCE = '''class Inventory:
    def __init__(self):
        self.stock = {}
        self.reservations = {}

    def add_stock(self, sku, quantity):
        self.stock[sku] = quantity

    def reserve(self, order_id, items):
        for sku, quantity in items.items():
            self.stock[sku] = self.stock.get(sku, 0) - quantity
            self.reservations[order_id] = dict(items)
            return True

    def snapshot(self):
        return self.stock
'''

TEST_SOURCE = '''import unittest

from inventory import Inventory


class InventoryTests(unittest.TestCase):
    def test_add_stock_accumulates(self):
        inventory = Inventory()
        inventory.add_stock("moss", 2)
        inventory.add_stock("moss", 3)
        self.assertEqual(inventory.snapshot(), {"moss": 5})

    def test_add_stock_rejects_invalid_quantity(self):
        inventory = Inventory()
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inventory.add_stock("moss", value)

    def test_reserve_multiple_skus_atomically(self):
        inventory = Inventory()
        inventory.add_stock("fern", 4)
        inventory.add_stock("moss", 5)
        self.assertTrue(inventory.reserve("order-1", {"moss": 2, "fern": 3}))
        self.assertEqual(inventory.snapshot(), {"fern": 1, "moss": 3})

    def test_insufficient_stock_does_not_partially_mutate(self):
        inventory = Inventory()
        inventory.add_stock("fern", 1)
        inventory.add_stock("moss", 5)
        before = inventory.snapshot()
        with self.assertRaises(ValueError):
            inventory.reserve("order-2", {"moss": 2, "fern": 3})
        self.assertEqual(inventory.snapshot(), before)
        self.assertNotIn("order-2", inventory.reservations)

    def test_duplicate_order_is_rejected(self):
        inventory = Inventory()
        inventory.add_stock("moss", 5)
        inventory.reserve("same", {"moss": 1})
        with self.assertRaises(ValueError):
            inventory.reserve("same", {"moss": 1})
        self.assertEqual(inventory.snapshot(), {"moss": 4})

    def test_snapshot_is_sorted_and_independent(self):
        inventory = Inventory()
        inventory.add_stock("zeta", 1)
        inventory.add_stock("alpha", 2)
        snapshot = inventory.snapshot()
        self.assertEqual(list(snapshot), ["alpha", "zeta"])
        snapshot["alpha"] = 99
        self.assertEqual(inventory.snapshot()["alpha"], 2)


if __name__ == "__main__":
    unittest.main()
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_workspace(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "inventory.py").write_text(INVENTORY_SOURCE, encoding="utf-8")
    (path / "test_inventory.py").write_text(TEST_SOURCE, encoding="utf-8")
    (path / "README.md").write_text("# Inventory benchmark\n\nRun `python -m unittest -v`.\n", encoding="utf-8")
    return sha256(path / "test_inventory.py")


async def run_trial(mode: str, trial: int, workspace: Path) -> dict[str, Any]:
    test_hash_before = create_workspace(workspace)
    events: list[AgentEvent] = []

    async def publish(event: AgentEvent) -> None:
        events.append(event)

    client = OpenAICompatibleClient(api_key=settings.api_key, base_url=settings.base_url, model=settings.model)
    runner = Orchestrator(
        session_id=f"benchmark-{mode}-{trial}",
        task=TASK,
        tools=ToolRegistry(Workspace(str(workspace)), command_timeout_seconds=settings.command_timeout_seconds),
        client=client,
        settings=settings,
        publish=publish,
        locale="zh-CN",
        execution_mode=mode,
        command_mode="auto",
    )
    started = time.perf_counter()
    error = ""
    try:
        completed = await runner.run()
    except Exception as reason:  # The report must retain unexpected failures instead of aborting all trials.
        completed = False
        error = f"{type(reason).__name__}: {reason}"
    elapsed = time.perf_counter() - started
    acceptance = subprocess.run(
        [sys.executable, "-m", "unittest", "-v"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    tool_events = [event for event in events if event.type == "tool_finished"]
    final_event = next((event for event in reversed(events) if event.type in {"task_finished", "task_failed"}), None)
    return {
        "mode": mode,
        "trial": trial,
        "completed": completed,
        "acceptance_passed": acceptance.returncode == 0,
        "tests_unchanged": sha256(workspace / "test_inventory.py") == test_hash_before,
        "elapsed_seconds": round(elapsed, 3),
        "llm_requests": client.request_count,
        "usage": client.usage_totals,
        "agent_roles": [event.role.value for event in events if event.type == "agent_started" and event.role],
        "tool_calls": len(tool_events),
        "tool_breakdown": {
            name: sum(1 for event in tool_events if event.payload.get("tool") == name)
            for name in ("list_files", "read_file", "search_text", "write_file", "run_command")
        },
        "changed_files": final_event.payload.get("changed_files", []) if final_event else [],
        "completion_source": final_event.payload.get("completion_source", "") if final_event else "",
        "final_event": final_event.type.value if final_event else "missing",
        "final_summary": final_event.summary if final_event else "",
        "acceptance_output": (acceptance.stdout + acceptance.stderr)[-3000:],
        "error": error,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run a reproducible single-vs-multi MossCode benchmark.")
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()
    if not settings.api_key:
        print("LLM_API_KEY is not configured; benchmark was not started.", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    benchmark_root = ROOT / "data" / "benchmarks" / stamp
    benchmark_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for trial in range(1, args.repeats + 1):
        for mode in ("single", "multi"):
            workspace = benchmark_root / f"{mode}-{trial}"
            result = await run_trial(mode, trial, workspace)
            results.append(result)
            print(json.dumps({key: result[key] for key in ("mode", "trial", "completed", "acceptance_passed", "elapsed_seconds", "llm_requests", "tool_calls")}, ensure_ascii=False), flush=True)

    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "model": settings.model,
        "task": TASK,
        "repeats": args.repeats,
        "results": results,
    }
    output = benchmark_root / "results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.keep_workspaces:
        for child in benchmark_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
    print(f"RESULTS={output}")
    return 0 if all(item["completed"] and item["acceptance_passed"] and item["tests_unchanged"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
