from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class RunTaskRequest(BaseModel):
    agent_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    scene: str | None = None


class RunTaskResponse(BaseModel):
    task_id: str


class WritebackConfirmRequest(BaseModel):
    queue_id: str
    edits: dict[str, Any] | None = None  # 允许用户在确认前小幅编辑


class WritebackRejectRequest(BaseModel):
    queue_id: str
    reason: str | None = None
