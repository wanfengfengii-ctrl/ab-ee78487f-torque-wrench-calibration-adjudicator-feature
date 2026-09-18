"""误登记作废测试：软作废保留快照、按剩余有效历史重放重算、冲突与未找到、
原因整体拒绝，以及重启后作废证据与重算结果的持久化一致性。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.calibration import (
    STATUS_IN_SERVICE,
    STATUS_OBSERVATION,
    STATUS_OUT_OF_SERVICE,
    CalibrationService,
    replay,
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
#: 双项超限载荷（同样判定 fail）
DUAL_FAIL_BODY = {
    "target_nm": 100.00,
    "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00],
}

VOID_REASON = "读数被登记到了错误扳手编号，属于误登记。"


def _cal_url(wrench_sn: str) -> str:
    return f"/api/v1/wrenches/{wrench_sn}/calibrations"


def _void_url(wrench_sn: str, seq: int) -> str:
    return f"{_cal_url(wrench_sn)}/{seq}"


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


def _register_sequence(client, wrench_sn: str, bodies: list[dict]) -> None:
    for body in bodies:
        client.post(_cal_url(wrench_sn), json=body)


def _record_by_seq(profile: dict, seq: int) -> dict:
    return next(h for h in profile["history"] if h["seq"] == seq)


#: 作废不得改动的原始字段：登记序号、登记时刻 + 完整单次判定快照
_ORIGINAL_FIELDS = (
    "seq",
    "registered_at",
    "target_nm",
    "measured_nm",
    "mean_nm",
    "deviation_pct",
    "deviation_limit_pct",
    "deviation_ok",
    "range_pct",
    "range_limit_pct",
    "range_ok",
    "overall",
    "failure_reasons",
)


class TestVoidLastFailureRestoresState:
    def test_voiding_last_fail_restores_observation_and_count(self, client):
        sn = "V-001"
        _register_sequence(client, sn, [PASS_BODY, FAIL_BODY, DUAL_FAIL_BODY])
        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 2

        resp = client.request("DELETE", _void_url(sn, 3), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        body = resp.json()
        # 响应返回被作废序号及重算后的档案摘要
        assert body["voided_seq"] == 3
        assert body["wrench_sn"] == sn
        assert body["reason"] == VOID_REASON
        assert body["voided_at"]
        assert body["status"] == STATUS_OBSERVATION
        assert body["consecutive_fail_count"] == 1
        assert body["total_records"] == 3

        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_OBSERVATION
        assert profile["consecutive_fail_count"] == 1
        assert profile["total_records"] == 3

    def test_voiding_both_failing_records_restores_in_service(self, client):
        sn = "V-002"
        _register_sequence(client, sn, [PASS_BODY, FAIL_BODY, DUAL_FAIL_BODY])
        client.request("DELETE", _void_url(sn, 3), json={"reason": VOID_REASON})
        resp = client.request("DELETE", _void_url(sn, 2), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_IN_SERVICE
        assert resp.json()["consecutive_fail_count"] == 0

        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_IN_SERVICE
        assert profile["consecutive_fail_count"] == 0
        assert profile["total_records"] == 3

    def test_history_marks_voided_and_keeps_original_snapshot_untouched(self, client):
        sn = "V-003"
        _register_sequence(client, sn, [PASS_BODY, FAIL_BODY, DUAL_FAIL_BODY])
        before = client.get(_cal_url(sn)).json()["history"]

        resp = client.request("DELETE", _void_url(sn, 3), json={"reason": VOID_REASON})
        voided_at = resp.json()["voided_at"]

        history = client.get(_cal_url(sn)).json()["history"]
        assert [h["seq"] for h in history] == [1, 2, 3]
        # 未作废记录：原字段与作废前逐字节一致，仅补充 is_valid/作废空字段
        for seq in (1, 2):
            for key in _ORIGINAL_FIELDS:
                assert history[seq - 1][key] == before[seq - 1][key]
            assert history[seq - 1]["is_valid"] is True
            assert history[seq - 1]["voided_at"] is None
            assert history[seq - 1]["void_reason"] is None
        # 已作废记录：原快照字段保持不变，另附作废证据
        voided = history[2]
        for key in _ORIGINAL_FIELDS:
            assert voided[key] == before[2][key]
        assert voided["is_valid"] is False
        assert voided["voided_at"] == voided_at
        assert voided["void_reason"] == VOID_REASON


class TestVoidMiddleRecordRecomputes:
    def test_replay_recomputes_from_remaining_valid_history(self, client):
        # fail, fail, pass, fail：末尾状态为观察（计数 1）
        sn = "V-010"
        _register_sequence(client, sn, [FAIL_BODY, DUAL_FAIL_BODY, PASS_BODY, FAIL_BODY])
        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_OBSERVATION
        assert profile["consecutive_fail_count"] == 1

        # 作废中间的合格记录（seq=3）：剩余 fail, fail, fail 重放 →
        # 观察 → 停用(2) → 停用(3)
        resp = client.request("DELETE", _void_url(sn, 3), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == STATUS_OUT_OF_SERVICE
        assert body["consecutive_fail_count"] == 3
        assert body["total_records"] == 4

        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 3
        assert _record_by_seq(profile, 3)["is_valid"] is False

    def test_void_first_record_replays_rest_from_scratch(self, client):
        # fail, fail, pass, fail → 作废首个 fail：剩余 fail, pass, fail
        # 重放：观察(1) → 在用(0) → 观察(1)
        sn = "V-011"
        _register_sequence(client, sn, [FAIL_BODY, DUAL_FAIL_BODY, PASS_BODY, FAIL_BODY])
        resp = client.request("DELETE", _void_url(sn, 1), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_OBSERVATION
        assert resp.json()["consecutive_fail_count"] == 1

    def test_voiding_only_record_leaves_neutral_in_service_profile(self, client):
        sn = "V-012"
        client.post(_cal_url(sn), json=FAIL_BODY)
        resp = client.request("DELETE", _void_url(sn, 1), json={"reason": VOID_REASON})
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_IN_SERVICE
        assert resp.json()["consecutive_fail_count"] == 0
        assert resp.json()["total_records"] == 1
        # 档案仍可查询，历史保留这条作废记录
        profile = client.get(_cal_url(sn)).json()
        assert profile["total_records"] == 1
        assert profile["history"][0]["is_valid"] is False

    def test_registration_after_void_continues_sequence_and_recomputes_live(self, client):
        sn = "V-013"
        _register_sequence(client, sn, [FAIL_BODY, DUAL_FAIL_BODY])
        client.request("DELETE", _void_url(sn, 2), json={"reason": VOID_REASON})
        # 序号在含作废记录之上继续递增（不复用作废序号）
        rec = client.post(_cal_url(sn), json=FAIL_BODY)
        assert rec.json()["seq"] == 3
        # 有效历史为 fail(seq1), fail(seq3)：重放后停用、计数 2
        profile = client.get(_cal_url(sn)).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 2


class TestReplayPureFunction:
    @staticmethod
    def _stored(seq: int, overall: str) -> StoredRecord:
        return StoredRecord(
            seq=seq,
            registered_at="t",
            snapshot={"overall": overall},
            is_valid=True,
        )

    @pytest.mark.parametrize(
        "overalls,expected",
        [
            ([], (STATUS_IN_SERVICE, 0)),
            (["pass"], (STATUS_IN_SERVICE, 0)),
            (["fail"], (STATUS_OBSERVATION, 1)),
            (["fail", "fail"], (STATUS_OUT_OF_SERVICE, 2)),
            (["fail", "fail", "fail"], (STATUS_OUT_OF_SERVICE, 3)),
            (["fail", "pass", "fail"], (STATUS_OBSERVATION, 1)),
            (["pass", "fail", "fail"], (STATUS_OUT_OF_SERVICE, 2)),
            (["fail", "fail", "pass"], (STATUS_IN_SERVICE, 0)),
        ],
    )
    def test_replay_table(self, overalls, expected):
        records = [self._stored(i + 1, o) for i, o in enumerate(overalls)]
        assert replay(records) == expected


class TestDuplicateVoidHasNoSideEffect:
    def test_second_void_returns_conflict_and_changes_nothing(self, client):
        sn = "V-020"
        _register_sequence(client, sn, [PASS_BODY, FAIL_BODY, DUAL_FAIL_BODY])
        first = client.request(
            "DELETE", _void_url(sn, 2), json={"reason": VOID_REASON}
        )
        assert first.status_code == 200
        # 作废 seq2 后有效历史为 pass, fail → 观察/1
        before = client.get(_cal_url(sn)).json()

        resp = client.request(
            "DELETE", _void_url(sn, 2), json={"reason": "再次作废的另一个原因"}
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "CALIBRATION_RECORD_ALREADY_VOIDED"
        assert err["details"]
        assert err["details"][0]["loc"] == ["path", "seq"]

        after = client.get(_cal_url(sn)).json()
        # 档案摘要与作废记录证据（含首次原因与作废时刻）均不被改动
        assert after == before
        record = _record_by_seq(after, 2)
        assert record["void_reason"] == VOID_REASON
        assert record["voided_at"] == first.json()["voided_at"]

    def test_conflict_does_not_block_voiding_other_records(self, client):
        sn = "V-021"
        _register_sequence(client, sn, [FAIL_BODY, FAIL_BODY])
        client.request("DELETE", _void_url(sn, 1), json={"reason": VOID_REASON})
        conflict = client.request(
            "DELETE", _void_url(sn, 1), json={"reason": VOID_REASON}
        )
        assert conflict.status_code == 409
        # 另一条记录仍可正常作废
        ok = client.request("DELETE", _void_url(sn, 2), json={"reason": VOID_REASON})
        assert ok.status_code == 200
        assert ok.json()["status"] == STATUS_IN_SERVICE


class TestVoidNotFound:
    def test_unknown_wrench_returns_structured_404_without_leak(self, client):
        client.post(_cal_url("V-030"), json=PASS_BODY)
        resp = client.request(
            "DELETE", _void_url("V-031", 1), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "WRENCH_NOT_FOUND"
        assert "history" not in resp.json()
        assert "status" not in resp.json()
        assert "V-030" not in resp.text

    def test_unknown_seq_returns_structured_404_without_profile_data(self, client):
        sn = "V-032"
        client.post(_cal_url(sn), json=PASS_BODY)
        resp = client.request(
            "DELETE", _void_url(sn, 99), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "CALIBRATION_RECORD_NOT_FOUND"
        assert err["details"][0]["loc"] == ["path", "seq"]
        body = resp.json()
        assert "history" not in body
        assert "status" not in body

    def test_unknown_seq_does_not_mutate_profile_or_records(self, client):
        sn = "V-033"
        client.post(_cal_url(sn), json=FAIL_BODY)
        before = client.get(_cal_url(sn)).json()
        resp = client.request(
            "DELETE", _void_url(sn, 2), json={"reason": VOID_REASON}
        )
        assert resp.status_code == 404
        after = client.get(_cal_url(sn)).json()
        assert after == before

    @pytest.mark.parametrize("seq_value", ["abc", "0", "-1"])
    def test_invalid_seq_path_rejected(self, client, seq_value):
        resp = client.request(
            "DELETE",
            f"{_cal_url('V-034')}/{seq_value}",
            json={"reason": VOID_REASON},
        )
        assert resp.status_code == 422


class TestVoidReasonValidation:
    @pytest.mark.parametrize(
        "reason",
        [
            "",
            "   ",
            "\t\n ",
            "x" * 201,
        ],
    )
    def test_invalid_reason_rejected_before_write(self, client, reason):
        sn = "V-040"
        client.post(_cal_url(sn), json=FAIL_BODY)
        before = client.get(_cal_url(sn)).json()

        resp = client.request(
            "DELETE", _void_url(sn, 1), json={"reason": reason}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

        after = client.get(_cal_url(sn)).json()
        assert after == before
        assert after["history"][0]["is_valid"] is True

    def test_valid_boundary_lengths_accepted(self, client):
        sn = "V-041"
        client.post(_cal_url(sn), json=FAIL_BODY)
        resp = client.request(
            "DELETE", _void_url(sn, 1), json={"reason": "x" * 200}
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "x" * 200

    def test_non_string_and_missing_reason_rejected(self, client):
        client.post(_cal_url("V-042"), json=FAIL_BODY)
        for payload in ({"reason": 123}, {"reason": None}, {}, {"why": VOID_REASON}):
            resp = client.request(
                "DELETE", _void_url("V-042", 1), json=payload
            )
            assert resp.status_code == 422, resp.json()

    def test_extra_field_rejected(self, client):
        client.post(_cal_url("V-043"), json=FAIL_BODY)
        resp = client.request(
            "DELETE",
            _void_url("V-043", 1),
            json={"reason": VOID_REASON, "operator": "bob"},
        )
        assert resp.status_code == 422

    def test_reason_surrounding_whitespace_trimmed(self, client):
        sn = "V-044"
        client.post(_cal_url(sn), json=FAIL_BODY)
        resp = client.request(
            "DELETE", _void_url(sn, 1), json={"reason": f"  {VOID_REASON}\n"}
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == VOID_REASON
        record = client.get(_cal_url(sn)).json()["history"][0]
        assert record["void_reason"] == VOID_REASON


class TestVoidPersistenceAcrossRestart:
    def test_void_evidence_and_recomputed_state_survive_restart(self, client, tmp_path):
        db_path = str(tmp_path / "cal.db")
        sn = "V-060"
        _register_sequence(client, sn, [PASS_BODY, FAIL_BODY, DUAL_FAIL_BODY])
        void_resp = client.request(
            "DELETE", _void_url(sn, 3), json={"reason": VOID_REASON}
        ).json()

        # 全新仓库/服务实例指向同一文件（模拟服务重启）
        reopened = CalibrationService(CalibrationRepository(db_path))
        profile = reopened.get_profile(sn)

        # 重算结果一致：有效历史 pass, fail → 观察/1
        assert profile.status == STATUS_OBSERVATION
        assert profile.consecutive_fail_count == 1
        assert profile.total_records == 3
        by_seq = {r.seq: r for r in profile.history}
        assert by_seq[3].is_valid is False
        assert by_seq[3].void_reason == VOID_REASON
        assert by_seq[3].voided_at == void_resp["voided_at"]
        # 原快照字段未被删除或篡改（作废仅追加标记，快照字段平铺在响应中）
        assert by_seq[3].overall == "fail"
        assert by_seq[1].is_valid is True
        assert by_seq[1].voided_at is None

    def test_old_database_is_migrated_with_void_columns(self, tmp_path):
        """旧版库（无作废三列）重建仓库后平滑加列，作废功能可用。"""
        import sqlite3

        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE wrench_profiles (
                    wrench_sn TEXT PRIMARY KEY, status TEXT NOT NULL,
                    consecutive_fail_count INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE calibration_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wrench_sn TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    registered_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    UNIQUE (wrench_sn, seq)
                );
                INSERT INTO wrench_profiles
                    VALUES ('LEGACY', 'observation', 1, 't0');
                INSERT INTO calibration_records
                    (wrench_sn, seq, registered_at, snapshot_json)
                    VALUES ('LEGACY', 1, 't0', '{"overall": "fail"}');
                """
            )
            conn.commit()
        finally:
            conn.close()

        service = CalibrationService(CalibrationRepository(db_path))
        result = service.void_record("LEGACY", 1, VOID_REASON)
        assert result.status == STATUS_IN_SERVICE
        assert result.consecutive_fail_count == 0
        # 迁移可重复执行：再次构造仓库不报错
        CalibrationRepository(db_path)
