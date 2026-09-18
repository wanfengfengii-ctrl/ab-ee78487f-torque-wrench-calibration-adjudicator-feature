"""校准档案领域服务：设备健康状态迁移与登记/查询/作废编排。

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

作废误登记不删除快照（保留审计证据）：在仓库的单个互斥写事务内原子
完成「记录标记 + 原因/作废时间 + 档案状态回写」，其中档案状态由
:func:`replay_profile` 按登记顺序重放该档案中**仍有效**的判定重新
计算——作废末次不合格可恢复状态，作废中间记录同样按剩余历史重算。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from app.calculator import verify_torque
from app.db import ProfileRow, CalibrationRepository, StoredRecord
from app.models import (
    CalibrationProfileResponse,
    CalibrationRecordResponse,
    CalibrationRegisterResponse,
    CalibrationVoidResponse,
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


class CalibrationRecordNotFoundError(LookupError):
    """作废目标不存在（扳手未建档或序号不在档案中）。

    路由层据此返回结构化 404；错误外形与未知扳手一致，不泄露其他档案。
    """

    def __init__(self, wrench_sn: str, seq: int) -> None:
        self.wrench_sn = wrench_sn
        self.seq = seq
        super().__init__(f"calibration record not found: {wrench_sn}#{seq}")


class RecordAlreadyVoidedError(RuntimeError):
    """重复作废同一记录：路由层据此返回 409，档案保持不变。"""

    def __init__(self, wrench_sn: str, seq: int) -> None:
        self.wrench_sn = wrench_sn
        self.seq = seq
        super().__init__(f"calibration record already voided: {wrench_sn}#{seq}")


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


def replay_profile(records: Iterable[StoredRecord]) -> tuple[str, int]:
    """按登记顺序重放仍有效的判定，重算 (状态, 连续不合格次数)。

    作废后档案的当前状态不再依赖作废前的累计值，而是由剩余有效历史
    从头重放得出，因此作废末次不合格可恢复状态、作废中间记录也按
    剩余历史重算。空序列（全部作废）回到初始态：在用、计数 0——
    没有任何有效判定时，档案不构成任何不合格证据。
    """
    profile: ProfileRow | None = None
    status, count = STATUS_IN_SERVICE, 0
    for record in records:
        passed = record.snapshot["overall"] == "pass"
        status, count = transition(profile, passed)
        profile = ProfileRow(
            wrench_sn="",
            status=status,
            consecutive_fail_count=count,
            created_at="",
        )
    return status, count


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

    def void_record(
        self, wrench_sn: str, seq: int, reason: str
    ) -> CalibrationVoidResponse:
        """作废一条误登记：标记记录、保存原因与作废时间，并按仍有效的
        历史重放重算档案状态；全部在仓库的同一互斥写事务内原子完成。

        序号不存在抛 :class:`CalibrationRecordNotFoundError`（事务内
        尚未写入，直接回滚）；记录已作废抛
        :class:`RecordAlreadyVoidedError`，档案保持不变。
        """
        with self._repo.void_transaction() as tx:
            record = tx.get_record(wrench_sn, seq)
            if record is None:
                raise CalibrationRecordNotFoundError(wrench_sn, seq)
            if not record.is_valid:
                raise RecordAlreadyVoidedError(wrench_sn, seq)
            voided_at = self._clock()
            tx.mark_voided(wrench_sn, seq, reason=reason, voided_at=voided_at)
            remaining = tx.list_valid_records(wrench_sn)
            new_status, new_fail_count = replay_profile(remaining)
            tx.update_profile(wrench_sn, new_status, new_fail_count)
            total_records = tx.count_records(wrench_sn)

        return CalibrationVoidResponse(
            wrench_sn=wrench_sn,
            voided_seq=seq,
            void_reason=reason,
            voided_at=voided_at,
            status=new_status,
            consecutive_fail_count=new_fail_count,
            total_records=total_records,
            valid_records=len(remaining),
        )


def _to_record_response(record: StoredRecord) -> CalibrationRecordResponse:
    return CalibrationRecordResponse(
        seq=record.seq,
        registered_at=record.registered_at,
        is_valid=record.is_valid,
        void_reason=record.void_reason,
        voided_at=record.voided_at,
        **record.snapshot,
    )
