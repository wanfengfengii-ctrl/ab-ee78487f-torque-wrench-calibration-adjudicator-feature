"""扭矩复核的十进制定点计算核心。

所有判定均使用 ``decimal.Decimal`` 完成，内部比较**不舍入**；
只有响应展示层（见 ``app.main``）才调用 :func:`round_for_display`
做四舍五入保留两位小数。

稳定性说明：读数与目标值最多两位小数，可证明偏差率商若不等于
边界值 2.00，至少相差 2e-5（极差率对 3.00 同理），而 40 位十进制
精度的舍入误差约在 1e-38 量级，因此临界样本的判定结论在任何
终端上都稳定一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

READINGS_COUNT = 5

#: 带符号偏差率绝对值上限（%），等于边界仍合格
DEVIATION_LIMIT_PCT = Decimal("2.00")
#: 极差率上限（%），等于边界仍合格
RANGE_LIMIT_PCT = Decimal("3.00")

#: 内部计算精度（十进制有效位数），远高于边界判定所需
CALC_PRECISION = 40

_DISPLAY_QUANTUM = Decimal("0.01")


def round_for_display(value: Decimal) -> Decimal:
    """仅用于响应展示：四舍五入保留两位小数（ties 远离零）。"""
    return value.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TorqueVerification:
    """一次复核的完整结果（内部字段均为未舍入的精确值）。"""

    target_nm: Decimal
    measured_nm: tuple[Decimal, ...]
    mean_nm: Decimal
    deviation_pct: Decimal
    range_pct: Decimal
    deviation_ok: bool
    range_ok: bool

    @property
    def overall_ok(self) -> bool:
        return self.deviation_ok and self.range_ok

    @property
    def failure_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.deviation_ok:
            reasons.append("deviation_pct_out_of_limit")
        if not self.range_ok:
            reasons.append("range_pct_out_of_limit")
        return reasons


def verify_torque(
    target_nm: Decimal,
    measured_nm: list[Decimal] | tuple[Decimal, ...],
) -> TorqueVerification:
    """复核五次扭矩读数。

    :param target_nm: 目标扭矩（N·m），必须为正数。
    :param measured_nm: 恰好五次读数（N·m）。
    :raises ValueError: 读数个数不为 5 或目标值非正。
    """
    readings = tuple(measured_nm)
    if len(readings) != READINGS_COUNT:
        raise ValueError(
            f"expected exactly {READINGS_COUNT} readings, got {len(readings)}"
        )
    if target_nm <= 0:
        raise ValueError("target_nm must be positive")

    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        total = sum(readings, Decimal(0))
        mean_nm = total / Decimal(READINGS_COUNT)
        deviation_pct = (mean_nm - target_nm) / target_nm * Decimal(100)
        range_pct = (max(readings) - min(readings)) / target_nm * Decimal(100)

    # 内部比较直接使用未舍入的精确值；等于边界仍合格。
    deviation_ok = abs(deviation_pct) <= DEVIATION_LIMIT_PCT
    range_ok = range_pct <= RANGE_LIMIT_PCT

    return TorqueVerification(
        target_nm=target_nm,
        measured_nm=readings,
        mean_nm=mean_nm,
        deviation_pct=deviation_pct,
        range_pct=range_pct,
        deviation_ok=deviation_ok,
        range_ok=range_ok,
    )
