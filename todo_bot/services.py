from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .constants import (
    ACTIVE_STATUSES,
    COLORS,
    DELETE_SCOPE_FUTURE,
    DELETE_SCOPE_SERIES,
    MAX_ASSIGNEES,
    MAX_DESCRIPTION_LENGTH,
    MAX_REMINDERS,
    MAX_TITLE_LENGTH,
    PRIORITIES,
    PRIORITY_SORT,
    REMINDER_NONE_LABEL,
    REMINDER_OFFSETS,
    REPEAT_NONE_LABEL,
    REPEAT_RULE_VALUES,
    RESTORE_DAYS,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_TODO,
    STATUSES,
    TAG_ALL_LABEL,
    TAG_NONE_LABEL,
    TAGS,
    TASK_TYPE_PERSONAL,
    TASK_TYPE_SHARED,
)
from .models import Task
from .repository import TaskRepository
from .time_utils import (
    add_repeat_period,
    format_user_datetime,
    latest_repeat_due_after,
    now_utc,
    parse_user_date_end,
    parse_user_datetime,
    to_jst,
)

UNSET = object()
MENTION_RE = re.compile(r"<@([!&]?\d+)>")


class UserFacingError(Exception):
    """利用者へそのまま表示してよいエラーです。"""


class PermissionDenied(UserFacingError):
    pass


def sanitize_mentions(value: str) -> str:
    text = value.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return MENTION_RE.sub(lambda match: f"<@\u200b{match.group(1)}>", text)


def normalize_task_id(task_id: str) -> str:
    return task_id.strip().upper()


def normalize_title(title: str) -> str:
    text = sanitize_mentions(title.strip())
    if not text:
        raise UserFacingError("タスク名を入力してください。空白だけの名前は使えません。")
    if len(text) > MAX_TITLE_LENGTH:
        raise UserFacingError(f"タスク名は{MAX_TITLE_LENGTH}文字以内で入力してください。")
    return text


def normalize_description(description: str | None) -> str | None:
    if description is None:
        return None
    text = sanitize_mentions(description.strip())
    if not text:
        return None
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise UserFacingError(f"タスク内容は{MAX_DESCRIPTION_LENGTH}文字以内で入力してください。")
    return text


def normalize_tag(label: str | None) -> str | None:
    if label in (None, "", TAG_NONE_LABEL, TAG_ALL_LABEL):
        return None
    if label not in TAGS:
        raise UserFacingError("タグは「勉強」「仕事」「趣味」「タグなし」から選んでください。")
    return label


def normalize_reminders(labels: list[str], due_at: datetime | None) -> list[int]:
    offsets: list[int] = []
    for label in labels:
        if not label or label == REMINDER_NONE_LABEL:
            continue
        if label not in REMINDER_OFFSETS:
            raise UserFacingError("不明なリマインド設定です。")
        offset = REMINDER_OFFSETS[label]
        if offset is not None:
            offsets.append(offset)
    unique_offsets = list(dict.fromkeys(offsets))
    if len(unique_offsets) > MAX_REMINDERS:
        raise UserFacingError(f"リマインドは最大{MAX_REMINDERS}件までです。")
    if unique_offsets and due_at is None:
        raise UserFacingError("期限がないタスクにはリマインドを設定できません。")
    return unique_offsets


def normalize_repeat(label: str | None, due_at: datetime | None) -> str | None:
    if label in (None, "", REPEAT_NONE_LABEL):
        return None
    if label not in REPEAT_RULE_VALUES:
        raise UserFacingError("繰り返しは「なし」「毎日」「毎週」「毎月」から選んでください。")
    rule = REPEAT_RULE_VALUES[label]
    if rule and due_at is None:
        raise UserFacingError("繰り返しタスクには期限が必要です。")
    return rule


def normalize_assignees(user_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(user_id) for user_id in user_ids if str(user_id).strip()))
    if len(normalized) > MAX_ASSIGNEES:
        raise UserFacingError(f"担当者は最大{MAX_ASSIGNEES}人までです。")
    return normalized


class TaskService:
    def __init__(self, repository: TaskRepository):
        self.repo = repository

    def create_task(
        self,
        *,
        guild_id: int | str,
        channel_id: int | str,
        creator_id: int | str,
        task_type: str,
        title: str,
        description: str | None = None,
        due_at_text: str | None = None,
        priority: str = "中",
        tag: str | None = None,
        color: str = "デフォルト",
        reminder_labels: list[str] | None = None,
        repeat_label: str | None = None,
        repeat_end_date_text: str | None = None,
        assignee_ids: list[int | str] | None = None,
        current_time: datetime | None = None,
    ) -> Task:
        now = current_time or now_utc()
        if task_type not in (TASK_TYPE_PERSONAL, TASK_TYPE_SHARED):
            raise UserFacingError("タスク種別は「個人」または「共有」から選んでください。")
        if priority not in PRIORITIES:
            raise UserFacingError("優先度は「高」「中」「低」から選んでください。")
        if color not in COLORS:
            raise UserFacingError("不明な色です。")

        due_at = parse_user_datetime(due_at_text, allow_past=False, now=now) if due_at_text else None
        repeat_end_at = parse_user_date_end(repeat_end_date_text) if repeat_end_date_text else None
        repeat_rule = normalize_repeat(repeat_label, due_at)
        if repeat_rule and repeat_end_at and due_at and repeat_end_at < due_at:
            raise UserFacingError("繰り返し終了日は期限日以降にしてください。")

        task_id = self.repo.next_task_id()
        creator = str(creator_id)
        assignees = normalize_assignees([creator] + [str(item) for item in (assignee_ids or [])])
        reminder_offsets = normalize_reminders(reminder_labels or [], due_at)
        anchor_day = to_jst(due_at).day if due_at and repeat_rule == "monthly" else None
        task = Task(
            task_id=task_id,
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            task_type=task_type,
            creator_id=creator,
            owner_id=creator,
            title=normalize_title(title),
            description=normalize_description(description),
            status=STATUS_TODO,
            previous_status=None,
            color=color,
            priority=priority,
            tag=normalize_tag(tag),
            due_at=due_at,
            timezone="Asia/Tokyo",
            repeat_rule=repeat_rule,
            repeat_end_at=repeat_end_at,
            parent_series_id=task_id if repeat_rule else None,
            repeat_anchor_day=anchor_day,
            repeat_generated_at=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            completed_by=None,
            deleted_at=None,
            restore_until=None,
        )
        self.repo.create_task(task, assignees, reminder_offsets)
        return task

    def edit_task(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        title: str | object = UNSET,
        description: str | None | object = UNSET,
        due_at_text: str | None | object = UNSET,
        priority: str | object = UNSET,
        tag: str | None | object = UNSET,
        color: str | object = UNSET,
        status: str | object = UNSET,
        reminder_labels: list[str] | object = UNSET,
        repeat_label: str | None | object = UNSET,
        repeat_end_date_text: str | None | object = UNSET,
        assignee_ids: list[int | str] | object = UNSET,
        current_time: datetime | None = None,
    ) -> Task:
        now = current_time or now_utc()
        task = self._require_task(task_id, guild_id)
        self._require_owner(task, actor_id)
        if task.is_deleted:
            raise UserFacingError("削除済みタスクは編集できません。先に復元してください。")

        fields: dict[str, Any] = {"updated_at": now}
        new_due_at = task.due_at
        new_repeat_rule = task.repeat_rule

        if title is not UNSET:
            fields["title"] = normalize_title(str(title))
        if description is not UNSET:
            fields["description"] = normalize_description(description)
        if due_at_text is not UNSET:
            new_due_at = parse_user_datetime(due_at_text, allow_past=False, now=now) if due_at_text else None
            fields["due_at"] = new_due_at
            if new_due_at is None:
                new_repeat_rule = None
                fields["repeat_rule"] = None
                fields["repeat_end_at"] = None
                fields["repeat_anchor_day"] = None
        if priority is not UNSET:
            if priority not in PRIORITIES:
                raise UserFacingError("優先度は「高」「中」「低」から選んでください。")
            fields["priority"] = priority
        if tag is not UNSET:
            fields["tag"] = normalize_tag(tag)
        if color is not UNSET:
            if color not in COLORS:
                raise UserFacingError("不明な色です。")
            fields["color"] = color
        if status is not UNSET:
            if status not in STATUSES:
                raise UserFacingError("不明な状態です。")
            fields.update(self._status_fields(task, str(status), str(actor_id), now))
        if repeat_label is not UNSET:
            new_repeat_rule = normalize_repeat(repeat_label, new_due_at)
            fields["repeat_rule"] = new_repeat_rule
            fields["parent_series_id"] = task.series_id if new_repeat_rule else None
            fields["repeat_anchor_day"] = to_jst(new_due_at).day if new_due_at and new_repeat_rule == "monthly" else None
            fields["repeat_generated_at"] = None
        if repeat_end_date_text is not UNSET:
            repeat_end_at = parse_user_date_end(repeat_end_date_text) if repeat_end_date_text else None
            if repeat_end_at and new_due_at and repeat_end_at < new_due_at:
                raise UserFacingError("繰り返し終了日は期限日以降にしてください。")
            fields["repeat_end_at"] = repeat_end_at

        if new_repeat_rule and new_due_at is None:
            raise UserFacingError("繰り返しタスクには期限が必要です。")

        self.repo.update_task(task.task_id, fields)
        if assignee_ids is not UNSET:
            self.repo.replace_assignees(task.task_id, normalize_assignees([str(item) for item in assignee_ids]))
        if reminder_labels is not UNSET or due_at_text is not UNSET:
            if reminder_labels is UNSET:
                reminder_offsets = self.repo.reminder_offsets(task.task_id) if new_due_at else []
            else:
                reminder_offsets = normalize_reminders(reminder_labels, new_due_at)
            self.repo.replace_reminders(task.task_id, new_due_at, reminder_offsets)
        return self.repo.get_task(task.task_id)

    def change_status(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        status: str,
        current_time: datetime | None = None,
    ) -> Task:
        task = self._require_task(task_id, guild_id)
        self._require_owner_or_assignee(task, actor_id)
        if status not in STATUSES:
            raise UserFacingError("不明な状態です。")
        fields = self._status_fields(task, status, str(actor_id), current_time or now_utc())
        fields["updated_at"] = current_time or now_utc()
        self.repo.update_task(task.task_id, fields)
        return self.repo.get_task(task.task_id)

    def complete_task(self, *, guild_id: int | str, actor_id: int | str, task_id: str) -> Task:
        return self.change_status(guild_id=guild_id, actor_id=actor_id, task_id=task_id, status=STATUS_DONE)

    def uncomplete_task(self, *, guild_id: int | str, actor_id: int | str, task_id: str) -> Task:
        task = self._require_task(task_id, guild_id)
        self._require_owner_or_assignee(task, actor_id)
        if task.status != STATUS_DONE:
            raise UserFacingError("このタスクは完了状態ではありません。")
        now = now_utc()
        self.repo.update_task(
            task.task_id,
            {
                "status": task.previous_status or STATUS_TODO,
                "previous_status": None,
                "completed_at": None,
                "completed_by": None,
                "updated_at": now,
            },
        )
        return self.repo.get_task(task.task_id)

    def delete_task(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        scope: str = "this",
        reason: str | None = None,
        current_time: datetime | None = None,
    ) -> list[Task]:
        now = current_time or now_utc()
        task = self._require_task(task_id, guild_id)
        self._require_owner(task, actor_id)
        targets = self._delete_targets(task, scope)
        for target in targets:
            self.repo.update_task(
                target.task_id,
                {
                    "deleted_at": now,
                    "restore_until": now + timedelta(days=RESTORE_DAYS),
                    "updated_at": now,
                },
            )
        if task.repeat_rule and scope == "this" and task.due_at:
            self._create_next_repeat_now(task, now)
        self.repo.add_audit_log(
            action="delete",
            actor_id=str(actor_id),
            task_id=task.task_id,
            guild_id=str(guild_id),
            reason=reason,
            created_at=now,
        )
        return targets

    def restore_task(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        current_time: datetime | None = None,
    ) -> Task:
        now = current_time or now_utc()
        task = self._require_task(task_id, guild_id, include_deleted=True)
        self._require_owner(task, actor_id)
        if task.deleted_at is None:
            raise UserFacingError("このタスクは削除されていません。")
        if task.restore_until and task.restore_until < now:
            raise UserFacingError("復元期限を過ぎているため復元できません。")
        self.repo.update_task(
            task.task_id,
            {"deleted_at": None, "restore_until": None, "updated_at": now},
        )
        return self.repo.get_task(task.task_id)

    def transfer_owner(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        new_owner_id: int | str,
        is_admin: bool,
        reason: str | None = None,
    ) -> Task:
        if not is_admin:
            raise PermissionDenied("この操作にはDiscordの管理者権限が必要です。")
        task = self._require_task(task_id, guild_id, include_deleted=True)
        now = now_utc()
        self.repo.update_task(task.task_id, {"owner_id": str(new_owner_id), "updated_at": now})
        self.repo.add_audit_log(
            action="transfer_owner",
            actor_id=str(actor_id),
            task_id=task.task_id,
            guild_id=str(guild_id),
            reason=reason,
            details=f"new_owner_id={new_owner_id}",
            created_at=now,
        )
        return self.repo.get_task(task.task_id)

    def emergency_delete(
        self,
        *,
        guild_id: int | str,
        actor_id: int | str,
        task_id: str,
        is_admin: bool,
        reason: str,
    ) -> Task:
        if not is_admin:
            raise PermissionDenied("この操作にはDiscordの管理者権限が必要です。")
        if not reason or not reason.strip():
            raise UserFacingError("緊急削除では削除理由が必須です。")
        task = self._require_task(task_id, guild_id, include_deleted=True)
        now = now_utc()
        self.repo.update_task(
            task.task_id,
            {
                "deleted_at": now,
                "restore_until": now + timedelta(days=RESTORE_DAYS),
                "updated_at": now,
            },
        )
        self.repo.add_audit_log(
            action="emergency_delete",
            actor_id=str(actor_id),
            task_id=task.task_id,
            guild_id=str(guild_id),
            reason=reason.strip(),
            created_at=now,
        )
        return self.repo.get_task(task.task_id)

    def list_tasks(
        self,
        *,
        guild_id: int | str,
        user_id: int | str,
        status_filter: str | None = None,
        tag_filter: str | None = None,
        relation_filter: str | None = None,
        include_deleted: bool = False,
    ) -> list[Task]:
        tag_value = None
        if tag_filter == TAG_NONE_LABEL:
            tag_value = "__NONE__"
        elif tag_filter and tag_filter != TAG_ALL_LABEL:
            tag_value = normalize_tag(tag_filter)
        effective_status = None if status_filter in (None, "未完了", "すべて", "削除済み") else status_filter
        tasks = self.repo.list_user_tasks(
            guild_id=str(guild_id),
            user_id=str(user_id),
            status_filter=effective_status,
            tag_filter=tag_value,
            relation_filter=relation_filter,
            include_deleted=include_deleted,
        )
        if status_filter in (None, "未完了"):
            tasks = [task for task in tasks if task.status in ACTIVE_STATUSES]
        return sorted(tasks, key=lambda task: self._sort_key(task))

    def get_visible_task(self, *, guild_id: int | str, actor_id: int | str, task_id: str) -> Task:
        task = self._require_task(task_id, guild_id, include_deleted=True)
        self._require_view(task, actor_id)
        return task

    def get_admin_task(self, *, guild_id: int | str, task_id: str, is_admin: bool) -> Task:
        if not is_admin:
            raise PermissionDenied("この操作にはDiscordの管理者権限が必要です。")
        return self._require_task(task_id, guild_id, include_deleted=True)

    def generate_repeats(self, current_time: datetime | None = None) -> list[Task]:
        now = current_time or now_utc()
        generated: list[Task] = []
        for task in self.repo.repeat_candidates(now):
            if task.status == STATUS_CANCELED:
                continue
            latest_due, skipped_count = latest_repeat_due_after(
                task.due_at,
                task.repeat_rule,
                now=now,
                repeat_end_at=task.repeat_end_at,
                anchor_day=task.repeat_anchor_day,
            )
            if latest_due is None:
                continue
            new_task = self._copy_for_repeat(task, latest_due, now)
            assignees = self.repo.get_assignees(task.task_id)
            reminder_offsets = self.repo.reminder_offsets(task.task_id)
            self.repo.create_task(new_task, assignees, reminder_offsets)
            self.repo.update_task(task.task_id, {"repeat_generated_at": now, "updated_at": now})
            if skipped_count:
                self.repo.add_repeat_skip(
                    task.series_id,
                    f"Bot停止中に{skipped_count}件の繰り返し予定をスキップしました。",
                    now,
                )
            generated.append(new_task)
        return generated

    def cleanup_expired_deleted(self, current_time: datetime | None = None) -> int:
        return self.repo.purge_expired_deleted(current_time or now_utc())

    def _require_task(self, task_id: str, guild_id: int | str, *, include_deleted: bool = False) -> Task:
        task = self.repo.get_task(normalize_task_id(task_id))
        if task is None or task.guild_id != str(guild_id):
            raise UserFacingError("指定されたタスクIDが見つかりません。")
        if task.is_deleted and not include_deleted:
            raise UserFacingError("指定されたタスクは削除済みです。復元してから操作してください。")
        return task

    def _require_view(self, task: Task, actor_id: int | str) -> None:
        actor = str(actor_id)
        assignees = self.repo.get_assignees(task.task_id)
        if task.task_type == TASK_TYPE_SHARED and not task.is_deleted:
            return
        if actor in {task.owner_id, task.creator_id} or actor in assignees:
            return
        raise PermissionDenied("このタスクを表示する権限がありません。")

    def _require_owner(self, task: Task, actor_id: int | str) -> None:
        if task.owner_id != str(actor_id):
            raise PermissionDenied("この操作はタスク所有者だけが実行できます。")

    def _require_owner_or_assignee(self, task: Task, actor_id: int | str) -> None:
        actor = str(actor_id)
        assignees = self.repo.get_assignees(task.task_id)
        if actor in {task.owner_id, task.creator_id} or actor in assignees:
            return
        raise PermissionDenied("この操作を実行する権限がありません。")

    def _status_fields(self, task: Task, status: str, actor_id: str, now: datetime) -> dict[str, Any]:
        if status == STATUS_DONE:
            return {
                "status": STATUS_DONE,
                "previous_status": task.status if task.status != STATUS_DONE else task.previous_status,
                "completed_at": now,
                "completed_by": actor_id,
            }
        fields: dict[str, Any] = {"status": status}
        if task.status == STATUS_DONE:
            fields["completed_at"] = None
            fields["completed_by"] = None
            fields["previous_status"] = None
        return fields

    def _delete_targets(self, task: Task, scope: str) -> list[Task]:
        if scope == DELETE_SCOPE_SERIES:
            return [item for item in self.repo.list_series_tasks(task.series_id) if not item.is_deleted]
        if scope == DELETE_SCOPE_FUTURE:
            series = self.repo.list_series_tasks(task.series_id)
            return [
                item
                for item in series
                if not item.is_deleted and (item.due_at is None or task.due_at is None or item.due_at >= task.due_at)
            ]
        return [task]

    def _create_next_repeat_now(self, task: Task, now: datetime) -> Task | None:
        if not task.repeat_rule or not task.due_at:
            return None
        next_due = add_repeat_period(task.due_at, task.repeat_rule, task.repeat_anchor_day)
        if task.repeat_end_at and next_due > task.repeat_end_at:
            return None
        new_task = self._copy_for_repeat(task, next_due, now)
        self.repo.create_task(new_task, self.repo.get_assignees(task.task_id), self.repo.reminder_offsets(task.task_id))
        self.repo.update_task(task.task_id, {"repeat_generated_at": now, "updated_at": now})
        return new_task

    def _copy_for_repeat(self, task: Task, due_at: datetime, created_at: datetime) -> Task:
        return replace(
            task,
            task_id=self.repo.next_task_id(),
            status=STATUS_TODO,
            previous_status=None,
            due_at=due_at,
            parent_series_id=task.series_id,
            repeat_generated_at=None,
            created_at=created_at,
            updated_at=created_at,
            completed_at=None,
            completed_by=None,
            deleted_at=None,
            restore_until=None,
        )

    def _sort_key(self, task: Task) -> tuple[int, datetime, int, datetime]:
        now = now_utc()
        if task.due_at and task.status in ACTIVE_STATUSES and task.due_at < now:
            overdue_group = 0
        elif task.due_at:
            overdue_group = 1
        else:
            overdue_group = 2
        due = task.due_at or datetime.max.replace(tzinfo=now.tzinfo)
        return (overdue_group, due, PRIORITY_SORT.get(task.priority, 9), task.created_at)


def task_line(task: Task) -> str:
    due_label = format_user_datetime(task.due_at)
    tag_label = task.tag or TAG_NONE_LABEL
    deleted = " / 削除済み" if task.is_deleted else ""
    return f"`{task.task_id}` {task.title} / {task.status}{deleted} / 期限: {due_label} / 優先度: {task.priority} / タグ: {tag_label}"
