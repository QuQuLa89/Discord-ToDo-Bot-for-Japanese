import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from todo_bot.bot import TodoDiscordBot
from todo_bot.constants import TASK_TYPE_PERSONAL


@pytest.mark.parametrize("failure_at", ["fetch", "send"])
def test_personal_reminder_failure_does_not_skip_other_recipients(failure_at):
    repo = Mock()
    repo.get_assignees.return_value = ["100", "200"]
    creator = SimpleNamespace(send=AsyncMock())
    assignee = SimpleNamespace(send=AsyncMock())
    failure = discord.DiscordException("通知先に接続できません。")
    if failure_at == "fetch":
        get_user = Mock(return_value=None)
        fetch_user = AsyncMock(side_effect=[failure, assignee])
    else:
        creator.send.side_effect = failure
        get_user = Mock(side_effect=[creator, assignee])
        fetch_user = AsyncMock()
    bot = SimpleNamespace(
        service=SimpleNamespace(repo=repo),
        get_user=get_user,
        fetch_user=fetch_user,
    )
    task = SimpleNamespace(
        task_id="T-000001",
        creator_id="100",
        guild_id="1",
        task_type=TASK_TYPE_PERSONAL,
        title="資料作成",
        due_at=None,
    )
    asyncio.run(
        TodoDiscordBot._send_reminder(bot, SimpleNamespace(reminder_id=1), task)
    )
    assignee.send.assert_awaited_once()
    repo.add_notification_failure.assert_called_once()
    assert repo.add_notification_failure.call_args.kwargs["user_id"] == "100"
    assert (
        repo.add_notification_failure.call_args.kwargs["failure_type"]
        == "personal_dm_failed"
    )
    repo.mark_reminder_failed.assert_called_once()
    repo.mark_reminder_sent.assert_called_once()
