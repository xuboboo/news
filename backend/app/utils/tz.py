"""北京时间（UTC+8）口径：对用户展示的「自然日」一律按北京时间切分。

日报最初按 UTC 自然日取窗口：用户 9 月 11 日早上看到的最新一期标着
「9 月 10 日」，且窗口横跨北京 9/10 早 8 点 → 9/11 早 8 点，与直觉不符
（2026-09-11 改版）。现统一为：期刊标注发布日（北京时间当天），
统计窗口 = 前一个北京时间自然日全天。
"""
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

TZ_BJ = timezone(timedelta(hours=8))


def bj_now() -> datetime:
    """当前北京时间。"""
    return datetime.now(TZ_BJ)


def bj_day_start(day: date_type) -> datetime:
    """北京时间 day 的 00:00（带时区，可直接与 UTC 时间戳比较）。"""
    return datetime(day.year, day.month, day.day, tzinfo=TZ_BJ)
