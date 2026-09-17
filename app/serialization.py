"""复核结果 → 响应模型的唯一映射路径（单次、批量与校准档案共用）。

校准档案登记时持久化的“复核快照”也由此构造，保证档案中的判定字段
与单次、批量接口逐字段一致，不存在第二套映射导致的分歧。
"""

from __future__ import annotations

from decimal import Decimal

from app.calculator import (
    DEVIATION_LIMIT_PCT,
    RANGE_LIMIT_PCT,
    TorqueVerification,
    round_for_display,
)
from app.models import TorqueVerifyRequest, TorqueVerifyResponse

_DISPLAY_QUANTUM = Decimal("0.01")


def fmt_display(value: Decimal) -> str:
    """展示值：四舍五入保留两位小数的定点字符串。"""
    return format(round_for_display(value), "f")


def fmt_reading(value: Decimal) -> str:
    """原始读数回显：规范化为两位小数的定点字符串（数值不变）。"""
    return format(value.quantize(_DISPLAY_QUANTUM), "f")


def build_verify_response(
    payload: TorqueVerifyRequest, result: TorqueVerification
) -> TorqueVerifyResponse:
    """十进制定点计算结果 → 响应模型（单次、批量、档案快照的唯一映射）。"""
    return TorqueVerifyResponse(
        target_nm=fmt_reading(payload.target_nm),
        measured_nm=[fmt_reading(v) for v in payload.measured_nm],
        mean_nm=fmt_display(result.mean_nm),
        deviation_pct=fmt_display(result.deviation_pct),
        deviation_limit_pct=format(DEVIATION_LIMIT_PCT, "f"),
        deviation_ok=result.deviation_ok,
        range_pct=fmt_display(result.range_pct),
        range_limit_pct=format(RANGE_LIMIT_PCT, "f"),
        range_ok=result.range_ok,
        overall="pass" if result.overall_ok else "fail",
        failure_reasons=result.failure_reasons,
    )
