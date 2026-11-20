"""区间与有理数运算单元测试。"""

import sys
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from housing_warehouse.rational import (
    Interval,
    area_of,
    fraction_text,
    intersect_intervals,
    merge_intervals,
    parse_fraction,
    subtract_intervals,
    sum_length,
    total_length,
)


class RationalCase(unittest.TestCase):
    def test_parse_and_text_roundtrip(self):
        self.assertEqual(parse_fraction("1/4"), Fraction(1, 4))
        self.assertEqual(parse_fraction("0"), Fraction(0))
        self.assertEqual(fraction_text(Fraction(2, 4)), "1/2")
        self.assertEqual(fraction_text(Fraction(1)), "1")

    def test_invalid_intervals_rejected(self):
        for pair in ((1, 0), (0, 0), (-1, 1), (0, 2), (1, 1)):
            with self.assertRaises(ValueError):
                Interval(Fraction(pair[0]), Fraction(pair[1]))

    def test_merge_and_union_detects_double_coverage(self):
        intervals = [
            Interval(Fraction(0), Fraction(1, 2)),
            Interval(Fraction(1, 4), Fraction(3, 4)),
        ]
        self.assertEqual(sum_length(intervals), Fraction(1))  # 直接求和：重叠被重复计入
        self.assertEqual(total_length(intervals), Fraction(3, 4))  # 并集只计一次
        merged = merge_intervals(intervals)
        self.assertEqual(merged, [Interval(Fraction(0), Fraction(3, 4))])

    def test_subtract_and_intersect(self):
        base = [Interval(Fraction(0), Fraction(1))]
        cut = [Interval(Fraction(1, 4), Fraction(1, 2))]
        self.assertEqual(
            subtract_intervals(base, cut),
            [Interval(Fraction(0), Fraction(1, 4)),
             Interval(Fraction(1, 2), Fraction(1))],
        )
        hit = intersect_intervals(
            [Interval(Fraction(0), Fraction(1, 2))],
            [Interval(Fraction(1, 4), Fraction(3, 4))],
        )
        self.assertEqual(hit, [Interval(Fraction(1, 4), Fraction(1, 2))])

    def test_area_is_exact_decimal(self):
        self.assertEqual(area_of("100.00", Fraction(1, 3)), Decimal("33.3333"))
        self.assertEqual(area_of("80.00", Fraction(1, 1)), Decimal("80.0000"))


if __name__ == "__main__":
    unittest.main()
