from __future__ import annotations

import logging
from math import ceil
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .config import load_settings, validate_token
from .constants import (
    COLORS,
    DELETE_SCOPE_FUTURE,
    DELETE_SCOPE_LABELS,
    DELETE_SCOPE_OCCURRENCE,
    DELETE_SCOPE_SERIES,
    PAGE_SIZE,
    PRIORITIES,
    REMINDER_NONE_LABEL,
    REMINDER_OFFSETS,
    REPEAT_NONE_LABEL,
    REPEAT_RULE_LABELS,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_ON_HOLD,
    STATUS_TODO,
    STATUSES,
    TAG_ALL_LABEL,
    TAG_NONE_LABEL,
    TAGS,
    TASK_TYPE_LABELS,
    TASK_TYPE_PERSONAL,
    TASK_TYPE_SHARED,
)
from .db import Database
from .models import Reminder, Task
from .repository import TaskRepository
from .services import PermissionDenied, TaskService, UNSET, UserFacingError, task_line
from .time_utils import format_user_date, format_user_datetime, now_utc

logger = logging.getLogger(__name__)


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def choice_list(values: list[str]) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=value, value=value) for value in values]


TASK_TYPE_CHOICES = [
    app_commands.Choice(name="個人", value=TASK_TYPE_PERSONAL),
    app_commands.Choice(name="共有", value=TASK_TYPE_SHARED),
]
PRIORITY_CHOICES = choice_list(PRIORITIES)
COLOR_CHOICES = choice_list(list(COLORS.keys()))
TAG_CHOICES = choice_list([TAG_NONE_LABEL] + TAGS)
TAG_FILTER_CHOICES = choice_list([TAG_ALL_LABEL, TAG_NONE_LABEL] + TAGS)
STATUS_CHOICES = choice_list(STATUSES)
STATUS_FILTER_CHOICES = choice_list(["未完了", "すべて", "削除済み"] + STATUSES)
REMINDER_CHOICES = choice_list(list(REMINDER_OFFSETS.keys()))
REPEAT_CHOICES = choice_list([REPEAT_NONE_LABEL, "毎日", "毎週", "毎月"])
DELETE_SCOPE_CHOICES = [
    app_commands.Choice(name=label, value=value) for value, label in DELETE_SCOPE_LABELS.items()
]
RELATION_CHOICES = [
    app_commands.Choice(name="すべて", value="all"),
    app_commands.Choice(name="所有のみ", value="owner"),
    app_commands.Choice(name="担当のみ", value="assignee"),
]


class TodoDiscordBot(discord.Client):
    def __init__(self, database: Database, service: TaskService):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.database = database
        self.service = service

    async def setup_hook(self) -> None:
        self.database.init_schema()
        backup = self.database.backup()
        if backup is None:
            logger.warning("バックアップは作成されませんでした。既存DBがないか、バックアップに失敗しています。")
        self.tree.add_command(task_group)
        await self.tree.sync()
        self.reminder_loop.start()

    async def close(self) -> None:
        if self.reminder_loop.is_running():
            self.reminder_loop.cancel()
        self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Discordへ接続しました: %s", self.user)

    @tasks.loop(minutes=1)
    async def reminder_loop(self) -> None:
        current = now_utc()
        try:
            generated = self.service.generate_repeats(current)
            if generated:
                logger.info("繰り返しタスクを%d件生成しました", len(generated))
            self.service.cleanup_expired_deleted(current)
            for reminder, task in self.service.repo.due_reminders(current):
                await self._send_reminder(reminder, task)
        except Exception:
            logger.exception("リマインド処理中にエラーが発生しました")

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()

    async def _send_reminder(self, reminder: Reminder, task: Task) -> None:
        sent_at = now_utc()
        assignees = self.service.repo.get_assignees(task.task_id)
        recipient_ids = list(dict.fromkeys([task.creator_id] + assignees))
        mention_text = " ".join(f"<@{user_id}>" for user_id in recipient_ids)
        message = (
            f"{mention_text}\n"
            f"リマインド: `{task.task_id}` {task.title}\n"
            f"期限: {format_user_datetime(task.due_at)}"
        )
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        try:
            if task.task_type == TASK_TYPE_PERSONAL:
                failures = []
                for user_id in recipient_ids:
                    user = self.get_user(int(user_id)) or await self.fetch_user(int(user_id))
                    try:
                        await user.send(message, allowed_mentions=allowed)
                    except discord.DiscordException as exc:
                        failures.append(str(exc))
                        self.service.repo.add_notification_failure(
                            user_id=user_id,
                            task_id=task.task_id,
                            guild_id=task.guild_id,
                            failure_type="personal_dm_failed",
                            message="個人タスクのDM通知に失敗しました。",
                            created_at=sent_at,
                        )
                if failures:
                    self.service.repo.mark_reminder_failed(reminder.reminder_id, sent_at, "; ".join(failures))
                self.service.repo.mark_reminder_sent(reminder.reminder_id, sent_at)
                return

            channel = self.get_channel(int(task.channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(task.channel_id))
            if not hasattr(channel, "send"):
                raise RuntimeError("通知先チャンネルへ送信できません。")
            await channel.send(message, allowed_mentions=allowed)
            self.service.repo.mark_reminder_sent(reminder.reminder_id, sent_at)
        except Exception as exc:
            logger.warning("共有タスク通知に失敗しました task_id=%s error=%s", task.task_id, exc)
            try:
                creator = self.get_user(int(task.creator_id)) or await self.fetch_user(int(task.creator_id))
                await creator.send(
                    f"共有タスク `{task.task_id}` のチャンネル通知に失敗しました。\n"
                    f"タスク名: {task.title}\n期限: {format_user_datetime(task.due_at)}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException:
                self.service.repo.add_notification_failure(
                    user_id=task.creator_id,
                    task_id=task.task_id,
                    guild_id=task.guild_id,
                    failure_type="shared_channel_failed",
                    message="共有タスクのチャンネル通知と作成者DM通知に失敗しました。",
                    created_at=sent_at,
                )
            self.service.repo.mark_reminder_sent(reminder.reminder_id, sent_at)


async def respond_error(interaction: discord.Interaction, message: str) -> None:
    content = f"処理できませんでした: {message}"
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def warn_notification_failures(interaction: discord.Interaction, service: TaskService) -> None:
    if not interaction.guild:
        return
    failures = service.repo.unsurfaced_failures(str(interaction.user.id), str(interaction.guild.id))
    if not failures:
        return
    lines = ["未通知または送信失敗したリマインドがあります。"]
    for item in failures:
        task_id = item.get("task_id") or "不明"
        lines.append(f"- `{task_id}`: {item['message']}")
    service.repo.mark_failures_surfaced(str(interaction.user.id), str(interaction.guild.id), now_utc())
    await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def build_task_embed(
    task: Task,
    *,
    service: TaskService,
    title: str | None = None,
    include_description: bool = True,
) -> discord.Embed:
    assignees = service.repo.get_assignees(task.task_id)
    reminders = service.repo.reminder_offsets(task.task_id)
    embed = discord.Embed(
        title=title or f"{task.task_id} {task.title}",
        color=COLORS.get(task.color, COLORS["デフォルト"]),
    )
    embed.add_field(name="状態", value=task.status, inline=True)
    embed.add_field(name="種別", value=TASK_TYPE_LABELS.get(task.task_type, task.task_type), inline=True)
    embed.add_field(name="優先度", value=task.priority, inline=True)
    embed.add_field(name="期限", value=format_user_datetime(task.due_at), inline=True)
    embed.add_field(name="タグ", value=task.tag or TAG_NONE_LABEL, inline=True)
    embed.add_field(name="色", value=task.color, inline=True)
    embed.add_field(name="所有者", value=f"<@{task.owner_id}>", inline=True)
    embed.add_field(
        name="担当者",
        value=", ".join(f"<@{user_id}>" for user_id in assignees) if assignees else "なし",
        inline=False,
    )
    if include_description:
        embed.add_field(name="内容", value=task.description or "なし", inline=False)
    if reminders:
        embed.add_field(name="リマインド", value=", ".join(_reminder_label(offset) for offset in reminders), inline=False)
    repeat_label = REPEAT_RULE_LABELS.get(task.repeat_rule, REPEAT_NONE_LABEL)
    if task.repeat_rule:
        repeat_label += f" / 終了日: {format_user_date(task.repeat_end_at)}"
    embed.add_field(name="繰り返し", value=repeat_label, inline=False)
    embed.set_footer(text=f"作成: {format_user_datetime(task.created_at)} / 更新: {format_user_datetime(task.updated_at)}")
    return embed


def _reminder_label(offset: int) -> str:
    for label, value in REMINDER_OFFSETS.items():
        if value == offset:
            return label
    return f"{offset}分前"


def get_service(interaction: discord.Interaction) -> TaskService:
    return interaction.client.service


def ensure_guild(interaction: discord.Interaction) -> tuple[int, int]:
    if interaction.guild is None or interaction.channel is None:
        raise UserFacingError("このBotはDiscordサーバー内のチャンネルでのみ利用できます。DMでは利用できません。")
    return interaction.guild.id, interaction.channel.id


def is_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


class ConfirmDeleteView(discord.ui.View):
    def __init__(
        self,
        *,
        service: TaskService,
        requester_id: int,
        guild_id: int,
        task_id: str,
        scope: str,
        emergency: bool = False,
        reason: str | None = None,
    ):
        super().__init__(timeout=120)
        self.service = service
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.task_id = task_id
        self.scope = scope
        self.emergency = emergency
        self.reason = reason

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("この確認ボタンは実行者だけが使えます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            if self.emergency:
                task = self.service.emergency_delete(
                    guild_id=self.guild_id,
                    actor_id=interaction.user.id,
                    task_id=self.task_id,
                    is_admin=is_admin(interaction),
                    reason=self.reason or "",
                )
                await interaction.response.edit_message(
                    content=f"緊急削除しました: `{task.task_id}`",
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                targets = self.service.delete_task(
                    guild_id=self.guild_id,
                    actor_id=interaction.user.id,
                    task_id=self.task_id,
                    scope=self.scope,
                )
                await interaction.response.edit_message(
                    content=f"{len(targets)}件のタスクを削除しました。30日以内は `/タスク 復元` で戻せます。",
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except UserFacingError as exc:
            await respond_error(interaction, str(exc))

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="削除を取り消しました。", embed=None, view=None)


task_group = app_commands.Group(name="タスク", description="ToDoタスクを管理します")
admin_group = app_commands.Group(name="管理", description="管理者向け操作")


@task_group.command(name="追加", description="タスクを追加します")
@app_commands.rename(
    task_type="種別",
    title="タスク名",
    description="内容",
    due_at="期限",
    priority="優先度",
    tag="タグ",
    color="色",
    reminder_1="通知1",
    reminder_2="通知2",
    reminder_3="通知3",
    repeat="繰り返し",
    repeat_end_date="繰り返し終了日",
    assignee_1="担当者1",
    assignee_2="担当者2",
    assignee_3="担当者3",
    assignee_4="担当者4",
)
@app_commands.describe(
    due_at="YYYY-MM-DD HH:MM 形式。例: 2026-06-10 21:00",
    repeat_end_date="YYYY-MM-DD 形式。繰り返しを止める日",
)
@app_commands.choices(
    task_type=TASK_TYPE_CHOICES,
    priority=PRIORITY_CHOICES,
    tag=TAG_CHOICES,
    color=COLOR_CHOICES,
    reminder_1=REMINDER_CHOICES,
    reminder_2=REMINDER_CHOICES,
    reminder_3=REMINDER_CHOICES,
    repeat=REPEAT_CHOICES,
)
async def add_task(
    interaction: discord.Interaction,
    task_type: str,
    title: str,
    description: str | None = None,
    due_at: str | None = None,
    priority: str = "中",
    tag: str = TAG_NONE_LABEL,
    color: str = "デフォルト",
    reminder_1: str = REMINDER_NONE_LABEL,
    reminder_2: str = REMINDER_NONE_LABEL,
    reminder_3: str = REMINDER_NONE_LABEL,
    repeat: str = REPEAT_NONE_LABEL,
    repeat_end_date: str | None = None,
    assignee_1: discord.User | None = None,
    assignee_2: discord.User | None = None,
    assignee_3: discord.User | None = None,
    assignee_4: discord.User | None = None,
) -> None:
    try:
        guild_id, channel_id = ensure_guild(interaction)
        service = get_service(interaction)
        assignees = [user.id for user in [assignee_1, assignee_2, assignee_3, assignee_4] if user]
        task = service.create_task(
            guild_id=guild_id,
            channel_id=channel_id,
            creator_id=interaction.user.id,
            task_type=task_type,
            title=title,
            description=description,
            due_at_text=due_at,
            priority=priority,
            tag=tag,
            color=color,
            reminder_labels=[reminder_1, reminder_2, reminder_3],
            repeat_label=repeat,
            repeat_end_date_text=repeat_end_date,
            assignee_ids=assignees,
        )
        embed = build_task_embed(task, service=service, title="タスクを追加しました")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=task.task_type == TASK_TYPE_PERSONAL,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await warn_notification_failures(interaction, service)
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))
    except Exception:
        logger.exception("タスク追加中にエラーが発生しました")
        await respond_error(interaction, "保存中に予期しないエラーが発生しました。ログを確認してください。")


@task_group.command(name="一覧", description="タスク一覧を表示します")
@app_commands.rename(status_filter="状態", tag_filter="タグ", relation="関係", page="ページ")
@app_commands.choices(status_filter=STATUS_FILTER_CHOICES, tag_filter=TAG_FILTER_CHOICES, relation=RELATION_CHOICES)
async def list_tasks(
    interaction: discord.Interaction,
    status_filter: str = "未完了",
    tag_filter: str = TAG_ALL_LABEL,
    relation: str = "all",
    page: app_commands.Range[int, 1, 100] = 1,
) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        include_deleted = status_filter == "削除済み"
        tasks_ = service.list_tasks(
            guild_id=guild_id,
            user_id=interaction.user.id,
            status_filter=status_filter,
            tag_filter=tag_filter,
            relation_filter=None if relation == "all" else relation,
            include_deleted=include_deleted,
        )
        total_pages = max(1, ceil(len(tasks_) / PAGE_SIZE))
        page = min(page, total_pages)
        start = (page - 1) * PAGE_SIZE
        shown = tasks_[start : start + PAGE_SIZE]
        embed = discord.Embed(title="タスク一覧", color=COLORS["デフォルト"])
        embed.description = (
            f"条件: 状態={status_filter} / タグ={tag_filter} / ページ={page}/{total_pages}\n"
            + ("\n".join(task_line(task) for task in shown) if shown else "表示できるタスクはありません。")
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await warn_notification_failures(interaction, service)
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))
    except Exception:
        logger.exception("タスク一覧中にエラーが発生しました")
        await respond_error(interaction, "一覧取得中に予期しないエラーが発生しました。ログを確認してください。")


@task_group.command(name="詳細", description="タスク詳細を表示します")
@app_commands.rename(task_id="タスクid")
async def task_detail(interaction: discord.Interaction, task_id: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.get_visible_task(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id)
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service),
            ephemeral=task.task_type == TASK_TYPE_PERSONAL,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await warn_notification_failures(interaction, service)
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="編集", description="タスクを編集します")
@app_commands.rename(
    task_id="タスクid",
    title="タスク名",
    description="内容",
    clear_description="内容を削除",
    due_at="期限",
    clear_due="期限を削除",
    priority="優先度",
    tag="タグ",
    color="色",
    status="状態",
    reminder_1="通知1",
    reminder_2="通知2",
    reminder_3="通知3",
    replace_reminders="通知を置換",
    repeat="繰り返し",
    repeat_end_date="繰り返し終了日",
    clear_repeat_end="繰り返し終了日を削除",
    replace_assignees="担当者を置換",
    assignee_1="担当者1",
    assignee_2="担当者2",
    assignee_3="担当者3",
    assignee_4="担当者4",
    assignee_5="担当者5",
)
@app_commands.describe(due_at="YYYY-MM-DD HH:MM 形式。例: 2026-06-10 21:00")
@app_commands.choices(
    priority=PRIORITY_CHOICES,
    tag=TAG_CHOICES,
    color=COLOR_CHOICES,
    status=STATUS_CHOICES,
    reminder_1=REMINDER_CHOICES,
    reminder_2=REMINDER_CHOICES,
    reminder_3=REMINDER_CHOICES,
    repeat=REPEAT_CHOICES,
)
async def edit_task(
    interaction: discord.Interaction,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    clear_description: bool = False,
    due_at: str | None = None,
    clear_due: bool = False,
    priority: str | None = None,
    tag: str | None = None,
    color: str | None = None,
    status: str | None = None,
    reminder_1: str = REMINDER_NONE_LABEL,
    reminder_2: str = REMINDER_NONE_LABEL,
    reminder_3: str = REMINDER_NONE_LABEL,
    replace_reminders: bool = False,
    repeat: str | None = None,
    repeat_end_date: str | None = None,
    clear_repeat_end: bool = False,
    replace_assignees: bool = False,
    assignee_1: discord.User | None = None,
    assignee_2: discord.User | None = None,
    assignee_3: discord.User | None = None,
    assignee_4: discord.User | None = None,
    assignee_5: discord.User | None = None,
) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        assignees = [user.id for user in [assignee_1, assignee_2, assignee_3, assignee_4, assignee_5] if user]
        task = service.edit_task(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            task_id=task_id,
            title=title if title is not None else UNSET,
            description=None if clear_description else (description if description is not None else UNSET),
            due_at_text=None if clear_due else (due_at if due_at is not None else UNSET),
            priority=priority if priority is not None else UNSET,
            tag=tag if tag is not None else UNSET,
            color=color if color is not None else UNSET,
            status=status if status is not None else UNSET,
            reminder_labels=[reminder_1, reminder_2, reminder_3]
            if replace_reminders
            or any(label != REMINDER_NONE_LABEL for label in [reminder_1, reminder_2, reminder_3])
            else UNSET,
            repeat_label=repeat if repeat is not None else UNSET,
            repeat_end_date_text=None if clear_repeat_end else (repeat_end_date if repeat_end_date is not None else UNSET),
            assignee_ids=assignees if replace_assignees else UNSET,
        )
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="タスクを編集しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await warn_notification_failures(interaction, service)
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))
    except Exception:
        logger.exception("タスク編集中にエラーが発生しました")
        await respond_error(interaction, "編集中に予期しないエラーが発生しました。ログを確認してください。")


@task_group.command(name="削除", description="タスクを削除します")
@app_commands.rename(task_id="タスクid", scope="繰り返し範囲")
@app_commands.choices(scope=DELETE_SCOPE_CHOICES)
async def delete_task(
    interaction: discord.Interaction,
    task_id: str,
    scope: str = DELETE_SCOPE_OCCURRENCE,
) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.get_visible_task(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id)
        if task.owner_id != str(interaction.user.id):
            raise PermissionDenied("この操作はタスク所有者だけが実行できます。")
        view = ConfirmDeleteView(
            service=service,
            requester_id=interaction.user.id,
            guild_id=guild_id,
            task_id=task.task_id,
            scope=scope,
        )
        await interaction.response.send_message(
            content=f"次のタスクを削除します。よろしいですか？ 範囲: {DELETE_SCOPE_LABELS.get(scope, 'この回だけ')}",
            embed=build_task_embed(task, service=service),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="復元", description="削除後30日以内のタスクを復元します")
@app_commands.rename(task_id="タスクid")
async def restore_task(interaction: discord.Interaction, task_id: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.restore_task(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id)
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="タスクを復元しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="完了", description="タスクを完了にします")
@app_commands.rename(task_id="タスクid")
async def complete_task(interaction: discord.Interaction, task_id: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.complete_task(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id)
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="タスクを完了しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="完了取消", description="完了したタスクを未完了へ戻します")
@app_commands.rename(task_id="タスクid")
async def uncomplete_task(interaction: discord.Interaction, task_id: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.uncomplete_task(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id)
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="完了を取り消しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="状態変更", description="タスクの状態を変更します")
@app_commands.rename(task_id="タスクid", status="状態")
@app_commands.choices(status=STATUS_CHOICES)
async def change_status(interaction: discord.Interaction, task_id: str, status: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.change_status(guild_id=guild_id, actor_id=interaction.user.id, task_id=task_id, status=status)
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="状態を変更しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@task_group.command(name="ヘルプ", description="使い方を表示します")
async def help_task(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="ToDoリストbot ヘルプ", color=COLORS["デフォルト"])
    embed.description = (
        "`/タスク 追加` でタスクを登録します。期限は `2026-06-10 21:00` の形式です。\n"
        "`/タスク 一覧` で自分が所有または担当するタスクを確認します。\n"
        "`/タスク 詳細` で内容、担当者、通知、繰り返し設定を確認します。\n"
        "`/タスク 編集` は所有者だけが使えます。値を消す場合は削除用の項目を使います。\n"
        "`/タスク 削除` は確認ボタンを押した場合だけ削除します。30日以内なら復元できます。\n"
        "`/タスク 完了`、`/タスク 完了取消`、`/タスク 状態変更` で進捗を更新できます。\n"
        "`/タスク 管理 所有権移譲` と `/タスク 管理 緊急削除` は管理者専用です。"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin_group.command(name="所有権移譲", description="管理者がタスク所有者を変更します")
@app_commands.rename(task_id="タスクid", new_owner="新しい所有者", reason="理由")
async def transfer_owner(
    interaction: discord.Interaction,
    task_id: str,
    new_owner: discord.User,
    reason: str | None = None,
) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.transfer_owner(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            task_id=task_id,
            new_owner_id=new_owner.id,
            is_admin=is_admin(interaction),
            reason=reason,
        )
        await interaction.response.send_message(
            embed=build_task_embed(task, service=service, title="所有権を移譲しました"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


@admin_group.command(name="緊急削除", description="管理者が理由付きでタスクを緊急削除します")
@app_commands.rename(task_id="タスクid", reason="削除理由")
async def emergency_delete(interaction: discord.Interaction, task_id: str, reason: str) -> None:
    try:
        guild_id, _ = ensure_guild(interaction)
        service = get_service(interaction)
        task = service.get_admin_task(guild_id=guild_id, task_id=task_id, is_admin=is_admin(interaction))
        view = ConfirmDeleteView(
            service=service,
            requester_id=interaction.user.id,
            guild_id=guild_id,
            task_id=task.task_id,
            scope=DELETE_SCOPE_SERIES,
            emergency=True,
            reason=reason,
        )
        await interaction.response.send_message(
            content="次のタスクを緊急削除します。監査ログへ理由を保存します。よろしいですか？",
            embed=build_task_embed(task, service=service),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except UserFacingError as exc:
        await respond_error(interaction, str(exc))


task_group.add_command(admin_group)


def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_path)
    try:
        validate_token(settings)
    except RuntimeError as exc:
        print(str(exc))
        return

    database = Database(settings.db_path)
    repository = TaskRepository(database)
    service = TaskService(repository)
    bot = TodoDiscordBot(database, service)
    logger.info("Botを起動します")
    bot.run(settings.token, log_handler=None)
