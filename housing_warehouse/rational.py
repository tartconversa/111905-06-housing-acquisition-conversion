"""精确有理数与覆盖区间运算。

源资料中的 coverage 是原房屋归一化空间上的 ``[from, to)`` 有理区间（约定为
``[0,1)`` 内），区间不表示面积数值，只标识原房屋的空间范围；不同原房屋的区间
彼此独立，永远不能跨房屋合并或比较。

所有区间运算使用 :class:`fractions.Fraction` 精确完成，面积换算时才落到
``Decimal``，从根上避免浮点导致的覆盖重复或丢失。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Iterable, Sequence

AREA_QUANT = Decimal("0.0001")


def parse_fraction(value: str) -> Fraction:
    """解析 ``"a/b"`` 或整数形式的有理数字符串。"""
    text = str(value).strip()
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(int(text))


def fraction_text(value: Fraction) -> str:
    """分数回写为资料风格的 ``"a/b"`` 文本（整数省略分母）。"""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


@dataclass(frozen=True)
class Interval:
    """归一化房屋空间上的 ``[start, end)`` 有理区间。"""

    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        if not (Fraction(0) <= self.start < self.end <= Fraction(1)):
            raise ValueError(f"非法覆盖区间: [{self.start}, {self.end})")

    @classmethod
    def parse(cls, pair: Sequence[str]) -> "Interval":
        if len(pair) != 2:
            raise ValueError(f"覆盖区间必须是 [from, to) 两个端点: {pair!r}")
        return cls(parse_fraction(pair[0]), parse_fraction(pair[1]))

    @property
    def length(self) -> Fraction:
        return self.end - self.start

    @property
    def text(self) -> tuple[str, str]:
        return fraction_text(self.start), fraction_text(self.end)

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> "Interval | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start < end:
            return Interval(start, end)
        return None

    def contains(self, other: "Interval") -> bool:
        return self.start <= other.start and other.end <= self.end


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """合并同房屋内彼此相接或相交的区间，返回不相交且有序的区间列表。"""
    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    merged: list[Interval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, interval.end)
        else:
            merged.append(interval)
    return merged


def total_length(intervals: Iterable[Interval]) -> Fraction:
    """区间集合的并集长度（重复覆盖只计一次），用于重计检测与面积守恒。"""
    return sum((item.length for item in merge_intervals(intervals)), Fraction(0))


def sum_length(intervals: Iterable[Interval]) -> Fraction:
    """区间长度的直接求和（重复覆盖会被重复计入），与并集长度对照识别重计。"""
    return sum((item.length for item in intervals), Fraction(0))


def area_of(home_area_sqm: str, length: Fraction) -> Decimal:
    """按覆盖长度把房屋总面积折算为面积（平方米，四位小数）。"""
    area = Decimal(home_area_sqm) * Decimal(length.numerator) / Decimal(length.denominator)
    return area.quantize(AREA_QUANT, rounding=ROUND_HALF_UP)


def interval_area(home_area_sqm: str, interval: Interval) -> Decimal:
    return area_of(home_area_sqm, interval.length)


def subtract_intervals(base: Sequence[Interval], cut: Sequence[Interval]) -> list[Interval]:
    """从 ``base`` 并集中挖去 ``cut`` 并集，返回剩余的不相交区间。"""
    cuts = merge_intervals(cut)
    remaining: list[Interval] = []
    for piece in merge_intervals(base):
        cursor = piece.start
        for hole in cuts:
            if hole.end <= cursor:
                continue
            if hole.start >= piece.end:
                break
            if hole.start > cursor:
                remaining.append(Interval(cursor, min(hole.start, piece.end)))
            cursor = max(cursor, hole.end)
            if cursor >= piece.end:
                break
        if cursor < piece.end:
            remaining.append(Interval(cursor, piece.end))
    return remaining


def intersect_intervals(left: Sequence[Interval], right: Sequence[Interval]) -> list[Interval]:
    """两个区间集合的交集（成对求交后合并）。"""
    pieces: list[Interval] = []
    for first in left:
        for second in right:
            hit = first.intersection(second)
            if hit is not None:
                pieces.append(hit)
    return merge_intervals(pieces)
