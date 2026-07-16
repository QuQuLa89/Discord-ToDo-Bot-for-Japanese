# Discord-ToDo-Bot-for-Japanese

日本語向け自己ホスト型の Discord ToDo Bot（Python 3.10 / discord.py 2.3 / SQLite）。

## コマンド

- 起動: `python main.py`
- テスト: `pytest tests/ -q`
- 依存: `pip install -r requirements.txt`
- Docker起動: `docker compose up -d --build`（`.env` が必要。データは `data/`・`logs/`・`backups/` にバインドマウント）

## 構成

- `main.py` … エントリーポイント
- `todo_bot/` … `bot.py`（Discord 層）/ `services.py`（ビジネスロジック）/ `repository.py`・`db.py`（SQLite）/ `config.py`（.env 読み込み）
- `tests/test_services.py` … サービス層のユニットテスト
- `data/`（DB）と `backups/`（起動時バックアップ7世代）はコミットしない

## 注意

- Bot トークンは `.env` の `DISCORD_BOT_TOKEN`。読まない・出力しない・コミットしない。
