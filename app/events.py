"""SSE event types for the streaming protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentEvent:
    type: str
    data: dict[str, Any] | None = None
    message: str = ""

    def to_sse(self) -> str:
        payload: dict[str, Any] = {"type": self.type}
        if self.data is not None:
            payload["data"] = self.data
        if self.message:
            payload["message"] = self.message
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def start_event(issue_title: str = "", file_count: int = 0) -> AgentEvent:
    return AgentEvent(type="start", data={"title": issue_title, "file_count": file_count})


def session_event(session_id: str) -> AgentEvent:
    return AgentEvent(type="session", data={"session_id": session_id})


def tool_call_event(name: str, args: dict, iteration: int) -> AgentEvent:
    return AgentEvent(type="tool_call", data={"name": name, "args": args, "iteration": iteration})


def tool_result_event(name: str, result_preview: str) -> AgentEvent:
    return AgentEvent(type="tool_result", data={"name": name, "preview": result_preview[:300]})


def thinking_event(content: str) -> AgentEvent:
    return AgentEvent(type="thinking", data={"content": content[:500]})


def report_event(report: dict) -> AgentEvent:
    return AgentEvent(type="report", data=report)


def error_event(message: str) -> AgentEvent:
    return AgentEvent(type="error", message=message)


def done_event() -> AgentEvent:
    return AgentEvent(type="done")
