"""FastAPI 应用入口：扭矩扳手复核 HTTP API。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.calibration import CalibrationService, WrenchNotFoundError
from app.calculator import verify_torque
from app.db import CalibrationRepository
from app.models import (
    CalibrationProfileResponse,
    CalibrationRegisterResponse,
    TorqueVerifyBatchRequest,
    TorqueVerifyBatchResponse,
    TorqueVerifyRequest,
    TorqueVerifyResponse,
    WrenchId,
)
from app.parsing import install_exact_decimal_request_class
from app.serialization import build_verify_response

# 让请求体中的 JSON 数值按原始文本精确解析为 Decimal（不经 float64），
# 必须在应用开始接受请求前安装，且位于 FastAPI() 实例化前后均可——
# FastAPI 在每次请求时才从 fastapi.routing 模块全局名查找 Request 类。
install_exact_decimal_request_class()

#: 应用内 SQLite 档案库路径（可用 CALIBRATION_DB_PATH 覆盖）
DEFAULT_DB_PATH = os.environ.get("CALIBRATION_DB_PATH", "data/calibration.db")


def create_calibration_service(db_path: str | None = None) -> CalibrationService:
    """构造校准档案服务（仓库自行建库建表），供路由与测试替换。"""
    return CalibrationService(CalibrationRepository(db_path or DEFAULT_DB_PATH))


app = FastAPI(
    title="Torque Wrench Verification API",
    version="1.1.0",
    description=(
        "装配线扭矩扳手复核：十进制定点计算，内部比较不舍入，"
        "响应展示四舍五入保留两位小数；校准档案按扳手编号纵向闭环，"
        "由连续结果自动给出在用/观察/停用状态。"
    ),
)

#: 默认档案服务（懒加载单例）；测试可直接替换或用依赖覆盖
app.state.calibration_service = None


def get_calibration_service() -> CalibrationService:
    """按请求提供档案服务：首次使用时才建库建表，避免导入期写文件系统。"""
    if app.state.calibration_service is None:
        app.state.calibration_service = create_calibration_service()
    return app.state.calibration_service


def _sanitize_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """提取 JSON 安全的错误字段（loc/type/msg）。"""
    return [
        {
            "loc": list(err.get("loc", ())),
            "type": err.get("type"),
            "msg": err.get("msg"),
        }
        for err in errors
    ]


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """任一数值非法即整体拒绝，返回结构化错误。"""
    return JSONResponse(
        status_code=422,  # 422 Unprocessable Content
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    "请求未通过校验，已整体拒绝：target_nm 与 measured_nm 的"
                    "所有数值须在 1.00–500.00 N·m 之间且最多两位小数，"
                    "measured_nm 必须恰好包含 5 个读数；"
                    "批量复核的 items 须包含 1–20 个测点；"
                    "wrench_sn 路径参数须为非空白且不超过 64 字符的字符串。"
                ),
                "details": _sanitize_errors(exc.errors()),
            }
        },
    )


@app.exception_handler(WrenchNotFoundError)
async def wrench_not_found_handler(
    request: Request, exc: WrenchNotFoundError
) -> JSONResponse:
    """未知扳手编号：按现有结构化错误外形返回 404，不泄露任何其他档案。"""
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "WRENCH_NOT_FOUND",
                "message": "未找到该扭矩扳手编号的校准档案，请先完成一次登记。",
                "details": [
                    {
                        "loc": ["path", "wrench_sn"],
                        "type": "not_found",
                        "msg": "calibration profile not found",
                    }
                ],
            }
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _verify_one(payload: TorqueVerifyRequest) -> TorqueVerifyResponse:
    """复用十进制定点计算完成单次复核，并映射为响应模型。"""
    result = verify_torque(payload.target_nm, payload.measured_nm)
    return build_verify_response(payload, result)


@app.post(
    "/api/v1/torque/verify",
    response_model=TorqueVerifyResponse,
    summary="复核五次扭矩读数",
)
def verify_endpoint(payload: TorqueVerifyRequest) -> TorqueVerifyResponse:
    return _verify_one(payload)


@app.post(
    "/api/v1/torque/verify/batch",
    response_model=TorqueVerifyBatchResponse,
    summary="批量复核：一次提交一至二十个测点",
)
def verify_batch_endpoint(
    payload: TorqueVerifyBatchRequest,
) -> TorqueVerifyBatchResponse:
    # 请求体数值已由 ExactDecimalRequest 精确解析；任一测点非法会在
    # 模型层整体拒绝。此处逐项复用单次路径：计算全部测点，不因首个
    # 不合格提前终止。
    items: list[TorqueVerifyResponse] = []
    failed_indices: list[int] = []
    for index, item in enumerate(payload.items):
        response = _verify_one(item)
        items.append(response)
        if response.overall == "fail":
            failed_indices.append(index)

    return TorqueVerifyBatchResponse(
        items=items,
        total=len(items),
        passed_count=len(items) - len(failed_indices),
        failed_count=len(failed_indices),
        failed_indices=failed_indices,
        overall="pass" if not failed_indices else "fail",
    )


# ---------------------------------------------------------------------------
# 校准档案纵向闭环：一个登记入口 + 一个按扳手编号查询入口
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/wrenches/{wrench_sn}/calibrations",
    response_model=CalibrationRegisterResponse,
    summary="登记一次复核并自动建立/更新校准档案",
)
def register_calibration(
    wrench_sn: WrenchId,
    payload: TorqueVerifyRequest,
    service: CalibrationService = Depends(get_calibration_service),
) -> CalibrationRegisterResponse:
    """首次登记自动建档；请求体与单次复核接口完全一致（精确解析+判定复用）。"""
    # 纯编排：解析与校验由契约层完成，判定与状态迁移由领域服务完成。
    return service.register(wrench_sn, payload)


@app.get(
    "/api/v1/wrenches/{wrench_sn}/calibrations",
    response_model=CalibrationProfileResponse,
    summary="按扳手编号查询校准健康档案",
)
def get_calibration_profile(
    wrench_sn: WrenchId,
    service: CalibrationService = Depends(get_calibration_service),
) -> CalibrationProfileResponse:
    """返回当前状态、连续不合格次数与按登记顺序排列的完整判定历史。"""
    return service.get_profile(wrench_sn)
