from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from .time_utils import now_utc

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.connection.close()

    def init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('personal', 'shared')),
                creator_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                previous_status TEXT,
                color TEXT NOT NULL,
                priority TEXT NOT NULL,
                tag TEXT,
                due_at TEXT,
                timezone TEXT NOT NULL,
                repeat_rule TEXT,
                repeat_end_at TEXT,
                parent_series_id TEXT,
                repeat_anchor_day INTEGER,
                repeat_generated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                completed_by TEXT,
                deleted_at TEXT,
                restore_until TEXT
            );

            CREATE TABLE IF NOT EXISTS assignees (
                task_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (task_id, user_id),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                offset_minutes INTEGER NOT NULL,
                remind_at TEXT NOT NULL,
                sent_at TEXT,
                failed_at TEXT,
                failure_reason TEXT,
                UNIQUE(task_id, offset_minutes),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notification_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                task_id TEXT,
                guild_id TEXT,
                failure_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                surfaced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                task_id TEXT,
                guild_id TEXT,
                reason TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS repeat_skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_series_id TEXT NOT NULL,
                skipped_due_at TEXT,
                created_at TEXT NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_guild_owner ON tasks(guild_id, owner_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_repeat ON tasks(repeat_rule, due_at, repeat_generated_at);
            CREATE INDEX IF NOT EXISTS idx_assignees_user ON assignees(user_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent_at, remind_at);
            """
        )
        self.connection.commit()

    def backup(self, backup_dir: str | Path = "backups", keep: int = 7) -> Path | None:
        if not self.path.exists():
            return None
        destination_dir = Path(backup_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        timestamp = now_utc().strftime("%Y%m%d_%H%M%S")
        destination = destination_dir / f"{self.path.stem}_{timestamp}{self.path.suffix}"
        try:
            self.connection.commit()
            shutil.copy2(self.path, destination)
            backups = sorted(
                destination_dir.glob(f"{self.path.stem}_*{self.path.suffix}"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for old_backup in backups[keep:]:
                old_backup.unlink(missing_ok=True)
            logger.info("SQLiteバックアップを作成しました: %s", destination)
            return destination
        except OSError:
            logger.exception("SQLiteバックアップに失敗しました")
            return None

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return cursor

    def executemany(self, sql: str, params: list[tuple]) -> None:
        self.connection.executemany(sql, params)
        self.connection.commit()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())
