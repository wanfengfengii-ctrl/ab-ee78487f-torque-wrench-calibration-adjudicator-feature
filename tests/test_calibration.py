"""校准档案纵向闭环测试：建档、状态迁移、计数清零、非法不写入、隔离与持久化。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from decimal import Decimal

from app.calibration import (
    STATUS_IN_SERVICE,
    STATUS_OBSERVATION,
    STATUS_OUT_OF_SERVICE,
    CalibrationService,
    transition,
)
from app.db import CalibrationRepository, ProfileRow
from app.main import app, get_calibration_service
from app.models import TorqueVerifyRequest

VERIFY_URL = "/api/v1/torque/verify"


def _cal_url(wrench_sn: str) -> str:
    return f"/api/v1/wrenches/{wrench_sn}/calibrations"


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
#: 双项超限载荷
DUAL_FAIL_BODY = {
    "target_nm": 100.00,
    "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00],
}
#: 非法载荷：读数越界
INVALID_BODY = {
    "target_nm": 100.00,
    "measured_nm": [100.00, 0.50, 100.00, 100.00, 100.00],
}


def _req(body: dict) -> TorqueVerifyRequest:
    """由测试载荷构造领域层请求（按文本转 Decimal，与请求层等价）。"""
    return TorqueVerifyRequest(
        target_nm=Decimal(str(body["target_nm"])),
        measured_nm=[Decimal(str(v)) for v in body["measured_nm"]],
    )


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


def _assert_verification_fields(record: dict) -> None:
    """快照须包含单次接口的全部判定字段。"""
    for key in (
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
    ):
        assert key in record


class TestFirstRegistrationCreatesProfile:
    def test_first_pass_creates_in_service_profile(self, client):
        resp = client.post(_cal_url("W-001"), json=PASS_BODY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["wrench_sn"] == "W-001"
        assert body["status"] == STATUS_IN_SERVICE
        assert body["consecutive_fail_count"] == 0
        assert body["seq"] == 1
        assert body["overall"] == "pass"
        _assert_verification_fields(body)
        assert body["registered_at"]

        profile = client.get(_cal_url("W-001")).json()
        assert profile["status"] == STATUS_IN_SERVICE
        assert profile["consecutive_fail_count"] == 0
        assert profile["total_records"] == 1
        assert len(profile["history"]) == 1

    def test_first_fail_creates_observation_profile(self, client):
        resp = client.post(_cal_url("W-002"), json=FAIL_BODY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == STATUS_OBSERVATION
        assert body["consecutive_fail_count"] == 1
        assert body["seq"] == 1
        assert body["overall"] == "fail"

    def test_query_before_registration_is_404(self, client):
        resp = client.get(_cal_url("NEVER-SEEN"))
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "WRENCH_NOT_FOUND"
        assert body["error"]["message"]
        assert isinstance(body["error"]["details"], list)
        assert body["error"]["details"][0]["loc"] == ["path", "wrench_sn"]
        # 错误外形不得携带任何档案数据
        assert "history" not in body
        assert "status" not in body


class TestStateMigration:
    def test_two_consecutive_failures_lead_to_out_of_service(self, client):
        r1 = client.post(_cal_url("W-010"), json=PASS_BODY).json()
        assert r1["status"] == STATUS_IN_SERVICE

        r2 = client.post(_cal_url("W-010"), json=FAIL_BODY).json()
        assert r2["status"] == STATUS_OBSERVATION
        assert r2["consecutive_fail_count"] == 1
        assert r2["seq"] == 2

        r3 = client.post(_cal_url("W-010"), json=DUAL_FAIL_BODY).json()
        assert r3["status"] == STATUS_OUT_OF_SERVICE
        assert r3["consecutive_fail_count"] == 2
        assert r3["seq"] == 3

        profile = client.get(_cal_url("W-010")).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 2

    def test_pass_after_failures_resets_count_and_restores(self, client):
        client.post(_cal_url("W-011"), json=FAIL_BODY)
        client.post(_cal_url("W-011"), json=FAIL_BODY)
        profile = client.get(_cal_url("W-011")).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 2

        rec = client.post(_cal_url("W-011"), json=PASS_BODY).json()
        assert rec["status"] == STATUS_IN_SERVICE
        assert rec["consecutive_fail_count"] == 0

        profile = client.get(_cal_url("W-011")).json()
        assert profile["status"] == STATUS_IN_SERVICE
        assert profile["consecutive_fail_count"] == 0
        assert [h["overall"] for h in profile["history"]] == [
            "fail",
            "fail",
            "pass",
        ]

    def test_pass_breaks_streak_then_fail_starts_fresh(self, client):
        client.post(_cal_url("W-012"), json=FAIL_BODY)  # observation, 1
        client.post(_cal_url("W-012"), json=PASS_BODY)  # in_service, 0
        rec = client.post(_cal_url("W-012"), json=FAIL_BODY)  # observation, 1
        assert rec.json()["status"] == STATUS_OBSERVATION
        assert rec.json()["consecutive_fail_count"] == 1

    def test_repeated_failures_keep_out_of_service_and_accumulate(self, client):
        for _ in range(3):
            client.post(_cal_url("W-013"), json=FAIL_BODY)
        profile = client.get(_cal_url("W-013")).json()
        assert profile["status"] == STATUS_OUT_OF_SERVICE
        assert profile["consecutive_fail_count"] == 3


class TestInvalidReadingDoesNotWrite:
    def test_invalid_reading_rejected_without_write_or_state_change(self, client):
        # 既有状态：观察（1 次不合格）
        client.post(_cal_url("W-020"), json=PASS_BODY)
        client.post(_cal_url("W-020"), json=FAIL_BODY)
        before = client.get(_cal_url("W-020")).json()
        assert before["status"] == STATUS_OBSERVATION
        assert before["total_records"] == 2

        # 非法读数（越界）→ 与单次接口一致的结构化 422
        resp = client.post(_cal_url("W-020"), json=INVALID_BODY)
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_ERROR"
        assert err["details"]
        assert "overall" not in resp.json()

        # 非法读数个数同样拒绝
        resp = client.post(
            _cal_url("W-020"),
            json={"target_nm": 100.00, "measured_nm": [100.00] * 4},
        )
        assert resp.status_code == 422

        # 档案状态与历史完全不变
        after = client.get(_cal_url("W-020")).json()
        assert after["status"] == before["status"]
        assert after["consecutive_fail_count"] == before["consecutive_fail_count"]
        assert after["total_records"] == 2
        assert after["history"] == before["history"]

    def test_invalid_reading_on_unknown_wrench_does_not_create_profile(self, client):
        resp = client.post(_cal_url("W-021"), json=INVALID_BODY)
        assert resp.status_code == 422
        follow_up = client.get(_cal_url("W-021"))
        assert follow_up.status_code == 404
        assert follow_up.json()["error"]["code"] == "WRENCH_NOT_FOUND"

    def test_exact_decimal_parsing_is_reused_on_registration(self, client):
        # 会被 float64 洗白的临界值在登记入口同样必须整体拒绝
        resp = client.post(
            _cal_url("W-022"),
            content=(
                '{"target_nm": 100.00,'
                ' "measured_nm": [500.00000000000001, 500.00, 500.00, 500.00, 500.00]}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert client.get(_cal_url("W-022")).status_code == 404


class TestHistoryAndSnapshot:
    def test_history_follows_registration_order_with_sequential_seqs(self, client):
        for body in (PASS_BODY, FAIL_BODY, PASS_BODY):
            client.post(_cal_url("W-030"), json=body)
        profile = client.get(_cal_url("W-030")).json()
        assert [h["seq"] for h in profile["history"]] == [1, 2, 3]
        assert [h["overall"] for h in profile["history"]] == [
            "pass",
            "fail",
            "pass",
        ]
        for record in profile["history"]:
            _assert_verification_fields(record)
            assert record["registered_at"]

    def test_snapshot_matches_single_verify_endpoint_field_by_field(self, client):
        single = client.post(VERIFY_URL, json=FAIL_BODY).json()
        client.post(_cal_url("W-031"), json=PASS_BODY)
        registered = client.post(_cal_url("W-031"), json=FAIL_BODY).json()
        for key, value in single.items():
            assert registered[key] == value
        history = client.get(_cal_url("W-031")).json()["history"]
        for key, value in single.items():
            assert history[1][key] == value

    def test_extra_field_in_registration_body_rejected(self, client):
        resp = client.post(
            _cal_url("W-032"),
            json={**PASS_BODY, "operator": "alice"},
        )
        assert resp.status_code == 422
        assert client.get(_cal_url("W-032")).status_code == 404


class TestProfileIsolation:
    def test_unknown_sn_does_not_leak_other_profiles(self, client):
        client.post(_cal_url("W-040"), json=PASS_BODY)
        client.post(_cal_url("W-041"), json=FAIL_BODY)

        resp = client.get(_cal_url("W-042"))
        assert resp.status_code == 404
        text = resp.text
        assert "W-040" not in text
        assert "W-041" not in text

    def test_each_wrench_has_independent_state_and_sequence(self, client):
        client.post(_cal_url("W-040"), json=FAIL_BODY)
        client.post(_cal_url("W-041"), json=PASS_BODY)

        p40 = client.get(_cal_url("W-040")).json()
        p41 = client.get(_cal_url("W-041")).json()
        assert p40["status"] == STATUS_OBSERVATION
        assert p40["consecutive_fail_count"] == 1
        assert p40["history"][0]["seq"] == 1
        assert p41["status"] == STATUS_IN_SERVICE
        assert p41["consecutive_fail_count"] == 0


class TestWrenchIdValidation:
    @pytest.mark.parametrize("sn", ["", "   ", "%20%20"])
    def test_blank_sn_rejected(self, client, sn):
        resp = client.post(f"/api/v1/wrenches/{sn}/calibrations", json=PASS_BODY)
        assert resp.status_code in (404, 422)

    def test_overlong_sn_rejected(self, client):
        resp = client.post(f"/api/v1/wrenches/{'A' * 65}/calibrations", json=PASS_BODY)
        assert resp.status_code == 422

    def test_sn_with_spaces_normalized_for_lookup(self, client):
        resp = client.post(_cal_url("W-050"), json=PASS_BODY)
        assert resp.status_code == 200
        # 首尾空白在契约层 strip，落到同一档案
        resp = client.get("/api/v1/wrenches/%20W-050%20/calibrations")
        assert resp.status_code == 200
        assert resp.json()["wrench_sn"] == "W-050"


class TestPersistenceAcrossConnections:
    def test_state_survives_new_service_instance(self, client, tmp_path):
        db_path = str(tmp_path / "cal.db")
        client.post(_cal_url("W-060"), json=FAIL_BODY)
        client.post(_cal_url("W-060"), json=FAIL_BODY)

        # 全新仓库/服务实例指向同一文件：状态与完整历史仍在
        reopened = CalibrationService(CalibrationRepository(db_path))
        profile = reopened.get_profile("W-060")
        assert profile.status == STATUS_OUT_OF_SERVICE
        assert profile.consecutive_fail_count == 2
        assert len(profile.history) == 2
        assert profile.history[0].overall == "fail"


class TestConcurrentRegistration:
    def test_concurrent_passes_get_unique_sequential_seqs(self, client):
        results: list[dict] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def register() -> None:
            try:
                resp = client.post(_cal_url("W-070"), json=PASS_BODY)
                with lock:
                    results.append(resp.json())
            except Exception as exc:  # pragma: no cover - 仅用于暴露并发缺陷
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=register) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        seqs = sorted(r["seq"] for r in results)
        assert seqs == list(range(1, 9))
        profile = client.get(_cal_url("W-070")).json()
        assert profile["total_records"] == 8


class TestConcurrentReadConsistency:
    """边登记边查询时，单次查询响应必须始终自洽：

    status / consecutive_fail_count 必须与 history 末尾连续不合格数一致，
    不得出现“档案行仍旧（在用/0）而历史已含新不合格记录”的读偏斜。
    """

    def test_profile_row_and_history_come_from_one_snapshot(self, tmp_path):
        """确定性交错：读者读完档案行后挂起，登记提交后读者再读历史。"""

        import sqlite3

        db_path = str(tmp_path / "cal.db")
        writer = CalibrationService(CalibrationRepository(db_path))
        writer.register("W-080", _req(PASS_BODY))

        reached = threading.Event()
        release = threading.Event()

        class PausingRepository(CalibrationRepository):
            """档案行 SELECT 步进后挂起，强制登记提交夹在两次读语句之间。"""

            def __init__(self, path):
                self.armed = False
                super().__init__(path)

            def _connect(self):
                repo = self

                class _PausingConnection(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        cur = super().execute(sql, parameters)
                        if repo.armed and " FROM wrench_profiles " in sql:
                            # 该 SELECT 已步进、读快照已固定；此刻挂起，
                            # 让写者在历史 SELECT 之前完成提交。
                            repo.armed = False
                            reached.set()
                            release.wait(timeout=5)
                        return cur

                conn = sqlite3.connect(
                    self.db_path, timeout=30.0, factory=_PausingConnection
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 30000")
                return conn

        reader_repo = PausingRepository(db_path)
        observed: dict = {}

        def read() -> None:
            observed["result"] = reader_repo.get_profile_with_history("W-080")

        reader_repo.armed = True
        t = threading.Thread(target=read)
        t.start()
        assert reached.wait(5.0)

        # 读者已取得档案行快照；此刻另一连接写入并提交一次不合格登记
        writer.register("W-080", _req(FAIL_BODY))
        release.set()
        t.join(5.0)
        assert not t.is_alive()

        profile, records = observed["result"]
        # 两次读必须来自同一快照：读者取得的是登记前快照
        # （in_service / 0 / 仅 1 条合格历史），绝不允许旧状态配新历史。
        assert profile.status == STATUS_IN_SERVICE
        assert profile.consecutive_fail_count == 0
        assert len(records) == 1
        assert records[0].snapshot["overall"] == "pass"

    def test_concurrent_register_and_query_never_observed_torn(self, tmp_path):
        """压力版（DB 层并发）：持续登记时每次档案读取都必须内部自洽。"""

        db_path = str(tmp_path / "cal.db")
        writer = CalibrationService(CalibrationRepository(db_path))
        writer.register("W-081", _req(PASS_BODY))
        stop = threading.Event()
        violations: list[str] = []

        def expected_for(records) -> tuple[str, int]:
            trailing_fails = 0
            for record in reversed(records):
                if record.snapshot["overall"] == "fail":
                    trailing_fails += 1
                else:
                    break
            if trailing_fails == 0:
                return STATUS_IN_SERVICE, 0
            if trailing_fails == 1:
                return STATUS_OBSERVATION, 1
            return STATUS_OUT_OF_SERVICE, trailing_fails

        def query_loop() -> None:
            reader = CalibrationRepository(db_path)
            while not stop.is_set():
                found = reader.get_profile_with_history("W-081")
                assert found is not None
                profile, records = found
                exp_status, exp_count = expected_for(records)
                if (
                    profile.status != exp_status
                    or profile.consecutive_fail_count != exp_count
                ):
                    violations.append(
                        f"torn read: status={profile.status} "
                        f"count={profile.consecutive_fail_count} "
                        f"history={[r.snapshot['overall'] for r in records]}"
                    )

        # 登记序列 pass→fail→fail 循环，三种状态都会反复出现
        sequence = [PASS_BODY, FAIL_BODY, FAIL_BODY]

        def register_loop() -> None:
            for _ in range(40):
                for body in sequence:
                    writer.register("W-081", _req(body))

        readers = [threading.Thread(target=query_loop) for _ in range(4)]
        for r in readers:
            r.start()
        writer_thread = threading.Thread(target=register_loop)
        writer_thread.start()
        writer_thread.join()
        stop.set()
        for r in readers:
            r.join()

        assert violations == []


class TestTransitionPureFunction:
    @pytest.mark.parametrize(
        "prior_status,prior_count,passed,expected",
        [
            (None, 0, True, (STATUS_IN_SERVICE, 0)),
            (None, 0, False, (STATUS_OBSERVATION, 1)),
            (STATUS_IN_SERVICE, 0, False, (STATUS_OBSERVATION, 1)),
            (STATUS_OBSERVATION, 1, False, (STATUS_OUT_OF_SERVICE, 2)),
            (STATUS_OUT_OF_SERVICE, 2, False, (STATUS_OUT_OF_SERVICE, 3)),
            (STATUS_OUT_OF_SERVICE, 3, True, (STATUS_IN_SERVICE, 0)),
            (STATUS_OBSERVATION, 1, True, (STATUS_IN_SERVICE, 0)),
            (STATUS_IN_SERVICE, 0, True, (STATUS_IN_SERVICE, 0)),
        ],
    )
    def test_transition_table(
        self, prior_status, prior_count, passed, expected
    ):
        profile = (
            None
            if prior_status is None
            else ProfileRow(
                wrench_sn="x",
                status=prior_status,
                consecutive_fail_count=prior_count,
                created_at="t0",
            )
        )
        assert transition(profile, passed) == expected
