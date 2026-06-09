# ToDoリストbot

Discordサーバー内で個人タスクと共有タスクを管理する、日本語向けの自己ホスト型ToDo Botです。

## 対応環境

- OS: Windows 10 / 11
- Python: 3.10
- Discordライブラリ: discord.py 2.3.2
- データ保存: SQLite

## 主な機能

- `/タスク 追加`、`編集`、`削除`、`復元`、`完了`、`完了取消`、`状態変更`
- `/タスク 一覧`、`詳細`、`ヘルプ`
- 個人タスクと共有タスク
- 所有者、作成者、担当者による権限判定
- 最大5人の担当者
- 期限、最大3件のリマインド、優先度、色、固定タグ
- 毎日、毎週、毎月の繰り返しタスク
- Bot再起動後の未送信リマインド処理
- 削除後30日以内の復元
- 管理者による所有権移譲と緊急削除
- SQLite起動時バックアップ7世代保持

## セットアップ

1. Python 3.10をインストールします。
2. このフォルダで仮想環境を作成します。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. 依存パッケージをインストールします。

```powershell
pip install -r requirements.txt
```

4. `.env.example` を参考に `.env` を作成します。

```env
DISCORD_BOT_TOKEN=取得したBot token
TODO_BOT_DB_PATH=data/todo_bot.sqlite3
TODO_BOT_LOG_PATH=logs/todo_bot.log
```

重要: `.env` とBot tokenはGitHub、SNS、他人、スクリーンショットへ共有しないでください。漏えいした場合はDiscord Developer Portalでtokenを再発行してください。

5. Botを起動します。

```powershell
python main.py
```

## Discord Developer Portalでの作成手順

1. [Discord Developer Portal](https://discord.com/developers/applications) を開きます。
2. `New Application` でApplicationを作成します。
3. 左メニューの `Bot` でBot Userを作成します。
4. `Reset Token` または `View Token` でtokenを取得し、`.env` の `DISCORD_BOT_TOKEN` に設定します。
5. 左メニューの `OAuth2`、`URL Generator` を開きます。
6. Scopesで `bot` と `applications.commands` を選びます。
7. Bot PermissionsはAdministratorを選ばず、必要最小限にします。

必要権限:

- View Channels
- Send Messages
- Embed Links
- Read Message History

生成されたURLで、利用するDiscordサーバーへBotを招待します。

## コマンド

- `/タスク 追加`: タスクを登録します。期限は `YYYY-MM-DD HH:MM` 形式です。
- `/タスク 一覧`: 自分が所有または担当するタスクを5件単位で表示します。
- `/タスク 詳細`: タスクIDで詳細を表示します。
- `/タスク 編集`: 所有者がタスク項目を変更します。
- `/タスク 削除`: 確認ボタンを押した場合だけ論理削除します。
- `/タスク 復元`: 削除後30日以内のタスクを復元します。
- `/タスク 完了`: 作成者、所有者、担当者がタスクを完了します。
- `/タスク 完了取消`: 完了前の状態へ戻します。
- `/タスク 状態変更`: 未着手、進行中、保留、完了、中止へ変更します。
- `/タスク 管理 所有権移譲`: Discord管理者が所有者を変更します。
- `/タスク 管理 緊急削除`: Discord管理者が理由付きで削除します。

個人タスクの登録結果、一覧、通知失敗警告は公開チャンネルへ表示しません。

## exe化

開発環境で動作確認したあと、次のコマンドで単体exeを作成できます。

```powershell
pyinstaller --onefile --name todo-bot main.py
```

作成された `dist\todo-bot.exe` を起動するとBotが動きます。`.env` はexeを起動するフォルダに置いてください。

## テスト

Discordへ接続せず、保存・権限・繰り返しなどの中核処理を確認できます。

```powershell
python -m unittest discover -s tests -v
```

## データとログ

- SQLite DB: 既定では `data/todo_bot.sqlite3`
- ログ: 既定では `logs/todo_bot.log`
- バックアップ: `backups/` に直近7世代

ログにはtokenやタスク本文を出さない設計にしています。

# 開発プロセス

要件定義の原文は[こちら](https://app.notion.com/p/ToDo-bot-37a0b377c720814a89b6f6c22dcfc5e3?source=copy_link)

## NotionとChatGPT/Codexを使用した要件定義
- フォーマットをCodexに整えさせ、作成されたNotionページに要件定義を手動入力
- 要件定義内容をChatGPTに「要件定義として最適か」検討させる
- 検討内容を質問形式で表示させ、人間が最終決定権を持てるようにする
- 質問の回答内容を元にChatGPTでNotionの要件定義を修正させる
- 最終チェック

設計はDiscord UIに依存するため省略

## NotionとCodexを使用した開発
- Notionで作成した上記の要件定義をCodexに読み込ませコード作成
- 内容を人間が確認

## Codexを使用したテスト
- テスト内容をChatGPTに生成
- 生成内容に従って実際にテスト