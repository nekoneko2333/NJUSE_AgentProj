from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import ConversationTurn


@dataclass(frozen=True)
class ContextWindow:
    text: str
    memory_summary: str
    estimated_tokens: int


class ContextManager:
    """构造有界多轮上下文，并将较旧轮次压缩为可持久化摘要。"""

    def __init__(self, *, budget_chars: int = 12000, recent_turns: int = 3, summary_chars: int = 4000) -> None:
        self.budget_chars = max(budget_chars, 2000)
        self.recent_turns = max(recent_turns, 1)
        self.summary_chars = max(summary_chars, 800)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        clean = " ".join(text.split())
        return clean if len(clean) <= limit else clean[: limit - 1] + "…"

    def build(self, turns: list[ConversationTurn], shared_memory: str = "", shared_preferences: str = "") -> ContextWindow:
        # 失败、取消和中断的轮次仍包含用户原始要求；丢弃它们会让“重试/继续”失去指代对象。
        terminal = [turn for turn in turns if turn.status in {"finished", "failed", "cancelled", "interrupted"}]
        older, recent = terminal[:-self.recent_turns], terminal[-self.recent_turns:]
        summary_lines = [
            f"第{turn.position}轮（{turn.status}） 用户：{self._clip(turn.user_content, 280)} 结果：{self._clip(turn.assistant_summary, 520)}"
            for turn in older
        ]
        memory_summary = self._clip("\n".join(summary_lines), self.summary_chars) if summary_lines else ""
        sections: list[str] = []
        if shared_preferences.strip():
            sections.append("同一工作区的实时用户偏好（用户已授权；按新到旧排列，每类第一条是当前值并覆盖旧对话中的冲突内容；除非当前要求再次更新，否则必须遵循）：\n" + self._clip(shared_preferences, min(1800, self.summary_chars)))
        if shared_memory.strip():
            sections.append("同一工作区的其他对话记忆（用户已授权，仅作参考）：\n" + self._clip(shared_memory, min(3200, self.summary_chars)))
        if memory_summary:
            sections.append("较早对话摘要：\n" + memory_summary)
        if recent:
            recent_lines = [
                f"第{turn.position}轮（{turn.status}）\n用户：{self._clip(turn.user_content, 900)}\n结果：{self._clip(turn.assistant_summary, 1400)}"
                for turn in recent
            ]
            sections.append("最近对话：\n" + "\n\n".join(recent_lines))
        text = "\n\n".join(sections) or "这是该会话的第一轮任务。"
        if len(text) > self.budget_chars:
            text = text[-self.budget_chars:]
        return ContextWindow(text=text, memory_summary=memory_summary, estimated_tokens=max(1, len(text) // 4))

    def trim_role_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if sum(len(str(message.get("content", ""))) for message in messages) <= self.budget_chars:
            return messages
        prefix, tail = messages[:2], messages[2:]
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for message in tail:
            if message.get("role") == "assistant":
                if current:
                    groups.append(current)
                current = [message]
            elif message.get("role") == "tool" and current:
                current.append(message)
            else:
                if current:
                    groups.append(current)
                    current = []
                groups.append([message])
        if current:
            groups.append(current)

        kept_groups: list[list[dict[str, Any]]] = []
        used = sum(len(str(message.get("content", ""))) for message in prefix)
        for group in reversed(groups):
            size = sum(len(str(message.get("content", ""))) for message in group)
            if kept_groups and used + size > self.budget_chars:
                break
            kept_groups.append(group)
            used += size
        kept = [message for group in reversed(kept_groups) for message in group]
        return [*prefix, *kept]
