from __future__ import annotations

import calendar
from datetime import datetime, time, timedelta

from .constants import JST, REPEAT_DAILY, REPEAT_MONTHLY, REPEAT_WEEKLY, UTC

USER_DATETIME_FORMAT = "%Y-%m-%d %H:%M"
USER_DATE_FORMAT = "%Y-%m-%d"


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(UTC)


def to_jst(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(JST)


def parse_user_datetime(
    value: str, *, allow_past: bool, now: datetime | None = None
) -> datetime:
    text = value.strip()
    try:
        local = datetime.strptime(text, USER_DATETIME_FORMAT).replace(tzinfo=JST)
    except ValueError as exc:
        raise ValueError(
            "日時は `YYYY-MM-DD HH:MM` の形式で入力してください。例: `2026-06-10 21:00`"
        ) from exc

    parsed = local.astimezone(UTC)
    current = now or now_utc()
    if not allow_past and parsed <= current:
        raise ValueError("過去の日時は指定できません。未来の日時を入力してください。")
    return parsed


def parse_user_date_end(value: str) -> datetime:
    text = value.strip()
    try:
        parsed_date = datetime.strptime(text, USER_DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(
            "終了日は `YYYY-MM-DD` の形式で入力してください。例: `2026-06-30`"
        ) from exc
    local_end = datetime.combine(parsed_date, time(23, 59, 59), tzinfo=JST)
    return local_end.astimezone(UTC)


def format_user_datetime(value: datetime | None) -> str:
    if value is None:
        return "なし"
    return to_jst(value).strftime(USER_DATETIME_FORMAT)


def format_user_date(value: datetime | None) -> str:
    if value is None:
        return "なし"
    return to_jst(value).strftime(USER_DATE_FORMAT)


def add_repeat_period(
    value: datetime, rule: str, anchor_day: int | None = None
) -> datetime:
    local = value.astimezone(JST)
    if rule == REPEAT_DAILY:
        return (local + timedelta(days=1)).astimezone(UTC)
    if rule == REPEAT_WEEKLY:
        return (local + timedelta(weeks=1)).astimezone(UTC)
    if rule == REPEAT_MONTHLY:
        year = local.year
        month = local.month + 1
        if month > 12:
            year += 1
            month = 1
        wanted_day = anchor_day or local.day
        last_day = calendar.monthrange(year, month)[1]
        next_day = min(wanted_day, last_day)
        next_local = local.replace(year=year, month=month, day=next_day)
        return next_local.astimezone(UTC)
    raise ValueError(f"不明な繰り返し周期です: {rule}")


def latest_repeat_due_after(
    base_due_at: datetime,
    rule: str,
    *,
    now: datetime,
    repeat_end_at: datetime | None,
    anchor_day: int | None,
) -> tuple[datetime | None, int]:
    skipped = 0
    candidate = add_repeat_period(base_due_at, rule, anchor_day)
    latest: datetime | None = None
    while candidate <= now and (repeat_end_at is None or candidate <= repeat_end_at):
        if latest is not None:
            skipped += 1
        latest = candidate
        candidate = add_repeat_period(candidate, rule, anchor_day)
    return latest, skipped
