"""Pydantic models for Neon Sync Automations, Scheduled Monitors & Webhook Delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TriggerType = Literal["manual", "interval", "webhook"]
ActionType = Literal["agent_prompt", "extract_preset", "ai_brief"]


class AutomationTemplate(BaseModel):
    id: str
    name: str
    description: str
    action_type: ActionType
    trigger_type: TriggerType
    interval_minutes: int | None = None
    prompt: str | None = None
    preset_id: str | None = None
    badge: str = "AI"


class AutomationCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    target_url: str = Field(min_length=4, max_length=4096)
    trigger_type: TriggerType = "manual"
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    action_type: ActionType = "agent_prompt"
    prompt: str | None = Field(default=None, max_length=1500)
    preset_id: str | None = Field(default=None, max_length=64)
    webhook_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True


class AutomationUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    prompt: str | None = Field(default=None, max_length=1500)
    webhook_url: str | None = Field(default=None, max_length=2048)


class AutomationRule(BaseModel):
    id: str
    owner_id: str = "workspace"
    name: str
    target_url: str
    trigger_type: TriggerType
    interval_minutes: int | None = None
    action_type: ActionType
    prompt: str | None = None
    preset_id: str | None = None
    webhook_url: str | None = None
    enabled: bool = True
    run_count: int = 0
    last_status: str | None = None
    last_summary: str | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime


class AutomationRunRecord(BaseModel):
    id: str
    automation_id: str
    trigger: Literal["manual", "scheduled", "webhook"]
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None = None
    summary: str = ""
    job_ids: list[str] = Field(default_factory=list)
    webhook_status: str | None = None
    error: str | None = None
    output_preview: dict[str, Any] = Field(default_factory=dict)
