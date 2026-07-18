from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .constants import STATUS_CANCELED, STATUS_DONE
from .db import Database
from .models import Reminder, Task
from .time_utils import now_utc


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class TaskRepository:
    def __init__(self, database: Database):
        self.db = database

    def next_task_id(self) -> str:
        connection = self.db.connection
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'last_task_number'"
        ).fetchone()
        number = int(row["value"]) + 1 if row else 1
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('last_task_number', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(number),),
        )
        connection.commit()
        return f"T-{number:06d}"

    def create_task(
        self, task: Task, assignee_ids: list[str], reminder_offsets: list[int]
    ) -> None:
        self.db.connection.execute(
            """
            INSERT INTO tasks (
                task_id, guild_id, channel_id, task_type, creator_id, owner_id,
                title, description, status, previous_status, color, priority, tag,
                due_at, timezone, repeat_rule, repeat_end_at, parent_series_id,
                repeat_anchor_day, repeat_generated_at, created_at, updated_at,
                completed_at, completed_by, deleted_at, restore_until
            ) VALUES (
                :task_id, :guild_id, :channel_id, :task_type, :creator_id, :owner_id,
                :title, :description, :status, :previous_status, :color, :priority, :tag,
                :due_at, :timezone, :repeat_rule, :repeat_end_at, :parent_series_id,
                :repeat_anchor_day, :repeat_generated_at, :created_at, :updated_at,
                :completed_at, :completed_by, :deleted_at, :restore_until
            )
            """,
            self._task_to_params(task),
        )
        self._replace_assignees_without_commit(task.task_id, assignee_ids)
        self._replace_reminders_without_commit(
            task.task_id, task.due_at, reminder_offsets
        )
        self.db.connection.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self.db.query_one(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id.upper(),)
        )
        return self._row_to_task(row) if row else None

    def get_assignees(self, task_id: str) -> list[str]:
        rows = self.db.query_all(
            "SELECT user_id FROM assignees WHERE task_id = ? ORDER BY user_id",
            (task_id,),
        )
        return [row["user_id"] for row in rows]

    def get_reminders(self, task_id: str) -> list[Reminder]:
        rows = self.db.query_all(
            "SELECT * FROM reminders WHERE task_id = ? ORDER BY remind_at", (task_id,)
        )
        return [self._row_to_reminder(row) for row in rows]

    def reminder_offsets(self, task_id: str) -> list[int]:
        rows = self.db.query_all(
            "SELECT offset_minutes FROM reminders WHERE task_id = ? ORDER BY offset_minutes",
            (task_id,),
        )
        return [int(row["offset_minutes"]) for row in rows]

    def update_task(self, task_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        serialized = {
            key: _serialize_datetime(value) if isinstance(value, datetime) else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key} = :{key}" for key in serialized)
        serialized["task_id"] = task_id
        self.db.execute(
            f"UPDATE tasks SET {assignments} WHERE task_id = :task_id", serialized
        )

    def replace_assignees(self, task_id: str, assignee_ids: list[str]) -> None:
        self._replace_assignees_without_commit(task_id, assignee_ids)
        self.db.connection.commit()

    def replace_reminders(
        self, task_id: str, due_at: datetime | None, reminder_offsets: list[int]
    ) -> None:
        self._replace_reminders_without_commit(task_id, due_at, reminder_offsets)
        self.db.connection.commit()

    def list_user_tasks(
        self,
        *,
        guild_id: str,
        user_id: str,
        status_filter: str | None,
        tag_filter: str | None,
        relation_filter: str | None,
        include_deleted: bool = False,
    ) -> list[Task]:
        params: list[Any] = [guild_id, user_id]
        relation_sql = "t.owner_id = ? OR EXISTS (SELECT 1 FROM assignees a WHERE a.task_id = t.task_id AND a.user_id = ?)"
        params.append(user_id)
        if relation_filter == "owner":
            relation_sql = "t.owner_id = ?"
            params = [guild_id, user_id]
        elif relation_filter == "assignee":
            relation_sql = "EXISTS (SELECT 1 FROM assignees a WHERE a.task_id = t.task_id AND a.user_id = ?)"
            params = [guild_id, user_id]

        conditions = ["t.guild_id = ?", f"({relation_sql})"]
        if include_deleted:
            conditions.append("t.deleted_at IS NOT NULL")
        else:
            conditions.append("t.deleted_at IS NULL")

        if status_filter:
            conditions.append("t.status = ?")
            params.append(status_filter)
        if tag_filter == "__NONE__":
            conditions.append("t.tag IS NULL")
        elif tag_filter:
            conditions.append("t.tag = ?")
            params.append(tag_filter)

        sql = f"SELECT DISTINCT t.* FROM tasks t WHERE {' AND '.join(conditions)}"
        rows = self.db.query_all(sql, tuple(params))
        return [self._row_to_task(row) for row in rows]

    def due_reminders(self, at: datetime) -> list[tuple[Reminder, Task]]:
        rows = self.db.query_all(
            """
            SELECT r.*, t.task_id AS t_task_id, t.guild_id, t.channel_id, t.task_type,
                   t.creator_id, t.owner_id, t.title, t.description, t.status,
                   t.previous_status, t.color, t.priority, t.tag, t.due_at, t.timezone,
                   t.repeat_rule, t.repeat_end_at, t.parent_series_id, t.repeat_anchor_day,
                   t.repeat_generated_at, t.created_at, t.updated_at, t.completed_at,
                   t.completed_by, t.deleted_at, t.restore_until
            FROM reminders r
            JOIN tasks t ON t.task_id = r.task_id
            WHERE r.sent_at IS NULL
              AND r.remind_at <= ?
              AND t.deleted_at IS NULL
              AND t.status NOT IN (?, ?)
            ORDER BY r.remind_at
            """,
            (_serialize_datetime(at), STATUS_DONE, STATUS_CANCELED),
        )
        result: list[tuple[Reminder, Task]] = []
        for row in rows:
            reminder = self._row_to_reminder(row)
            task = self._joined_row_to_task(row)
            result.append((reminder, task))
        return result

    def mark_reminder_sent(self, reminder_id: int, sent_at: datetime) -> None:
        self.db.execute(
            "UPDATE reminders SET sent_at = ? WHERE id = ?",
            (_serialize_datetime(sent_at), reminder_id),
        )

    def mark_reminder_failed(
        self, reminder_id: int, failed_at: datetime, reason: str
    ) -> None:
        self.db.execute(
            "UPDATE reminders SET failed_at = ?, failure_reason = ? WHERE id = ?",
            (_serialize_datetime(failed_at), reason[:500], reminder_id),
        )

    def add_notification_failure(
        self,
        *,
        user_id: str,
        task_id: str | None,
        guild_id: str | None,
        failure_type: str,
        message: str,
        created_at: datetime,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO notification_failures(user_id, task_id, guild_id, failure_type, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                task_id,
                guild_id,
                failure_type,
                message[:500],
                _serialize_datetime(created_at),
            ),
        )

    def unsurfaced_failures(
        self, user_id: str, guild_id: str | None
    ) -> list[dict[str, str]]:
        rows = self.db.query_all(
            """
            SELECT * FROM notification_failures
            WHERE user_id = ? AND surfaced_at IS NULL AND (guild_id = ? OR guild_id IS NULL)
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id, guild_id),
        )
        return [dict(row) for row in rows]

    def mark_failures_surfaced(
        self, user_id: str, guild_id: str | None, surfaced_at: datetime
    ) -> None:
        self.db.execute(
            """
            UPDATE notification_failures
            SET surfaced_at = ?
            WHERE user_id = ? AND surfaced_at IS NULL AND (guild_id = ? OR guild_id IS NULL)
            """,
            (_serialize_datetime(surfaced_at), user_id, guild_id),
        )

    def add_audit_log(
        self,
        *,
        action: str,
        actor_id: str,
        task_id: str | None,
        guild_id: str | None,
        reason: str | None,
        details: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO audit_logs(action, actor_id, task_id, guild_id, reason, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                actor_id,
                task_id,
                guild_id,
                reason,
                details,
                _serialize_datetime(created_at or now_utc()),
            ),
        )

    def repeat_candidates(self, at: datetime) -> list[Task]:
        rows = self.db.query_all(
            """
            SELECT * FROM tasks
            WHERE repeat_rule IS NOT NULL
              AND due_at IS NOT NULL
              AND due_at <= ?
              AND deleted_at IS NULL
              AND repeat_generated_at IS NULL
            ORDER BY due_at
            """,
            (_serialize_datetime(at),),
        )
        return [self._row_to_task(row) for row in rows]

    def add_repeat_skip(
        self, parent_series_id: str, reason: str, created_at: datetime
    ) -> None:
        self.db.execute(
            """
            INSERT INTO repeat_skips(parent_series_id, created_at, reason)
            VALUES (?, ?, ?)
            """,
            (parent_series_id, _serialize_datetime(created_at), reason),
        )

    def purge_expired_deleted(self, at: datetime) -> int:
        cursor = self.db.execute(
            "DELETE FROM tasks WHERE deleted_at IS NOT NULL AND restore_until IS NOT NULL AND restore_until < ?",
            (_serialize_datetime(at),),
        )
        return cursor.rowcount

    def _replace_assignees_without_commit(
        self, task_id: str, assignee_ids: list[str]
    ) -> None:
        self.db.connection.execute(
            "DELETE FROM assignees WHERE task_id = ?", (task_id,)
        )
        self.db.connection.executemany(
            "INSERT INTO assignees(task_id, user_id) VALUES (?, ?)",
            [(task_id, user_id) for user_id in dict.fromkeys(assignee_ids)],
        )

    def _replace_reminders_without_commit(
        self,
        task_id: str,
        due_at: datetime | None,
        reminder_offsets: list[int],
    ) -> None:
        self.db.connection.execute(
            "DELETE FROM reminders WHERE task_id = ?", (task_id,)
        )
        if due_at is None:
            return
        params = []
        for offset in dict.fromkeys(reminder_offsets):
            remind_at = due_at if offset == 0 else due_at - timedelta(minutes=offset)
            params.append((task_id, offset, _serialize_datetime(remind_at)))
        self.db.connection.executemany(
            "INSERT INTO reminders(task_id, offset_minutes, remind_at) VALUES (?, ?, ?)",
            params,
        )

    def _task_to_params(self, task: Task) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "guild_id": task.guild_id,
            "channel_id": task.channel_id,
            "task_type": task.task_type,
            "creator_id": task.creator_id,
            "owner_id": task.owner_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "previous_status": task.previous_status,
            "color": task.color,
            "priority": task.priority,
            "tag": task.tag,
            "due_at": _serialize_datetime(task.due_at),
            "timezone": task.timezone,
            "repeat_rule": task.repeat_rule,
            "repeat_end_at": _serialize_datetime(task.repeat_end_at),
            "parent_series_id": task.parent_series_id,
            "repeat_anchor_day": task.repeat_anchor_day,
            "repeat_generated_at": _serialize_datetime(task.repeat_generated_at),
            "created_at": _serialize_datetime(task.created_at),
            "updated_at": _serialize_datetime(task.updated_at),
            "completed_at": _serialize_datetime(task.completed_at),
            "completed_by": task.completed_by,
            "deleted_at": _serialize_datetime(task.deleted_at),
            "restore_until": _serialize_datetime(task.restore_until),
        }

    def _row_to_task(self, row: Any) -> Task:
        return Task(
            task_id=row["task_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            task_type=row["task_type"],
            creator_id=row["creator_id"],
            owner_id=row["owner_id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            previous_status=row["previous_status"],
            color=row["color"],
            priority=row["priority"],
            tag=row["tag"],
            due_at=_parse_datetime(row["due_at"]),
            timezone=row["timezone"],
            repeat_rule=row["repeat_rule"],
            repeat_end_at=_parse_datetime(row["repeat_end_at"]),
            parent_series_id=row["parent_series_id"],
            repeat_anchor_day=row["repeat_anchor_day"],
            repeat_generated_at=_parse_datetime(row["repeat_generated_at"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            completed_at=_parse_datetime(row["completed_at"]),
            completed_by=row["completed_by"],
            deleted_at=_parse_datetime(row["deleted_at"]),
            restore_until=_parse_datetime(row["restore_until"]),
        )

    def _joined_row_to_task(self, row: Any) -> Task:
        mapping = dict(row)
        mapping["task_id"] = mapping.pop("t_task_id")
        return self._row_to_task(mapping)

    def _row_to_reminder(self, row: Any) -> Reminder:
        return Reminder(
            reminder_id=row["id"],
            task_id=row["task_id"],
            offset_minutes=int(row["offset_minutes"]),
            remind_at=_parse_datetime(row["remind_at"]),
            sent_at=_parse_datetime(row["sent_at"]),
            failed_at=_parse_datetime(row["failed_at"]),
            failure_reason=row["failure_reason"],
        )

    def list_series_tasks(self, series_id: str) -> list[Task]:
        rows = self.db.query_all(
            """
            SELECT * FROM tasks
            WHERE task_id = ? OR parent_series_id = ?
            ORDER BY due_at, created_at
            """,
            (series_id, series_id),
        )
        return [self._row_to_task(row) for row in rows]
