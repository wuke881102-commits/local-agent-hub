"""Agent 基类与注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol


@dataclass
class AgentContext:
    task_id: str
    agent_id: str
    inputs: dict[str, Any]
    lark: Any                # LarkCLI | MockLarkCLI
    llm: Any                 # LLMClient
    config: dict[str, Any] = field(default_factory=dict)
    emit: Callable[[str, str], Awaitable[None]] | None = None  # (level, message) → None

    async def log(self, level: str, message: str) -> None:
        if self.emit:
            await self.emit(level, message)


@dataclass
class AgentResult:
    task_id: str
    status: str                       # preview | done | failed
    result_path: str | None = None    # 草稿 HTML / JSON 路径
    payload: dict[str, Any] = field(default_factory=dict)
    writeback_proposal: dict[str, Any] | None = None
    error: str | None = None


class Agent(Protocol):
    id: str
    name: str
    description: str
    writeback_allowed: bool

    async def run(self, ctx: AgentContext) -> AgentResult:
        ...


AGENT_REGISTRY: dict[str, Agent] = {}


def register_agent(agent: Agent) -> Agent:
    AGENT_REGISTRY[agent.id] = agent
    return agent


def get_agent(agent_id: str) -> Agent | None:
    return AGENT_REGISTRY.get(agent_id)
