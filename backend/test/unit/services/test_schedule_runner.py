"""ScheduleRunner 单元测试：Cron+IANA 时区的下次触发时刻计算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from yuxi.services.schedule_runner import next_fire_time


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_daily_cron_crosses_timezone():
    """Asia/Shanghai 每天 09:00 触发，对应 UTC 01:00。"""
    after = _utc(2026, 8, 20, 0, 0)
    fired = next_fire_time("0 9 * * *", "Asia/Shanghai", after)
    assert fired == _utc(2026, 8, 20, 1, 0)


def test_daily_cron_already_fired_today_takes_tomorrow():
    after = _utc(2026, 8, 20, 2, 0)  # UTC 02:00 已过上海 09:00
    fired = next_fire_time("0 9 * * *", "Asia/Shanghai", after)
    assert fired == _utc(2026, 8, 21, 1, 0)


def test_interval_cron_in_utc():
    after = _utc(2026, 8, 20, 0, 0)
    fired = next_fire_time("*/15 * * * *", "UTC", after)
    assert fired == _utc(2026, 8, 20, 0, 15)


def test_daylight_saving_shift():
    """Europe/Berlin 3 月底进入夏令时，UTC 触发时刻相应前移一小时。"""
    before_dst = next_fire_time("0 9 * * *", "Europe/Berlin", _utc(2026, 3, 28, 0, 0))
    after_dst = next_fire_time("0 9 * * *", "Europe/Berlin", _utc(2026, 3, 30, 0, 0))
    assert before_dst == _utc(2026, 3, 28, 8, 0)  # 冬令时 UTC+1
    assert after_dst == _utc(2026, 3, 30, 7, 0)  # 夏令时 UTC+2
    assert after_dst - before_dst == timedelta(days=2, hours=-1)
