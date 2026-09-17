"""计算核心测试：十进制定点、边界相等、未舍入比较、展示舍入。"""

from decimal import Decimal

import pytest

from app.calculator import (
    DEVIATION_LIMIT_PCT,
    RANGE_LIMIT_PCT,
    round_for_display,
    verify_torque,
)

D = Decimal


class TestMean:
    def test_mean_is_sum_divided_by_five(self):
        r = verify_torque(
            D("100.00"),
            [D("100.01"), D("100.02"), D("100.03"), D("100.04"), D("100.05")],
        )
        assert r.mean_nm == D("100.03")

    def test_mean_keeps_full_precision(self):
        # 510.02 / 5 = 102.004，内部不得预先舍入成 102.00
        r = verify_torque(D("100.00"), [D("102.00")] * 4 + [D("102.02")])
        assert r.mean_nm == D("102.004")


class TestDeviation:
    def test_signed_positive(self):
        r = verify_torque(D("100.00"), [D("102.00")] * 5)
        assert r.deviation_pct == D("2.00")

    def test_signed_negative(self):
        r = verify_torque(D("100.00"), [D("98.00")] * 5)
        assert r.deviation_pct == D("-2.00")

    def test_exact_positive_boundary_passes(self):
        r = verify_torque(D("100.00"), [D("102.00")] * 5)
        assert r.deviation_ok is True
        assert r.overall_ok is True

    def test_exact_negative_boundary_passes(self):
        r = verify_torque(D("100.00"), [D("98.00")] * 5)
        assert r.deviation_ok is True
        assert r.overall_ok is True

    def test_just_above_boundary_fails(self):
        r = verify_torque(D("100.00"), [D("102.01")] * 5)
        assert r.deviation_pct == D("2.01")
        assert r.deviation_ok is False

    def test_unrounded_comparison_at_critical_sample(self):
        # 平均值 102.004 → 偏差率 2.004%：展示为 2.00%，
        # 但内部比较用未舍入值，必须判不合格。
        r = verify_torque(D("100.00"), [D("102.00")] * 4 + [D("102.02")])
        assert r.deviation_pct == D("2.004")
        assert round_for_display(r.deviation_pct) == D("2.00")
        assert r.deviation_ok is False
        assert r.overall_ok is False

    def test_unrounded_comparison_negative_side(self):
        # 平均值 97.996 → 偏差率 -2.004%：展示 -2.00%，判不合格。
        r = verify_torque(
            D("100.00"), [D("97.99"), D("97.99"), D("97.99"), D("97.99"), D("98.02")]
        )
        assert r.mean_nm == D("97.996")
        assert r.deviation_pct == D("-2.004")
        assert round_for_display(r.deviation_pct) == D("-2.00")
        assert r.deviation_ok is False


class TestRange:
    def test_exact_boundary_three_passes(self):
        r = verify_torque(
            D("100.00"),
            [D("100.50"), D("103.50"), D("102.00"), D("102.00"), D("102.00")],
        )
        assert r.range_pct == D("3.00")
        assert r.range_ok is True
        assert r.overall_ok is True

    def test_just_above_boundary_fails(self):
        r = verify_torque(
            D("100.00"),
            [D("100.49"), D("103.50"), D("102.00"), D("102.00"), D("102.00")],
        )
        assert r.range_pct == D("3.01")
        assert r.range_ok is False

    def test_unrounded_range_comparison(self):
        # 7.51 / 250.00 × 100 = 3.004%：展示 3.00%，判不合格。
        r = verify_torque(
            D("250.00"),
            [D("250.00"), D("257.51"), D("251.00"), D("252.00"), D("253.00")],
        )
        assert r.range_pct == D("3.004")
        assert round_for_display(r.range_pct) == D("3.00")
        assert r.range_ok is False


class TestOverallAndReasons:
    def test_dual_violation_reports_both_reasons(self):
        r = verify_torque(
            D("100.00"),
            [D("95.00"), D("110.00"), D("105.00"), D("105.00"), D("105.00")],
        )
        assert r.deviation_pct == D("4.00")
        assert r.range_pct == D("15.00")
        assert r.deviation_ok is False
        assert r.range_ok is False
        assert r.overall_ok is False
        assert r.failure_reasons == [
            "deviation_pct_out_of_limit",
            "range_pct_out_of_limit",
        ]

    def test_pass_has_no_reasons(self):
        r = verify_torque(D("100.00"), [D("100.00")] * 5)
        assert r.overall_ok is True
        assert r.failure_reasons == []

    def test_limits_are_fixed_decimals(self):
        assert DEVIATION_LIMIT_PCT == D("2.00")
        assert RANGE_LIMIT_PCT == D("3.00")


class TestDeterminism:
    def test_same_input_same_result(self):
        readings = [D("102.00")] * 4 + [D("102.02")]
        first = verify_torque(D("100.00"), readings)
        second = verify_torque(D("100.00"), list(readings))
        assert first == second

    def test_no_float_reintroduction(self):
        # 0.1 + 0.2 类误差不得出现：全部走十进制
        r = verify_torque(
            D("300.00"),
            [D("300.10"), D("300.20"), D("300.30"), D("299.90"), D("299.50")],
        )
        assert r.mean_nm == D("300.00")
        assert r.deviation_pct == D("0.00")


class TestInputGuards:
    def test_wrong_reading_count_raises(self):
        with pytest.raises(ValueError, match="exactly 5"):
            verify_torque(D("100.00"), [D("100.00")] * 4)

    def test_non_positive_target_raises(self):
        with pytest.raises(ValueError, match="positive"):
            verify_torque(D("0.00"), [D("100.00")] * 5)


class TestDisplayRounding:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2.005", "2.01"),
            ("2.004", "2.00"),
            ("-2.005", "-2.01"),
            ("0.125", "0.13"),
            ("99.999", "100.00"),
            ("3.00", "3.00"),
            ("-0.001", "-0.00"),
        ],
    )
    def test_round_half_up(self, raw, expected):
        assert format(round_for_display(D(raw)), "f") == expected
