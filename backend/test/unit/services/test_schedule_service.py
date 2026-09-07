"""定时任务定义服务单元测试：Cron/时区校验与错误码映射。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from yuxi.services.schedule_service import (
    schedule_thread_id,
    validate_cron_and_timezone,
)


def _detail_code(exc_info) -> int:
    return exc_info.value.detail["code"]


def test_valid_cron_and_timezone_pass():
    validate_cron_and_timezone("0 9 * * *", "Asia/Shanghai")
    validate_cron_and_timezone("*/5 * * * *", "UTC")


def test_six_segment_cron_rejected_as_20312():
    with pytest.raises(HTTPException) as exc_info:
        validate_cron_and_timezone("0 9 * * * *", "Asia/Shanghai")
    assert _detail_code(exc_info) == 20312


def test_invalid_cron_value_rejected_as_20312():
    with pytest.raises(HTTPException) as exc_info:
        validate_cron_and_timezone("99 * * * *", "Asia/Shanghai")
    assert _detail_code(exc_info) == 20312


def test_fixed_offset_timezone_rejected_as_20313():
    with pytest.raises(HTTPException) as exc_info:
        validate_cron_and_timezone("0 9 * * *", "+08:00")
    assert _detail_code(exc_info) == 20313


def test_missing_fields_rejected_as_param_error():
    with pytest.raises(HTTPException) as exc_info:
        validate_cron_and_timezone("", "Asia/Shanghai")
    assert _detail_code(exc_info) == 40001
    with pytest.raises(HTTPException) as exc_info:
        validate_cron_and_timezone("0 9 * * *", "")
    assert _detail_code(exc_info) == 40001


def test_schedule_thread_id_convention():
    assert schedule_thread_id(12) == "schedule:12"
