"""批量复核接口测试：编排汇总、逐项等价、整体拒绝、边界规模。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SINGLE_URL = "/api/v1/torque/verify"
BATCH_URL = "/api/v1/torque/verify/batch"

#: 常规合格测点
PASS_ITEM = {
    "target_nm": 100.00,
    "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95],
}
#: 临界合格测点：偏差率恰 2.00%、极差率恰 3.00%
BOUNDARY_ITEM = {
    "target_nm": 100.00,
    "measured_nm": [100.50, 103.50, 102.00, 102.00, 102.00],
}
#: 临界不合格测点：偏差率 2.004%，展示 2.00% 但判不合格
CRITICAL_ITEM = {
    "target_nm": 100.00,
    "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02],
}
#: 双项超限测点
DUAL_FAIL_ITEM = {
    "target_nm": 100.00,
    "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00],
}


def post_batch(payload):
    return client.post(BATCH_URL, json=payload)


def post_single(payload):
    return client.post(SINGLE_URL, json=payload)


class TestBatchHappyPath:
    def test_all_pass_batch(self):
        resp = post_batch({"items": [PASS_ITEM, BOUNDARY_ITEM]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["passed_count"] == 2
        assert body["failed_count"] == 0
        assert body["failed_indices"] == []
        assert body["overall"] == "pass"
        assert len(body["items"]) == 2
        assert all(item["overall"] == "pass" for item in body["items"])

    def test_results_follow_input_order(self):
        items = [
            {"target_nm": 200.00, "measured_nm": [200.00] * 5},
            {"target_nm": 50.00, "measured_nm": [50.00] * 5},
            {"target_nm": 300.00, "measured_nm": [300.00] * 5},
        ]
        resp = post_batch({"items": items})
        assert resp.status_code == 200
        echoes = [item["target_nm"] for item in resp.json()["items"]]
        assert echoes == ["200.00", "50.00", "300.00"]

    def test_min_batch_size_one_accepted(self):
        resp = post_batch({"items": [PASS_ITEM]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["overall"] == "pass"

    def test_max_batch_size_twenty_accepted(self):
        resp = post_batch({"items": [PASS_ITEM] * 20})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 20
        assert body["passed_count"] == 20
        assert body["overall"] == "pass"


class TestMixedBatch:
    def test_summary_and_failed_indices(self):
        resp = post_batch(
            {"items": [PASS_ITEM, CRITICAL_ITEM, BOUNDARY_ITEM, DUAL_FAIL_ITEM]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert body["passed_count"] == 2
        assert body["failed_count"] == 2
        assert body["failed_indices"] == [1, 3]
        assert body["overall"] == "fail"

    def test_all_items_computed_despite_failures(self):
        # 首个测点即不合格，后续测点仍须完整计算并保留全部字段
        resp = post_batch({"items": [DUAL_FAIL_ITEM, PASS_ITEM, CRITICAL_ITEM]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["failed_indices"] == [0, 2]
        assert len(body["items"]) == 3
        expected_keys = set(post_single(PASS_ITEM).json().keys())
        for item in body["items"]:
            assert set(item.keys()) == expected_keys
        assert body["items"][1]["overall"] == "pass"
        assert body["items"][2]["failure_reasons"] == ["deviation_pct_out_of_limit"]

    def test_all_fail_batch(self):
        resp = post_batch({"items": [CRITICAL_ITEM, DUAL_FAIL_ITEM]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed_count"] == 0
        assert body["failed_count"] == 2
        assert body["failed_indices"] == [0, 1]
        assert body["overall"] == "fail"


class TestItemByItemEquivalence:
    """批量项必须与逐个调用单次接口得到的字段和值完全一致。"""

    @pytest.mark.parametrize(
        "item",
        [PASS_ITEM, BOUNDARY_ITEM, CRITICAL_ITEM, DUAL_FAIL_ITEM],
        ids=["pass", "boundary", "critical", "dual-fail"],
    )
    def test_batch_item_equals_single_response(self, item):
        batch_body = post_batch({"items": [item]}).json()
        single_body = post_single(item).json()
        assert batch_body["items"][0] == single_body

    def test_mixed_batch_items_equal_single_responses(self):
        items = [PASS_ITEM, CRITICAL_ITEM, BOUNDARY_ITEM, DUAL_FAIL_ITEM]
        batch_body = post_batch({"items": items}).json()
        singles = [post_single(item).json() for item in items]
        assert batch_body["items"] == singles

    def test_boundary_values_equivalent_item_by_item(self):
        # 临界值逐项等价：恰等边界判合格、2.004% 临界判不合格，
        # 批量项的展示值与判定均与单次接口一致
        batch_body = post_batch({"items": [BOUNDARY_ITEM, CRITICAL_ITEM]}).json()
        boundary, critical = batch_body["items"]
        assert boundary["deviation_pct"] == "2.00"
        assert boundary["range_pct"] == "3.00"
        assert boundary["overall"] == "pass"
        assert critical["deviation_pct"] == "2.00"  # 2.004 的展示值
        assert critical["deviation_ok"] is False
        assert critical["overall"] == "fail"
        assert [boundary, critical] == [
            post_single(BOUNDARY_ITEM).json(),
            post_single(CRITICAL_ITEM).json(),
        ]


class TestBatchStructuredRejection:
    def _assert_structured_error(self, resp):
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["message"]
        assert isinstance(body["error"]["details"], list)
        assert len(body["error"]["details"]) >= 1
        # 整体拒绝：不得出现任何部分结果
        assert "items" not in body
        assert "overall" not in body
        assert "passed_count" not in body

    def test_empty_batch_rejected(self):
        self._assert_structured_error(post_batch({"items": []}))

    def test_oversized_batch_rejected(self):
        self._assert_structured_error(post_batch({"items": [PASS_ITEM] * 21}))

    def test_missing_items_field_rejected(self):
        self._assert_structured_error(post_batch({}))

    def test_extra_field_rejected(self):
        self._assert_structured_error(
            post_batch({"items": [PASS_ITEM], "operator": "alice"})
        )

    @pytest.mark.parametrize("bad", [0.99, 500.01, 100.001, "abc", None, True])
    def test_one_bad_reading_rejects_whole_batch(self, bad):
        bad_item = {
            "target_nm": 100.00,
            "measured_nm": [100.00, 100.00, bad, 100.00, 100.00],
        }
        resp = post_batch({"items": [PASS_ITEM, bad_item, BOUNDARY_ITEM]})
        self._assert_structured_error(resp)

    def test_invalid_target_in_any_item_rejected(self):
        bad_item = {"target_nm": 500.01, "measured_nm": [100.00] * 5}
        self._assert_structured_error(post_batch({"items": [PASS_ITEM, bad_item]}))

    def test_wrong_reading_count_in_item_rejected(self):
        bad_item = {"target_nm": 100.00, "measured_nm": [100.00] * 4}
        self._assert_structured_error(post_batch({"items": [bad_item]}))

    def test_extra_field_in_nested_item_rejected(self):
        bad_item = {**PASS_ITEM, "operator": "alice"}
        self._assert_structured_error(post_batch({"items": [bad_item]}))

    def test_error_details_pinpoint_item_and_reading(self):
        bad_item = {
            "target_nm": 100.00,
            "measured_nm": [100.00, 0.50, 100.00, 100.00, 100.00],
        }
        resp = post_batch({"items": [PASS_ITEM, bad_item]})
        body = resp.json()
        locs = [tuple(d["loc"]) for d in body["error"]["details"]]
        assert ("body", "items", 1, "measured_nm", 1) in locs


class TestBatchExactDecimalParsing:
    """临界数值经 float64 归并后不得“洗白”：任一测点出现超长小数或
    轻微越界读数，整批必须拒绝且不产生任何部分结果。

    必须发送原始 JSON 文本：Python float 与 json.dumps 会在客户端
    发送前就归并这些超长数字，无法触达缺陷。
    """

    def _post_raw(self, raw: str):
        return client.post(
            BATCH_URL,
            content=raw,
            headers={"Content-Type": "application/json"},
        )

    def _assert_rejected_without_partial_results(self, resp):
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]
        assert "items" not in body
        assert "overall" not in body
        assert "passed_count" not in body

    def test_item_target_with_excess_decimal_places_rejects_batch(self):
        resp = self._post_raw(
            '{"items": ['
            '{"target_nm": 100.00,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]},'
            '{"target_nm": 100.000000000000001,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}]}'
        )
        self._assert_rejected_without_partial_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "items", 1, "target_nm") in locs

    def test_item_reading_just_below_lower_bound_rejects_batch(self):
        resp = self._post_raw(
            '{"items": ['
            '{"target_nm": 1.00,'
            ' "measured_nm": [1.00, 1.00, 1.00, 1.00, 1.00]},'
            '{"target_nm": 1.00,'
            ' "measured_nm": [0.99999999999999999, 1.00, 1.00, 1.00, 1.00]}]}'
        )
        self._assert_rejected_without_partial_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "items", 1, "measured_nm", 0) in locs

    def test_item_reading_just_above_upper_bound_rejects_batch(self):
        resp = self._post_raw(
            '{"items": [{"target_nm": 500.00,'
            ' "measured_nm": [500.00000000000001, 500.00, 500.00, 500.00, 500.00]}]}'
        )
        self._assert_rejected_without_partial_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "items", 0, "measured_nm", 0) in locs

    def test_exact_boundary_items_still_accepted(self):
        resp = self._post_raw(
            '{"items": ['
            '{"target_nm": 1, "measured_nm": [1.00, 1, 1.0, 1.00, 1.00]},'
            '{"target_nm": 500.00,'
            ' "measured_nm": [500.00, 500.00, 500.00, 500.00, 500.00]}]}'
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["overall"] == "pass"
