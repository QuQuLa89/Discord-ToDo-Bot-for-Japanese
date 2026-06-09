from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    token: str
    db_path: Path
    log_path: Path


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    db_path = Path(os.getenv("TODO_BOT_DB_PATH", "data/todo_bot.sqlite3"))
    log_path = Path(os.getenv("TODO_BOT_LOG_PATH", "logs/todo_bot.log"))
    return Settings(token=token, db_path=db_path, log_path=log_path)


def validate_token(settings: Settings) -> None:
    if not settings.token or settings.token == "ここにBotトークンを入力してください":
        raise RuntimeError(
            ".env に DISCORD_BOT_TOKEN が設定されていません。"
            "READMEの手順に従ってDiscord Developer PortalからBot tokenを取得し、.envへ保存してください。"
        )
