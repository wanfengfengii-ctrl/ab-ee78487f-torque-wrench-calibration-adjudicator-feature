"""校准档案领域服务：设备健康状态迁移与登记/查询编排。

状态机完全由本模块的 :func:`transition` 承载，路由与契约层不做任何
状态判定：

- 任一后续复核**合格** → ``in_service``（在用），连续不合格计数清零；
- **首次**不合格（含首次登记即不合格）→ ``observation``（观察），
  连续不合格计数为 1；
- **连续两次**不合格 → ``out_of_service``（停用）；停用期间继续不合格
  则保持停用、计数继续累加。

登记时复用 :func:`app.calculator.verify_torque` 的十进制定点精确判定
与 :func:`app.serialization.build_verify_response` 的唯一结果映射，
随后在仓库的单个互斥写事务内原子完成「档案 upsert + 复核快照 insert」。
非法读数在 Pydantic 契约层即被整体拒绝（HTTP 422），根本不会进入本
服务，因此不会写入记录也不会改变既有状态。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.calculator import verify_torque
from app.db import ProfileRow, CalibrationRepository, StoredRecord
from app.models import (
    CalibrationProfileResponse,
    CalibrationRecordResponse,
    CalibrationRegisterResponse,
    TorqueVerifyRequest,
)
from app.serialization import build_verify_response

#: 设备健康状态
STATUS_IN_SERVICE = "in_service"
STATUS_OBSERVATION = "observation"
STATUS_OUT_OF_SERVICE = "out_of_service"

#: 连续两次不合格即停用
OUT_OF_SERVICE_FAIL_THRESHOLD = 2


class WrenchNotFoundError(LookupError):
    """查询未登记的扳手编号：路由层据此返回结构化 404，不泄露其他档案。"""

    def __init__(self, wrench_sn: str) -> None:
        self.wrench_sn = wrench_sn
        super().__init__(f"calibration profile not found: {wrench_sn}")


def transition(
    profile: ProfileRow | None, passed: bool
) -> tuple[str, int]:
    """状态迁移纯函数：给定档案当前行与本次判定，返回新状态与新计数。

    :param profile: 档案当前行；``None`` 表示首次登记（自动建档）。
    :param passed: 本次复核是否合格。
    """
    if passed:
        # 任一后续合格（含首次登记即合格）→ 在用，计数清零。
        return STATUS_IN_SERVICE, 0
    new_count = (profile.consecutive_fail_count if profile else 0) + 1
    if new_count >= OUT_OF_SERVICE_FAIL_THRESHOLD:
        return STATUS_OUT_OF_SERVICE, new_count
    # 首次出现不合格 → 观察。
    return STATUS_OBSERVATION, new_count


def _utc_now_iso() -> str:
    """登记时刻：UTC ISO 8601 字符串（秒级精度、带偏移量）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CalibrationService:
    """校准档案应用服务：编排精确判定、状态迁移与原子持久化。"""

    def __init__(
        self,
        repository: CalibrationRepository,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._repo = repository
        self._clock = clock

    def register(
        self, wrench_sn: str, payload: TorqueVerifyRequest
    ) -> CalibrationRegisterResponse:
        """登记一次复核：复用精确判定，原子写入快照并迁移状态。

        首次登记自动建立档案。
        """
        # 先做纯计算：若判定本身失败（理论上契约层已拦截），绝不开事务。
        result = verify_torque(payload.target_nm, payload.measured_nm)
        snapshot = build_verify_response(payload, result).model_dump()
        passed = result.overall_ok

        with self._repo.registration_transaction() as tx:
            profile = tx.get_profile(wrench_sn)
            new_status, new_fail_count = transition(profile, passed)
            registered_at = self._clock()
            seq = tx.save_record(
                wrench_sn=wrench_sn,
                registered_at=registered_at,
                snapshot=snapshot,
                new_status=new_status,
                new_fail_count=new_fail_count,
            )

        return CalibrationRegisterResponse(
            wrench_sn=wrench_sn,
            status=new_status,
            consecutive_fail_count=new_fail_count,
            seq=seq,
            registered_at=registered_at,
            **snapshot,
        )

    def get_profile(self, wrench_sn: str) -> CalibrationProfileResponse:
        """按编号查询档案；未知编号抛 :class:`WrenchNotFoundError`。"""
        found = self._repo.get_profile_with_history(wrench_sn)
        if found is None:
            raise WrenchNotFoundError(wrench_sn)
        profile, records = found
        return CalibrationProfileResponse(
            wrench_sn=profile.wrench_sn,
            status=profile.status,
            consecutive_fail_count=profile.consecutive_fail_count,
            total_records=len(records),
            history=[_to_record_response(record) for record in records],
        )


def _to_record_response(record: StoredRecord) -> CalibrationRecordResponse:
    return CalibrationRecordResponse(
        seq=record.seq,
        registered_at=record.registered_at,
        **record.snapshot,
    )
