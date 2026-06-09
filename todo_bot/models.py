from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Task:
    task_id: str
    guild_id: str
    channel_id: str
    task_type: str
    creator_id: str
    owner_id: str
    title: str
    description: str | None
    status: str
    previous_status: str | None
    color: str
    priority: str
    tag: str | None
    due_at: datetime | None
    timezone: str
    repeat_rule: str | None
    repeat_end_at: datetime | None
    parent_series_id: str | None
    repeat_anchor_day: int | None
    repeat_generated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    completed_by: str | None
    deleted_at: datetime | None
    restore_until: datetime | None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_personal(self) -> bool:
        return self.task_type == "personal"

    @property
    def series_id(self) -> str:
        return self.parent_series_id or self.task_id


@dataclass(slots=True)
class Reminder:
    reminder_id: int
    task_id: str
    offset_minutes: int
    remind_at: datetime
    sent_at: datetime | None
    failed_at: datetime | None
    failure_reason: str | None
