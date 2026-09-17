"""请求与响应模型：所有数值以十进制定点（Decimal）承载。"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr

MIN_NM = Decimal("1.00")
MAX_NM = Decimal("500.00")

#: 扳手编号最大长度（strip 后）
WRENCH_ID_MAX_LEN = 64


def _at_most_two_decimal_places(value: Decimal) -> Decimal:
    """校验数值为有限值且最多两位小数，否则整体拒绝。"""
    if not value.is_finite():
        raise ValueError("value must be a finite number")
    if value.as_tuple().exponent < -2:
        raise ValueError("value must have at most 2 decimal places")
    return value


#: 合法的扭矩数值：1.00–500.00 N·m、有限、最多两位小数
TorqueValue = Annotated[
    Decimal,
    Field(ge=MIN_NM, le=MAX_NM),
    AfterValidator(_at_most_two_decimal_places),
]


class TorqueVerifyRequest(BaseModel):
    """复核请求：目标扭矩 + 恰好五次读数。"""

    model_config = ConfigDict(extra="forbid")

    target_nm: TorqueValue
    measured_nm: Annotated[list[TorqueValue], Field(min_length=5, max_length=5)]


#: 批量复核的测点数上下限
BATCH_MIN_ITEMS = 1
BATCH_MAX_ITEMS = 20


class TorqueVerifyBatchRequest(BaseModel):
    """批量复核请求：一至二十个测点，按输入顺序逐项复核。"""

    model_config = ConfigDict(extra="forbid")

    items: Annotated[
        list[TorqueVerifyRequest],
        Field(min_length=BATCH_MIN_ITEMS, max_length=BATCH_MAX_ITEMS),
    ]


class TorqueVerifyResponse(BaseModel):
    """复核响应：数值字段均为定点字符串，展示值已四舍五入到两位小数。

    注意：`mean_nm`、`deviation_pct`、`range_pct` 是**展示值**；
    判定（`*_ok`）使用的是未舍入的精确值，因此可能出现
    `deviation_pct` 显示 "2.00" 而 `deviation_ok` 为 false 的临界情形。
    """

    target_nm: str
    measured_nm: list[str]
    mean_nm: str
    deviation_pct: str
    deviation_limit_pct: str
    deviation_ok: bool
    range_pct: str
    range_limit_pct: str
    range_ok: bool
    overall: Literal["pass", "fail"]
    failure_reasons: list[str]


class TorqueVerifyBatchResponse(BaseModel):
    """批量复核响应：逐项完整结果（与单次接口字段和值完全一致）+ 整批汇总。

    `failed_indices` 为 0 起始的输入序号，与校验错误 `loc` 中的
    数组下标约定一致；`overall` 仅在全部测点合格时为 `"pass"`。
    """

    items: list[TorqueVerifyResponse]
    total: int
    passed_count: int
    failed_count: int
    failed_indices: list[int]
    overall: Literal["pass", "fail"]


#: 校准档案的三种设备状态
WrenchStatus = Literal["in_service", "observation", "out_of_service"]


def _wrench_id_validator(value: str) -> str:
    """校验扳手编号：非空白、去掉首尾空白后长度 1–64。"""
    value = value.strip()
    if not value:
        raise ValueError("wrench_sn must be a non-empty string")
    if len(value) > WRENCH_ID_MAX_LEN:
        raise ValueError(
            f"wrench_sn must be at most {WRENCH_ID_MAX_LEN} characters"
        )
    return value


#: 合法扭矩扳手编号：必须为字符串，strip 后非空且不超过 64 字符
WrenchId = Annotated[StrictStr, AfterValidator(_wrench_id_validator)]


class CalibrationRecordResponse(TorqueVerifyResponse):
    """档案中的一条复核快照：登记序号、登记时刻 + 完整单次判定字段。

    继承 :class:`TorqueVerifyResponse`，故快照字段与单次/批量接口
    完全一致（含 ``overall`` 与 ``failure_reasons``）。
    """

    seq: int
    registered_at: str


class CalibrationRegisterResponse(CalibrationRecordResponse):
    """登记响应：本次复核快照 + 迁移后的当前状态与连续不合格次数。"""

    wrench_sn: str
    status: WrenchStatus
    consecutive_fail_count: int


class CalibrationProfileResponse(BaseModel):
    """按扳手编号查询的档案视图：当前状态 + 按登记顺序排列的完整历史。"""

    wrench_sn: str
    status: WrenchStatus
    consecutive_fail_count: int
    total_records: int
    history: list[CalibrationRecordResponse]
