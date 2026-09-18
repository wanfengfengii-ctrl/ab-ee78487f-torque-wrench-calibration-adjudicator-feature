"""一次性验收服务：对运行中的 API 执行黑盒验收并输出证据。

覆盖验收要求：
1. 同一临界输入重复请求，必须稳定获得逐字节相同的证据；
2. 非法样本只得到结构化错误（HTTP 422 + VALIDATION_ERROR）；
3. 双项超限必须同时暴露两个原因；
4. 等于边界（2.00% / 3.00%）仍合格；
5. 临界样本（2.004%）展示为 2.00% 但判不合格；
6. 批量复核：全合格批次、混合批次（失败序号与汇总）、临界值逐项
   与单次接口等价、非法测点整体拒绝且不产生部分结果；
7. 校准档案纵向闭环：首次合格建档、两次连续不合格
   （在用→观察→停用）、合格后计数清零并恢复在用、非法读数不写入
   记录也不改变既有状态、未知编号结构化 404 且不泄露其他档案；
8. 误登记作废：作废末次不合格后状态恢复、作废中间记录按剩余有效
   历史重算、重复作废返回冲突且无副作用、序号/档案未找到返回不泄露
   档案的结构化 404、非法原因写入前整体拒绝、历史保留作废证据；
   设置 RESTART_DB_PATH（与 API 同一 SQLite 文件）时，额外用全新
   服务实例确认“重启”后作废证据与重算结果一致。

通过环境变量 API_BASE_URL 指向被验 API（默认 http://localhost:8000）。
全部通过退出码为 0，否则为 1。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
URL = f"{BASE_URL}/api/v1/torque/verify"
BATCH_URL = f"{BASE_URL}/api/v1/torque/verify/batch"
#: 与被验 API 共享的 SQLite 文件路径（docker compose 下挂同一命名卷）；
#: 设置后用全新仓库实例模拟服务重启，核对作废证据与重算结果。
RESTART_DB_PATH = os.environ.get("RESTART_DB_PATH")


def cal_url(wrench_sn: str) -> str:
    return f"{BASE_URL}/api/v1/wrenches/{wrench_sn}/calibrations"


def void_url(wrench_sn: str, seq: int) -> str:
    return f"{cal_url(wrench_sn)}/{seq}"

_failures: list[str] = []


def check(name: str, ok: bool, evidence: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {evidence}" if evidence else ""), flush=True)
    if not ok:
        _failures.append(name)


def wait_for_api(timeout_s: int = 90) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def main() -> int:
    print(f"验收目标: {BASE_URL}", flush=True)
    if not wait_for_api():
        print("[FAIL] API 健康检查超时", flush=True)
        return 1
    print("[PASS] API 健康检查", flush=True)

    # 1. 临界样本：偏差率 2.004%（展示 2.00%），重复请求证据必须一致
    critical = {
        "target_nm": 100.00,
        "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02],
    }
    responses = [httpx.post(URL, json=critical, timeout=5.0) for _ in range(5)]
    bodies = [r.text for r in responses]
    check(
        "临界样本五次请求响应逐字节一致",
        len(set(bodies)) == 1 and responses[0].status_code == 200,
    )
    critical_body = responses[0].json()
    check(
        "临界样本偏差率展示为四舍五入后的 2.00",
        critical_body.get("deviation_pct") == "2.00",
        f"deviation_pct={critical_body.get('deviation_pct')}",
    )
    check(
        "临界样本按未舍入值判不合格",
        critical_body.get("overall") == "fail"
        and critical_body.get("deviation_ok") is False,
        f"overall={critical_body.get('overall')}",
    )

    # 2. 边界相等仍合格：偏差率恰 2.00%、极差率恰 3.00%
    boundary = {
        "target_nm": 100.00,
        "measured_nm": [100.50, 103.50, 102.00, 102.00, 102.00],
    }
    resp = httpx.post(URL, json=boundary, timeout=5.0)
    boundary_body = resp.json()
    check(
        "边界相等（2.00% / 3.00%）判合格",
        resp.status_code == 200
        and boundary_body.get("deviation_pct") == "2.00"
        and boundary_body.get("range_pct") == "3.00"
        and boundary_body.get("overall") == "pass",
        f"overall={boundary_body.get('overall')}",
    )

    # 3. 非法样本：只得到结构化错误
    invalid_cases = {
        "读数不足五个": {"target_nm": 100.00, "measured_nm": [100.00] * 4},
        "读数低于下限 1.00": {
            "target_nm": 100.00,
            "measured_nm": [0.99, 100.00, 100.00, 100.00, 100.00],
        },
        "读数超过两位小数": {
            "target_nm": 100.00,
            "measured_nm": [100.001, 100.00, 100.00, 100.00, 100.00],
        },
        "目标值高于上限 500.00": {
            "target_nm": 500.01,
            "measured_nm": [100.00] * 5,
        },
        "非数值读数": {
            "target_nm": 100.00,
            "measured_nm": ["abc", 100.00, 100.00, 100.00, 100.00],
        },
    }
    for name, payload in invalid_cases.items():
        resp = httpx.post(URL, json=payload, timeout=5.0)
        try:
            err = resp.json().get("error", {})
            ok = resp.status_code == 422 and err.get("code") == "VALIDATION_ERROR"
        except (ValueError, AttributeError):
            ok = False
        check(f"非法样本整体拒绝：{name}", ok, f"HTTP {resp.status_code}")

    # 3b. 临界精度：超长小数 / 轻微越界值不得经 float64 归并后被洗白。
    # 必须发送原始 JSON 文本——Python float 与 json= 会在客户端先归并。
    precision_cases = {
        "目标值超过两位小数 100.000000000000001": (
            URL,
            '{"target_nm": 100.000000000000001,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}',
            ("target_nm",),
        ),
        "读数略低于下限 0.99999999999999999": (
            URL,
            '{"target_nm": 1.00,'
            ' "measured_nm": [0.99999999999999999, 1.00, 1.00, 1.00, 1.00]}',
            ("measured_nm", 0),
        ),
        "读数略高于上限 500.00000000000001": (
            URL,
            '{"target_nm": 500.00,'
            ' "measured_nm": [500.00000000000001, 500.00, 500.00, 500.00, 500.00]}',
            ("measured_nm", 0),
        ),
        "批量中含超过两位小数的目标值": (
            BATCH_URL,
            '{"items": ['
            '{"target_nm": 100.00,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]},'
            '{"target_nm": 100.000000000000001,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}]}',
            ("items", 1, "target_nm"),
        ),
        "批量中含略低于下限的读数": (
            BATCH_URL,
            '{"items": [{"target_nm": 1.00,'
            ' "measured_nm": [0.99999999999999999, 1.00, 1.00, 1.00, 1.00]}]}',
            ("items", 0, "measured_nm", 0),
        ),
    }
    for name, (url, raw_json, field_suffix) in precision_cases.items():
        resp = httpx.post(
            url,
            content=raw_json,
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
        try:
            body = resp.json()
            details = body.get("error", {}).get("details", [])
            locs = [tuple(d.get("loc", ())[1:]) for d in details]  # 去掉 body 前缀
            ok = (
                resp.status_code == 422
                and body.get("error", {}).get("code") == "VALIDATION_ERROR"
                and "items" not in body
                and "overall" not in body
                and field_suffix in locs
            )
        except (ValueError, AttributeError):
            ok = False
        check(f"临界精度整体拒绝：{name}", ok, f"HTTP {resp.status_code}")

    # 4. 双项超限：必须同时暴露两个原因
    dual = {
        "target_nm": 100.00,
        "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00],
    }
    resp = httpx.post(URL, json=dual, timeout=5.0)
    dual_body = resp.json()
    reasons = dual_body.get("failure_reasons", [])
    check(
        "双项超限总结果不合格",
        resp.status_code == 200 and dual_body.get("overall") == "fail",
    )
    check(
        "双项超限暴露偏差率原因",
        dual_body.get("deviation_ok") is False
        and "deviation_pct_out_of_limit" in reasons,
        f"reasons={reasons}",
    )
    check(
        "双项超限暴露极差率原因",
        dual_body.get("range_ok") is False
        and "range_pct_out_of_limit" in reasons,
        f"reasons={reasons}",
    )

    # 5. 批量复核
    passing = {
        "target_nm": 100.00,
        "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95],
    }

    # 5a. 全合格批次
    resp = httpx.post(
        BATCH_URL, json={"items": [passing, boundary]}, timeout=5.0
    )
    all_pass = resp.json()
    check(
        "全合格批次：整批合格且汇总为零失败",
        resp.status_code == 200
        and all_pass.get("overall") == "pass"
        and all_pass.get("total") == 2
        and all_pass.get("passed_count") == 2
        and all_pass.get("failed_count") == 0
        and all_pass.get("failed_indices") == []
        and len(all_pass.get("items", [])) == 2,
        f"overall={all_pass.get('overall')}",
    )

    # 5b. 混合批次：失败序号、汇总，且全部测点均被计算（不提前终止）
    mixed_items = [passing, critical, boundary, dual]
    resp = httpx.post(BATCH_URL, json={"items": mixed_items}, timeout=5.0)
    mixed = resp.json()
    check(
        "混合批次：失败序号为 [1, 3] 且汇总正确",
        resp.status_code == 200
        and mixed.get("overall") == "fail"
        and mixed.get("total") == 4
        and mixed.get("passed_count") == 2
        and mixed.get("failed_count") == 2
        and mixed.get("failed_indices") == [1, 3],
        f"failed_indices={mixed.get('failed_indices')}",
    )
    check(
        "混合批次：首个失败后的测点仍完整计算",
        len(mixed.get("items", [])) == 4
        and mixed["items"][2].get("overall") == "pass"
        and mixed["items"][3].get("failure_reasons")
        == ["deviation_pct_out_of_limit", "range_pct_out_of_limit"],
    )

    # 5c. 临界值逐项等价：批量项与逐个调用单次接口的字段和值完全一致
    singles = [
        httpx.post(URL, json=item, timeout=5.0).json() for item in mixed_items
    ]
    check(
        "批量项与单次接口逐项等价（含临界样本）",
        mixed.get("items") == singles,
    )

    # 5d. 非法测点：整体拒绝，不产生部分结果
    bad_item = {
        "target_nm": 100.00,
        "measured_nm": [100.00, 0.50, 100.00, 100.00, 100.00],
    }
    invalid_batches = {
        "批次中含非法读数": {"items": [passing, bad_item, boundary]},
        "空批次": {"items": []},
        "超量批次（21 个测点）": {"items": [passing] * 21},
    }
    for name, payload in invalid_batches.items():
        resp = httpx.post(BATCH_URL, json=payload, timeout=5.0)
        try:
            body = resp.json()
            err = body.get("error", {})
            ok = (
                resp.status_code == 422
                and err.get("code") == "VALIDATION_ERROR"
                and "items" not in body
                and "overall" not in body
            )
        except (ValueError, AttributeError):
            ok = False
        check(f"非法批次整体拒绝且无部分结果：{name}", ok, f"HTTP {resp.status_code}")

    # 6. 校准档案纵向闭环（每次验收使用全新唯一编号，保证可重复运行）
    cal_sn = f"ACCEPT-{uuid.uuid4().hex[:12]}"
    c_url = cal_url(cal_sn)

    # 6a. 未知编号：结构化 404，且不泄露其他档案
    resp = httpx.get(c_url, timeout=5.0)
    try:
        nf = resp.json()
        ok = (
            resp.status_code == 404
            and nf.get("error", {}).get("code") == "WRENCH_NOT_FOUND"
            and isinstance(nf.get("error", {}).get("details"), list)
            and "history" not in nf
            and "status" not in nf
        )
    except (ValueError, AttributeError):
        ok = False
    check("档案：未知编号返回结构化 404 且不含档案数据", ok, f"HTTP {resp.status_code}")

    # 6b. 首次合格登记：自动建档，在用、计数 0、序号 1
    resp = httpx.post(c_url, json=passing, timeout=5.0)
    first = resp.json()
    check(
        "档案：首次合格登记自动建档为在用",
        resp.status_code == 200
        and first.get("wrench_sn") == cal_sn
        and first.get("status") == "in_service"
        and first.get("consecutive_fail_count") == 0
        and first.get("seq") == 1
        and first.get("overall") == "pass",
        f"status={first.get('status')} seq={first.get('seq')}",
    )

    # 6c. 第一次不合格 → 观察，计数 1
    resp = httpx.post(c_url, json=critical, timeout=5.0)
    fail1 = resp.json()
    check(
        "档案：首次不合格转为观察",
        resp.status_code == 200
        and fail1.get("status") == "observation"
        and fail1.get("consecutive_fail_count") == 1
        and fail1.get("seq") == 2
        and fail1.get("overall") == "fail",
        f"status={fail1.get('status')} count={fail1.get('consecutive_fail_count')}",
    )

    # 6d. 非法读数：422，不写入记录、不改变既有状态
    before = httpx.get(c_url, timeout=5.0).json()
    invalid_register = {
        "target_nm": 100.00,
        "measured_nm": [100.00, 0.50, 100.00, 100.00, 100.00],
    }
    resp = httpx.post(c_url, json=invalid_register, timeout=5.0)
    try:
        body = resp.json()
        rejected = (
            resp.status_code == 422
            and body.get("error", {}).get("code") == "VALIDATION_ERROR"
            and "overall" not in body
        )
    except (ValueError, AttributeError):
        rejected = False
    check("档案：非法读数整体拒绝（422）", rejected, f"HTTP {resp.status_code}")

    after_invalid = httpx.get(c_url, timeout=5.0).json()
    check(
        "档案：非法读数不写入记录也不改变既有状态",
        rejected
        and after_invalid.get("status") == before.get("status") == "observation"
        and after_invalid.get("consecutive_fail_count") == 1
        and after_invalid.get("total_records") == before.get("total_records") == 2
        and after_invalid.get("history") == before.get("history"),
        f"total_records={after_invalid.get('total_records')}",
    )

    # 6e. 第二次连续不合格 → 停用，计数 2
    resp = httpx.post(c_url, json=dual, timeout=5.0)
    fail2 = resp.json()
    check(
        "档案：连续两次不合格转为停用",
        resp.status_code == 200
        and fail2.get("status") == "out_of_service"
        and fail2.get("consecutive_fail_count") == 2
        and fail2.get("seq") == 3,
        f"status={fail2.get('status')} count={fail2.get('consecutive_fail_count')}",
    )

    # 6f. 任一后续合格 → 恢复在用，计数清零
    resp = httpx.post(c_url, json=passing, timeout=5.0)
    recovered = resp.json()
    check(
        "档案：合格后计数清零并恢复在用",
        resp.status_code == 200
        and recovered.get("status") == "in_service"
        and recovered.get("consecutive_fail_count") == 0
        and recovered.get("seq") == 4,
        f"status={recovered.get('status')} count={recovered.get('consecutive_fail_count')}",
    )

    # 6g. 查询：当前状态 + 连续不合格次数 + 按登记顺序排列的完整判定历史
    profile = httpx.get(c_url, timeout=5.0).json()
    history = profile.get("history", [])
    check(
        "档案：查询返回当前状态、连续不合格次数与完整顺序历史",
        profile.get("status") == "in_service"
        and profile.get("consecutive_fail_count") == 0
        and profile.get("total_records") == 4
        and [h.get("seq") for h in history] == [1, 2, 3, 4]
        and [h.get("overall") for h in history] == ["pass", "fail", "fail", "pass"]
        and all("deviation_pct" in h and "failure_reasons" in h for h in history),
        f"total_records={profile.get('total_records')}",
    )

    # 6h. 未知编号错误不得泄露本档案编号
    resp = httpx.get(cal_url(f"UNKNOWN-{uuid.uuid4().hex[:8]}"), timeout=5.0)
    check(
        "档案：未知编号 404 响应不泄露其他档案编号",
        resp.status_code == 404 and cal_sn not in resp.text,
        f"HTTP {resp.status_code}",
    )

    # 7. 误登记作废（每次验收使用全新唯一编号，保证可重复运行）
    void_sn = f"VOID-{uuid.uuid4().hex[:12]}"
    v_url = cal_url(void_sn)
    void_reason = "有效读数误登记到错误扳手编号，按现场单据作废。"

    # 7a. 未知扳手作废：结构化 404（WRENCH_NOT_FOUND），不泄露其他档案
    resp = httpx.request(
        "DELETE",
        void_url(f"GHOST-{uuid.uuid4().hex[:8]}", 1),
        json={"reason": void_reason},
        timeout=5.0,
    )
    try:
        nf = resp.json()
        nf_ok = (
            resp.status_code == 404
            and nf.get("error", {}).get("code") == "WRENCH_NOT_FOUND"
            and "history" not in nf
            and "status" not in nf
        )
    except (ValueError, AttributeError):
        nf_ok = False
    check("作废：未知扳手返回结构化 404 且不含档案数据", nf_ok, f"HTTP {resp.status_code}")

    # 7b. 造历史：pass, fail, fail → 停用/2
    httpx.post(v_url, json=passing, timeout=5.0)
    httpx.post(v_url, json=critical, timeout=5.0)
    httpx.post(v_url, json=dual, timeout=5.0)
    before_void = httpx.get(v_url, timeout=5.0).json()
    check(
        "作废：准备数据为停用、连续不合格 2、共 3 条",
        before_void.get("status") == "out_of_service"
        and before_void.get("consecutive_fail_count") == 2
        and before_void.get("total_records") == 3,
        f"status={before_void.get('status')}",
    )

    # 7c. 序号不存在：结构化 404（CALIBRATION_RECORD_NOT_FOUND），档案不变
    resp = httpx.request(
        "DELETE", void_url(void_sn, 99), json={"reason": void_reason}, timeout=5.0
    )
    try:
        body = resp.json()
        seq_nf_ok = (
            resp.status_code == 404
            and body.get("error", {}).get("code") == "CALIBRATION_RECORD_NOT_FOUND"
            and "history" not in body
            and "status" not in body
        )
    except (ValueError, AttributeError):
        seq_nf_ok = False
    check("作废：不存在序号返回结构化 404 且不含档案数据", seq_nf_ok, f"HTTP {resp.status_code}")
    unchanged = httpx.get(v_url, timeout=5.0).json()
    check(
        "作废：序号不存在不改动档案",
        unchanged == before_void,
    )

    # 7d. 作废末次不合格（seq=3）：重放剩余 pass, fail → 观察/1
    resp = httpx.request(
        "DELETE", void_url(void_sn, 3), json={"reason": void_reason}, timeout=5.0
    )
    voided = resp.json()
    check(
        "作废：作废末次不合格后恢复为观察、计数 1",
        resp.status_code == 200
        and voided.get("voided_seq") == 3
        and voided.get("wrench_sn") == void_sn
        and voided.get("reason") == void_reason
        and bool(voided.get("voided_at"))
        and voided.get("status") == "observation"
        and voided.get("consecutive_fail_count") == 1
        and voided.get("total_records") == 3,
        f"status={voided.get('status')} count={voided.get('consecutive_fail_count')}",
    )

    # 7e. 查询仍返回完整历史：每条补充 is_valid 及作废信息，原字段不变
    profile_v = httpx.get(v_url, timeout=5.0).json()
    history_v = profile_v.get("history", [])
    evidence_ok = (
        [h.get("seq") for h in history_v] == [1, 2, 3]
        and [h.get("is_valid") for h in history_v] == [True, True, False]
        and history_v[2].get("voided_at") == voided.get("voided_at")
        and history_v[2].get("void_reason") == void_reason
        and history_v[0].get("voided_at") is None
        and history_v[0].get("void_reason") is None
        and history_v[2].get("overall") == "fail"
        and history_v[2].get("failure_reasons")
        == ["deviation_pct_out_of_limit", "range_pct_out_of_limit"]
    )
    check(
        "作废：完整历史保留快照并为每条补充有效性/作废信息",
        evidence_ok,
        f"is_valid={[h.get('is_valid') for h in history_v]}",
    )

    # 7f. 重复作废：409 冲突且无副作用（原因、作废时刻、档案均不变）
    resp = httpx.request(
        "DELETE",
        void_url(void_sn, 3),
        json={"reason": "另一条重复作废原因不应被写入"},
        timeout=5.0,
    )
    try:
        conflict_body = resp.json()
        conflict_ok = (
            resp.status_code == 409
            and conflict_body.get("error", {}).get("code")
            == "CALIBRATION_RECORD_ALREADY_VOIDED"
        )
    except (ValueError, AttributeError):
        conflict_ok = False
    check("作废：重复作废返回 409 冲突", conflict_ok, f"HTTP {resp.status_code}")
    after_conflict = httpx.get(v_url, timeout=5.0).json()
    check(
        "作废：重复作废不改动档案与作废证据",
        after_conflict == profile_v,
    )

    # 7g. 作废中间记录：另建 fail, fail, pass, fail（观察/1），作废中间的
    #     合格 seq=3 → 剩余 fail, fail, fail 重放为停用/3
    mid_sn = f"MID-{uuid.uuid4().hex[:12]}"
    m_url = cal_url(mid_sn)
    for item in (critical, dual, passing, critical):
        httpx.post(m_url, json=item, timeout=5.0)
    mid_before = httpx.get(m_url, timeout=5.0).json()
    check(
        "作废（中间）：准备数据为观察、计数 1",
        mid_before.get("status") == "observation"
        and mid_before.get("consecutive_fail_count") == 1,
    )
    resp = httpx.request(
        "DELETE", void_url(mid_sn, 3), json={"reason": void_reason}, timeout=5.0
    )
    mid_void = resp.json()
    check(
        "作废：作废中间合格记录后按剩余历史重算为停用、计数 3",
        resp.status_code == 200
        and mid_void.get("voided_seq") == 3
        and mid_void.get("status") == "out_of_service"
        and mid_void.get("consecutive_fail_count") == 3
        and mid_void.get("total_records") == 4,
        f"status={mid_void.get('status')} count={mid_void.get('consecutive_fail_count')}",
    )
    mid_after = httpx.get(m_url, timeout=5.0).json()
    check(
        "作废（中间）：查询结果与重算摘要一致且仅 seq=3 被标记",
        mid_after.get("status") == "out_of_service"
        and mid_after.get("consecutive_fail_count") == 3
        and [h.get("is_valid") for h in mid_after.get("history", [])]
        == [True, True, False, True],
    )

    # 7h. 非法原因：写入前整体拒绝（422），档案与记录不变
    invalid_reasons = {
        "空原因": "",
        "纯空白原因": "   ",
        f"超过 200 字（{201} 字）": "x" * 201,
        "非字符串原因": None,
        "缺少 reason 字段": "__MISSING__",
    }
    for name, value in invalid_reasons.items():
        payload = {} if value == "__MISSING__" else {"reason": value}
        resp = httpx.request(
            "DELETE", void_url(mid_sn, 1), json=payload, timeout=5.0
        )
        try:
            body = resp.json()
            reason_ok = (
                resp.status_code == 422
                and body.get("error", {}).get("code") == "VALIDATION_ERROR"
            )
        except (ValueError, AttributeError):
            reason_ok = False
        check(f"作废：非法原因写入前整体拒绝：{name}", reason_ok, f"HTTP {resp.status_code}")
    check(
        "作废：非法原因未改动既有档案",
        httpx.get(m_url, timeout=5.0).json() == mid_after,
    )

    # 7i. 重启一致性：用全新仓库实例打开同一 SQLite 文件，作废证据与
    #     重算结果必须与 API 进程中的视图逐字段一致。
    restart_ok = None
    if RESTART_DB_PATH and os.path.exists(RESTART_DB_PATH):
        from app.calibration import CalibrationService
        from app.db import CalibrationRepository

        reopened = CalibrationService(CalibrationRepository(RESTART_DB_PATH))

        def _restart_check(sn: str, expected_status: str, expected_count: int) -> dict:
            profile = reopened.get_profile(sn).model_dump()
            live = httpx.get(cal_url(sn), timeout=5.0).json()
            same = profile == live
            return {
                "same": same,
                "status": profile.get("status"),
                "count": profile.get("consecutive_fail_count"),
                "expected_status": expected_status,
                "expected_count": expected_count,
            }

        r_void = _restart_check(void_sn, "observation", 1)
        restart_ok = (
            r_void["same"]
            and r_void["status"] == "observation"
            and r_void["count"] == 1
        )
        check(
            "作废：重启后作废证据与重算结果一致（末次作废档）",
            restart_ok,
            f"status={r_void['status']} count={r_void['count']} same={r_void['same']}",
        )
        r_mid = _restart_check(mid_sn, "out_of_service", 3)
        check(
            "作废：重启后作废证据与重算结果一致（中间作废档）",
            r_mid["same"]
            and r_mid["status"] == "out_of_service"
            and r_mid["count"] == 3,
            f"status={r_mid['status']} count={r_mid['count']} same={r_mid['same']}",
        )
    else:
        print("[SKIP] 未提供 RESTART_DB_PATH，跳过重库重启一致性核对", flush=True)

    print("\n临界样本证据（五次请求一致）:", flush=True)
    print(json.dumps(critical_body, ensure_ascii=False, indent=2), flush=True)
    print("\n混合批次汇总证据:", flush=True)
    print(
        json.dumps(
            {k: v for k, v in mixed.items() if k != "items"},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    print("\n校准档案纵向闭环证据（按登记顺序）:", flush=True)
    print(
        json.dumps(
            {
                "wrench_sn": profile.get("wrench_sn"),
                "status": profile.get("status"),
                "consecutive_fail_count": profile.get("consecutive_fail_count"),
                "total_records": profile.get("total_records"),
                "history": [
                    {"seq": h.get("seq"), "overall": h.get("overall")}
                    for h in history
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    print("\n误登记作废证据（含重算档案摘要）:", flush=True)
    print(
        json.dumps(
            {
                "voided_last": {
                    "wrench_sn": void_sn,
                    "response": {
                        k: voided.get(k)
                        for k in (
                            "voided_seq",
                            "voided_at",
                            "reason",
                            "status",
                            "consecutive_fail_count",
                            "total_records",
                        )
                    },
                    "history": [
                        {
                            "seq": h.get("seq"),
                            "overall": h.get("overall"),
                            "is_valid": h.get("is_valid"),
                            "voided_at": h.get("voided_at"),
                            "void_reason": h.get("void_reason"),
                        }
                        for h in history_v
                    ],
                },
                "voided_middle": {
                    "wrench_sn": mid_sn,
                    "response": {
                        k: mid_void.get(k)
                        for k in (
                            "voided_seq",
                            "status",
                            "consecutive_fail_count",
                            "total_records",
                        )
                    },
                    "history": [
                        {
                            "seq": h.get("seq"),
                            "overall": h.get("overall"),
                            "is_valid": h.get("is_valid"),
                        }
                        for h in mid_after.get("history", [])
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    if _failures:
        print(f"\n验收失败 {len(_failures)} 项: {', '.join(_failures)}", flush=True)
        return 1
    print("\n全部验收检查通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
