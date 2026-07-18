from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from todo_bot.constants import (
    STATUS_DONE,
    STATUS_TODO,
    TASK_TYPE_PERSONAL,
    TASK_TYPE_SHARED,
    UTC,
)
from todo_bot.db import Database
from todo_bot.repository import TaskRepository
from todo_bot.services import PermissionDenied, TaskService, UserFacingError
from todo_bot.time_utils import format_user_datetime


class TaskServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "todo.sqlite3")
        database.init_schema()
        self.database = database
        self.service = TaskService(TaskRepository(database))

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def test_create_task_assigns_id_owner_assignee_and_reminders(self) -> None:
        task = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="資料作成",
            description="@everyone には通知しない",
            due_at_text="2099-01-01 10:00",
            reminder_labels=["期限時刻", "10分前", "10分前"],
            assignee_ids=[200],
        )

        self.assertEqual(task.task_id, "T-000001")
        self.assertEqual(task.owner_id, "100")
        self.assertEqual(self.service.repo.get_assignees(task.task_id), ["100", "200"])
        self.assertEqual(self.service.repo.reminder_offsets(task.task_id), [0, 10])
        self.assertIn(
            "@\u200beveryone", self.service.repo.get_task(task.task_id).description
        )

    def test_rejects_blank_title_and_too_many_assignees(self) -> None:
        with self.assertRaises(UserFacingError):
            self.service.create_task(
                guild_id=1,
                channel_id=10,
                creator_id=100,
                task_type=TASK_TYPE_PERSONAL,
                title="   ",
            )

        with self.assertRaises(UserFacingError):
            self.service.create_task(
                guild_id=1,
                channel_id=10,
                creator_id=100,
                task_type=TASK_TYPE_SHARED,
                title="担当者が多すぎる",
                assignee_ids=[101, 102, 103, 104, 105],
            )

    def test_owner_only_edit_but_assignee_can_complete(self) -> None:
        task = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_SHARED,
            title="レビュー",
            assignee_ids=[200],
        )

        with self.assertRaises(PermissionDenied):
            self.service.edit_task(
                guild_id=1, actor_id=200, task_id=task.task_id, title="変更"
            )

        completed = self.service.complete_task(
            guild_id=1, actor_id=200, task_id=task.task_id
        )
        self.assertEqual(completed.status, STATUS_DONE)
        self.assertEqual(completed.completed_by, "200")

        reopened = self.service.uncomplete_task(
            guild_id=1, actor_id=200, task_id=task.task_id
        )
        self.assertEqual(reopened.status, STATUS_TODO)
        self.assertIsNone(reopened.completed_by)

    def test_delete_and_restore_within_restore_period(self) -> None:
        task = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="消す予定",
        )

        deleted = self.service.delete_task(
            guild_id=1, actor_id=100, task_id=task.task_id
        )
        self.assertEqual(len(deleted), 1)
        self.assertIsNotNone(self.service.repo.get_task(task.task_id).deleted_at)

        restored = self.service.restore_task(
            guild_id=1, actor_id=100, task_id=task.task_id
        )
        self.assertIsNone(restored.deleted_at)
        self.assertIsNone(restored.restore_until)

    def test_default_list_excludes_completed_and_sorts_overdue_first(self) -> None:
        current = datetime(2026, 6, 9, 0, 0, tzinfo=UTC)
        overdue = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="期限切れ",
            due_at_text="2026-06-09 10:00",
            current_time=datetime(2026, 6, 8, 0, 0, tzinfo=UTC),
        )
        later = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="あとで",
            due_at_text="2099-01-01 10:00",
        )
        done = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="完了済み",
        )
        self.service.change_status(
            guild_id=1,
            actor_id=100,
            task_id=done.task_id,
            status=STATUS_DONE,
            current_time=current,
        )

        tasks = self.service.list_tasks(guild_id=1, user_id=100)
        self.assertEqual(
            [task.task_id for task in tasks], [overdue.task_id, later.task_id]
        )

    def test_monthly_repeat_generates_latest_only_after_long_stop(self) -> None:
        self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_PERSONAL,
            title="月末処理",
            due_at_text="2026-01-31 09:00",
            repeat_label="毎月",
            current_time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        )

        generated = self.service.generate_repeats(
            datetime(2026, 4, 2, 0, 0, tzinfo=UTC)
        )

        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].task_id, "T-000002")
        self.assertEqual(format_user_datetime(generated[0].due_at), "2026-03-31 09:00")
        self.assertIsNotNone(self.service.repo.get_task("T-000001").repeat_generated_at)

    def test_owner_can_replace_assignees_with_zero_people(self) -> None:
        task = self.service.create_task(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            task_type=TASK_TYPE_SHARED,
            title="担当解除",
        )
        self.service.edit_task(
            guild_id=1, actor_id=100, task_id=task.task_id, assignee_ids=[]
        )
        self.assertEqual(self.service.repo.get_assignees(task.task_id), [])


if __name__ == "__main__":
    unittest.main()
