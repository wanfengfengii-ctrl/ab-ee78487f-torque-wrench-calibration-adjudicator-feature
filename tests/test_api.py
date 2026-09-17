"""HTTP API 测试：正常链路、结构化错误、临界稳定性、双项超限。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
URL = "/api/v1/torque/verify"


def post(payload):
    return client.post(URL, json=payload)


class TestHappyPath:
    def test_pass_response_structure(self):
        resp = post(
            {
                "target_nm": 100.00,
                "measured_nm": [100.10, 99.90, 100.00, 100.05, 99.95],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["target_nm"] == "100.00"
        assert body["measured_nm"] == ["100.10", "99.90", "100.00", "100.05", "99.95"]
        assert body["mean_nm"] == "100.00"
        assert body["deviation_pct"] == "0.00"
        assert body["deviation_limit_pct"] == "2.00"
        assert body["deviation_ok"] is True
        assert body["range_pct"] == "0.20"
        assert body["range_limit_pct"] == "3.00"
        assert body["range_ok"] is True
        assert body["overall"] == "pass"
        assert body["failure_reasons"] == []

    def test_string_numbers_accepted(self):
        resp = post(
            {"target_nm": "100.00", "measured_nm": ["100.00"] * 5}
        )
        assert resp.status_code == 200
        assert resp.json()["overall"] == "pass"

    def test_readings_echoed_in_canonical_two_decimals(self):
        resp = post(
            {"target_nm": 100, "measured_nm": [100.1, 100, "100.20", 99.9, 100.00]}
        )
        assert resp.status_code == 200
        assert resp.json()["measured_nm"] == [
            "100.10",
            "100.00",
            "100.20",
            "99.90",
            "100.00",
        ]

    def test_boundary_values_of_domain_accepted(self):
        resp = post({"target_nm": 1.00, "measured_nm": [1.00] * 5})
        assert resp.status_code == 200
        resp = post({"target_nm": 500.00, "measured_nm": [500.00] * 5})
        assert resp.status_code == 200


class TestBoundaryJudgement:
    def test_equal_to_both_limits_passes(self):
        # 偏差率恰为 2.00%、极差率恰为 3.00% → 合格
        resp = post(
            {
                "target_nm": 100.00,
                "measured_nm": [100.50, 103.50, 102.00, 102.00, 102.00],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["deviation_pct"] == "2.00"
        assert body["range_pct"] == "3.00"
        assert body["overall"] == "pass"

    def test_critical_sample_displays_rounded_but_fails(self):
        # 偏差率 2.004%：展示 2.00%，但未舍入比较 → 不合格
        resp = post(
            {
                "target_nm": 100.00,
                "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mean_nm"] == "102.00"  # 102.004 的展示值
        assert body["deviation_pct"] == "2.00"  # 2.004 的展示值
        assert body["deviation_ok"] is False
        assert body["overall"] == "fail"
        assert body["failure_reasons"] == ["deviation_pct_out_of_limit"]

    def test_same_critical_input_stable_evidence(self):
        payload = {
            "target_nm": 100.00,
            "measured_nm": [102.00, 102.00, 102.00, 102.00, 102.02],
        }
        bodies = {post(payload).text for _ in range(5)}
        assert len(bodies) == 1

    def test_dual_violation_exposes_both_reasons(self):
        resp = post(
            {
                "target_nm": 100.00,
                "measured_nm": [95.00, 110.00, 105.00, 105.00, 105.00],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["overall"] == "fail"
        assert body["deviation_ok"] is False
        assert body["range_ok"] is False
        assert body["deviation_pct"] == "4.00"
        assert body["range_pct"] == "15.00"
        assert body["failure_reasons"] == [
            "deviation_pct_out_of_limit",
            "range_pct_out_of_limit",
        ]


class TestStructuredRejection:
    def _assert_structured_error(self, resp):
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["message"]
        assert isinstance(body["error"]["details"], list)
        assert len(body["error"]["details"]) >= 1
        # 整体拒绝：响应中不得出现任何结果字段
        assert "mean_nm" not in body
        assert "overall" not in body

    @pytest.mark.parametrize("count", [0, 1, 4, 6, 10])
    def test_reading_count_must_be_exactly_five(self, count):
        resp = post({"target_nm": 100.00, "measured_nm": [100.00] * count})
        self._assert_structured_error(resp)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("target_nm", 0.99),
            ("target_nm", 500.01),
            ("target_nm", -10),
            ("target_nm", 100.001),
            ("target_nm", "abc"),
            ("target_nm", None),
        ],
    )
    def test_invalid_target_rejected(self, field, value):
        resp = post({field: value, "measured_nm": [100.00] * 5})
        self._assert_structured_error(resp)

    @pytest.mark.parametrize("bad", [0.99, 500.01, 100.001, "abc", None, True])
    def test_one_bad_reading_rejects_whole_request(self, bad):
        readings = [100.00, 100.00, bad, 100.00, 100.00]
        resp = post({"target_nm": 100.00, "measured_nm": readings})
        self._assert_structured_error(resp)

    def test_nan_rejected(self):
        # httpx 的 json= 不允许 NaN，直接发送原始 JSON 文本
        resp = client.post(
            URL,
            content='{"target_nm": 100.00, "measured_nm": [NaN, NaN, NaN, NaN, NaN]}',
            headers={"Content-Type": "application/json"},
        )
        self._assert_structured_error(resp)

    def test_infinity_rejected(self):
        resp = client.post(
            URL,
            content='{"target_nm": Infinity, "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}',
            headers={"Content-Type": "application/json"},
        )
        self._assert_structured_error(resp)

    def test_missing_field_rejected(self):
        resp = post({"target_nm": 100.00})
        self._assert_structured_error(resp)

    def test_extra_field_rejected(self):
        resp = post(
            {
                "target_nm": 100.00,
                "measured_nm": [100.00] * 5,
                "operator": "alice",
            }
        )
        self._assert_structured_error(resp)

    def test_error_details_pinpoint_location(self):
        resp = post({"target_nm": 100.00, "measured_nm": [100.00, 0.50, 100.00, 100.00, 100.00]})
        body = resp.json()
        locs = [tuple(d["loc"]) for d in body["error"]["details"]]
        assert ("body", "measured_nm", 1) in locs


class TestExactDecimalParsing:
    """临界数值必须按原始 JSON 文本精确（Decimal）解析，不得经 float64 归并。

    注意：这些用例必须发送原始 JSON 文本——Python 客户端的 float 与
    json.dumps 在发送前就会把超长数字归并掉，无法触达服务端缺陷。
    """

    def _post_raw(self, raw: str):
        return client.post(
            URL,
            content=raw,
            headers={"Content-Type": "application/json"},
        )

    def _assert_rejected_without_results(self, resp):
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]
        # 整体拒绝：响应中不得出现任何结果字段
        assert "mean_nm" not in body
        assert "overall" not in body

    def test_target_with_excess_decimal_places_rejected(self):
        # 100.000000000000001 经 float64 会被归并为 100，必须仍判非法
        resp = self._post_raw(
            '{"target_nm": 100.000000000000001,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}'
        )
        self._assert_rejected_without_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "target_nm") in locs

    def test_reading_just_below_lower_bound_rejected(self):
        # 0.99999999999999999 经 float64 会被归并为 1.0，必须仍判越界
        resp = self._post_raw(
            '{"target_nm": 1.00,'
            ' "measured_nm": [0.99999999999999999, 1.00, 1.00, 1.00, 1.00]}'
        )
        self._assert_rejected_without_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "measured_nm", 0) in locs

    def test_reading_just_above_upper_bound_rejected(self):
        # 500.00000000000001 经 float64 会被归并为 500.0，必须仍判越界
        resp = self._post_raw(
            '{"target_nm": 500.00,'
            ' "measured_nm": [500.00000000000001, 500.00, 500.00, 500.00, 500.00]}'
        )
        self._assert_rejected_without_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "measured_nm", 0) in locs

    def test_exact_boundaries_still_accepted(self):
        # 精确等于 1.00 / 500.00（含整数写法）仍须受理
        resp = self._post_raw(
            '{"target_nm": 1, "measured_nm": [1.00, 1, 1.0, 1.00, 1.00]}'
        )
        assert resp.status_code == 200
        assert resp.json()["overall"] == "pass"
        resp = self._post_raw(
            '{"target_nm": 500.00,'
            ' "measured_nm": [500.00, 500.00, 500.00, 500.00, 500.00]}'
        )
        assert resp.status_code == 200
        assert resp.json()["overall"] == "pass"

    def test_large_integer_outside_range_rejected_exactly(self):
        # 超出 float64 精确范围的大整数也必须精确判定为越界
        resp = self._post_raw(
            '{"target_nm": 100.00,'
            ' "measured_nm": [9999999999999999999999999,'
            ' 100.00, 100.00, 100.00, 100.00]}'
        )
        self._assert_rejected_without_results(resp)
        locs = [tuple(d["loc"]) for d in resp.json()["error"]["details"]]
        assert ("body", "measured_nm", 0) in locs

    def test_trailing_zero_beyond_two_decimals_rejected_like_string(self):
        # 100.000 数值上恰为 100，但文本有三位小数：JSON 数字与数值
        # 字符串输入应一致拒绝（旧实现仅因 float 归并而放过了数字写法）。
        resp_number = self._post_raw(
            '{"target_nm": 100.000,'
            ' "measured_nm": [100.00, 100.00, 100.00, 100.00, 100.00]}'
        )
        resp_string = post(
            {"target_nm": "100.000", "measured_nm": ["100.00"] * 5}
        )
        assert resp_number.status_code == resp_string.status_code == 422

    def test_exponent_notation_normalized_when_in_range(self):
        # 指数写法在文本上不超过两位小数时（1e2=100）仍正常受理
        resp = self._post_raw(
            '{"target_nm": 1e2,'
            ' "measured_nm": [1.00e2, 100, 1.000e2, 100.00, 1e2]}'
        )
        assert resp.status_code == 200
        assert resp.json()["overall"] == "pass"


class TestHealth:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
