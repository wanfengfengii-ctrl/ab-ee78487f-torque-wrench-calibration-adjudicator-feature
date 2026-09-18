"""误登记作废闭环测试：标记不删除、按剩余历史重放重算、冲突与非法拒绝、持久化。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.calibration import (
    STATUS_IN_SERVICE,
    STATUS_OBSERVATION,
    STATUS_OUT_OF_SERVICE,
    CalibrationService,
    replay_profile,
)
from app.db import CalibrationRepository, StoredRecord
from app.main import app, get_calibration_service

#: 常规合格载荷
PASS_BODY = {
    "target_nm": 100.00,
    "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95],
}
#: 临界不合格载荷：偏差率 2.004%（展示 2.00%，判定 fail）
FAIL_BODY = {
    "target_nm": 100.00,
    "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02],
}

VOID_REASON = "误登记到错误扳手，实际读数属于另一台设备"


def _cal_url(wrench_sn: str) -> str:
    return f"/api/v1/wrenches/{wrench_sn}/calibrations"


def _void_url(wrench_sn: str, seq: int | str) -> str:
    return f"{_cal_url(wrench_sn)}/{seq}/void"


@pytest.fixture
def client(tmp_path):
    """每个测试一个独立临时 SQLite 库，互不污染。"""
    service = CalibrationService(CalibrationRepository(str(tmp_path / "cal.db")))
    app.state.calibration_service = service
    app.dependency_overrides[get_calibration_service] = lambda: service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    app.state.calibration_service = None


def _register(client, wrench_sn: str, bodies: list[dict]) -> list[dict]:
    """按顺序登记并返回每次的响应体。"""
    return [
        client.post(_cal_url(wrench_sn), json=body).json() for body in bodies
    ]


class TestVoidLastFailRestoresStatus:
    """作废末次不合格可恢复状态（验收要点）。"""

    def test_void_last_fail_recovers_from_out_of_service(self, client):
        _register(client, "W-100", [PASS_BODY, FAIL_BODY, FAIL_BODY])
        before = client.get(_cal_url("W-100")).json()
        assert before["status"] == STATUS_OUT_OF_SERVICE
        assert before["consecutive_fail_count"] == 2

        resp = client.post(
            _void_url("W-100", 3), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 200
        body = resp.json()
        # 重放剩余 [pass, fail] → 观察、连续不合格 1
        assert body["wrench_sn"] == "W-100"
        assert body["voided_seq"] == 3
        assert body["void_reason"] == VOID_REASON
        assert body["voided_at"]
        assert body["status"] == STATUS_OBSERVATION
        assert body["consecutive_fail_count"] == 1
        assert body["total_records"] == 3
        assert body["valid_records"] == 2

        profile = client.get(_cal_url("W-100")).json()
        assert profile["status"] == STATUS_OBSERVATION
        assert profile["consecutive_fail_count"] == 1

    def test_void_all_fails_returns_to_initial_state(self, client):
        _register(client, "W-101", [FAIL_BODY, FAIL_BODY])
        assert (
            client.get(_cal_url("W-101")).json()["status"]
            == STATUS_OUT_OF_SERVICE
        )

        r1 = client.post(_void_url("W-101", 2), json={"reason": VOID_REASON})
        assert r1.json()["status"] == STATUS_OBSERVATION
        assert r1.json()["consecutive_fail_count"] == 1

        # 全部作废：没有任何有效判定，档案回到初始态
        r2 = client.post(_void_url("W-101", 1), json={"reason": VOID_REASON})
        assert r2.status_code == 200
        assert r2.json()["status"] == STATUS_IN_SERVICE
        assert r2.json()["consecutive_fail_count"] == 0
        assert r2.json()["valid_records"] == 0
        assert r2.json()["total_records"] == 2

        profile = client.get(_cal_url("W-101")).json()
        assert profile["status"] == STATUS_IN_SERVICE
        assert profile["consecutive_fail_count"] == 0
        assert profile["total_records"] == 2


class TestVoidMiddleRecordReplaysRemaining:
    """作废中间记录会按剩余历史重算（验收要点）。"""

    def test_void_middle_fail_recomputes_status(self, client):
        _register(client, "W-110", [PASS_BODY, FAIL_BODY, FAIL_BODY])
        assert (
            client.get(_cal_url("W-110")).json()["status"]
            == STATUS_OUT_OF_SERVICE
        )

        # 作废中间的 seq 2：剩余 [pass, fail] → 观察、计数 1
        resp = client.post(_void_url("W-110", 2), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        body = resp.json()
        assert body["voided_seq"] == 2
        assert body["status"] == STATUS_OBSERVATION
        assert body["consecutive_fail_count"] == 1

        profile = client.get(_cal_url("W-110")).json()
        assert profile["status"] == STATUS_OBSERVATION
        assert profile["consecutive_fail_count"] == 1
        assert profile["total_records"] == 3

    def test_void_middle_pass_can_worsen_status(self, client):
        _register(client, "W-111", [FAIL_BODY, FAIL_BODY, PASS_BODY])
        assert client.get(_cal_url("W-111")).json()["status"] == STATUS_IN_SERVICE

        # 作废中间的合格 seq 3：剩余 [fail, fail] → 停用、计数 2
        resp = client.post(_void_url("W-111", 3), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_OUT_OF_SERVICE
        assert resp.json()["consecutive_fail_count"] == 2

    def test_register_after_void_continues_from_recomputed_state(self, client):
        _register(client, "W-112", [FAIL_BODY, FAIL_BODY])
        client.post(_void_url("W-112", 2), json={"reason": VOID_REASON})
        # 重算后为观察(1)；再登记一次不合格 → 停用(2)，seq 继续递增
        rec = client.post(_cal_url("W-112"), json=FAIL_BODY).json()
        assert rec["seq"] == 3
        assert rec["status"] == STATUS_OUT_OF_SERVICE
        assert rec["consecutive_fail_count"] == 2


class TestVoidHistoryKeepsFullAuditTrail:
    """查询仍返回完整历史，每条记录补充是否有效及作废信息。"""

    def test_voided_record_keeps_snapshot_and_carries_void_info(self, client):
        _register(client, "W-120", [PASS_BODY, FAIL_BODY, PASS_BODY])
        client.post(_void_url("W-120", 2), json={"reason": VOID_REASON})

        profile = client.get(_cal_url("W-120")).json()
        assert profile["total_records"] == 3
        history = profile["history"]
        assert [h["seq"] for h in history] == [1, 2, 3]
        assert [h["is_valid"] for h in history] == [True, False, True]

        voided = history[1]
        assert voided["void_reason"] == VOID_REASON
        assert voided["voided_at"]
        # 原快照字段保留（审计证据不丢失）
        assert voided["overall"] == "fail"
        assert voided["failure_reasons"] == ["deviation_pct_out_of_limit"]
        assert voided["registered_at"]

        for record in (history[0], history[2]):
            assert record["void_reason"] is None
            assert record["voided_at"] is None

    def test_unvoided_records_fields_unchanged(self, client):
        _register(client, "W-121", [PASS_BODY, FAIL_BODY])
        before = client.get(_cal_url("W-121")).json()["history"]

        client.post(_void_url("W-121", 2), json={"reason": VOID_REASON})
        after = client.get(_cal_url("W-121")).json()["history"]

        # 未作废记录的所有字段（含原快照字段）逐条保持不变
        assert after[0] == before[0]
        assert after[0]["is_valid"] is True
        assert after[0]["void_reason"] is None
        assert after[0]["voided_at"] is None


class TestVoidNotFound:
    """序号不存在：结构化 404，不泄露其他档案。"""

    def test_unknown_wrench_returns_404(self, client):
        resp = client.post(
            _void_url("W-130", 1), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "CALIBRATION_RECORD_NOT_FOUND"
        assert err["message"]
        assert isinstance(err["details"], list)
        # 错误外形不携带任何档案数据
        body = resp.json()
        assert "status" not in body
        assert "history" not in body

    def test_missing_seq_returns_404(self, client):
        _register(client, "W-131", [PASS_BODY])
        resp = client.post(
            _void_url("W-131", 99), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CALIBRATION_RECORD_NOT_FOUND"

    def test_not_found_does_not_leak_other_profiles(self, client):
        _register(client, "W-132", [PASS_BODY])
        resp = client.post(
            _void_url("W-133", 1), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        assert "W-132" not in resp.text

    @pytest.mark.parametrize("seq", ["0", "-1", "abc", "1.5"])
    def test_invalid_seq_path_param_rejected(self, client, seq):
        _register(client, "W-134", [PASS_BODY])
        resp = client.post(
            _void_url("W-134", seq), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


class TestVoidConflict:
    """重复作废返回冲突且不改动档案（验收要点）。"""

    def test_second_void_returns_409_without_side_effects(self, client):
        _register(client, "W-140", [PASS_BODY, FAIL_BODY, FAIL_BODY])
        first = client.post(_void_url("W-140", 3), json={"reason": VOID_REASON})
        assert first.status_code == 200
        profile_after_first = client.get(_cal_url("W-140")).json()

        resp = client.post(
            _void_url("W-140", 3), json={"reason": "再次作废同一序号"}
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "CALIBRATION_RECORD_ALREADY_VOIDED"
        assert err["message"]
        assert isinstance(err["details"], list)

        # 无副作用：档案状态、计数与历史（含首次作废信息）完全不变
        profile_after_second = client.get(_cal_url("W-140")).json()
        assert profile_after_second == profile_after_first
        assert profile_after_second["status"] == STATUS_OBSERVATION
        assert profile_after_second["consecutive_fail_count"] == 1
        voided = profile_after_second["history"][2]
        assert voided["is_valid"] is False
        assert voided["void_reason"] == VOID_REASON  # 保留首次原因
        assert voided["voided_at"] == first.json()["voided_at"]


class TestVoidReasonValidation:
    """原因非法在写入前整体拒绝。"""

    @pytest.mark.parametrize(
        "reason", ["", "   ", "x" * 201, "　" * 3]
    )
    def test_illegal_reason_rejected_with_422(self, client, reason):
        _register(client, "W-150", [FAIL_BODY])
        resp = client.post(_void_url("W-150", 1), json={"reason": reason})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("payload", [{}, {"reason": 123}, {"reason": None},
                                         {"reason": "ok", "extra": 1}])
    def test_malformed_body_rejected_with_422(self, client, payload):
        _register(client, "W-151", [FAIL_BODY])
        resp = client.post(_void_url("W-151", 1), json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_reason_boundary_lengths_accepted(self, client):
        _register(client, "W-152", [FAIL_BODY, FAIL_BODY])
        r1 = client.post(_void_url("W-152", 1), json={"reason": "误"})
        assert r1.status_code == 200
        assert r1.json()["void_reason"] == "误"
        r2 = client.post(_void_url("W-152", 2), json={"reason": "x" * 200})
        assert r2.status_code == 200
        assert r2.json()["void_reason"] == "x" * 200

    def test_reason_stripped_before_persist(self, client):
        _register(client, "W-153", [FAIL_BODY])
        resp = client.post(
            _void_url("W-153", 1), json={"reason": f"  {VOID_REASON}  "}
        )
        assert resp.status_code == 200
        assert resp.json()["void_reason"] == VOID_REASON

    def test_illegal_reason_writes_nothing(self, client):
        _register(client, "W-154", [PASS_BODY, FAIL_BODY])
        before = client.get(_cal_url("W-154")).json()

        resp = client.post(_void_url("W-154", 2), json={"reason": "x" * 201})
        assert resp.status_code == 422

        after = client.get(_cal_url("W-154")).json()
        assert after == before
        assert all(h["is_valid"] for h in after["history"])


class TestVoidPersistenceAcrossRestart:
    """服务重启后作废证据和重算结果一致（验收要点）。"""

    def test_void_evidence_and_recomputed_state_survive_restart(
        self, client, tmp_path
    ):
        db_path = str(tmp_path / "cal.db")
        _register(client, "W-160", [PASS_BODY, FAIL_BODY, FAIL_BODY])
        resp = client.post(_void_url("W-160", 2), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        voided_at = resp.json()["voided_at"]

        # 全新仓库/服务实例指向同一文件（模拟服务重启）
        reopened = CalibrationService(CalibrationRepository(db_path))
        profile = reopened.get_profile("W-160")

        # 重算结果一致：剩余 [pass, fail] → 观察、计数 1
        assert profile.status == STATUS_OBSERVATION
        assert profile.consecutive_fail_count == 1
        assert profile.total_records == 3

        # 作废证据一致：快照保留、原因与作废时间不变
        voided = profile.history[1]
        assert voided.is_valid is False
        assert voided.void_reason == VOID_REASON
        assert voided.voided_at == voided_at
        assert voided.overall == "fail"
        assert profile.history[0].is_valid is True
        assert profile.history[0].void_reason is None
        assert profile.history[2].is_valid is True

        # 重启后重复作废仍是 409 语义（领域异常），状态不被改动
        with pytest.raises(Exception) as excinfo:
            reopened.void_record("W-160", 2, "重复作废")
        assert excinfo.type.__name__ == "RecordAlreadyVoidedError"
        assert reopened.get_profile("W-160").status == STATUS_OBSERVATION


class TestReplayProfilePureFunction:
    """状态重放纯函数：由剩余有效历史重算 (状态, 连续不合格次数)。"""

    @staticmethod
    def _records(overalls: list[str]) -> list[StoredRecord]:
        return [
            StoredRecord(
                seq=index + 1,
                registered_at="t0",
                snapshot={"overall": overall},
            )
            for index, overall in enumerate(overalls)
        ]

    @pytest.mark.parametrize(
        "overalls,expected",
        [
            ([], (STATUS_IN_SERVICE, 0)),
            (["pass"], (STATUS_IN_SERVICE, 0)),
            (["fail"], (STATUS_OBSERVATION, 1)),
            (["fail", "fail"], (STATUS_OUT_OF_SERVICE, 2)),
            (["pass", "fail"], (STATUS_OBSERVATION, 1)),
            (["fail", "pass"], (STATUS_IN_SERVICE, 0)),
            (["fail", "fail", "fail"], (STATUS_OUT_OF_SERVICE, 3)),
            (["fail", "fail", "pass"], (STATUS_IN_SERVICE, 0)),
            (["pass", "fail", "pass", "fail"], (STATUS_OBSERVATION, 1)),
        ],
    )
    def test_replay_table(self, overalls, expected):
        assert replay_profile(self._records(overalls)) == expected
