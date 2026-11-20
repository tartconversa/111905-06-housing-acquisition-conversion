"""按日阶段推导引擎（SCD-2 分段物化）。

状态只在来源事件日变化，因此不逐日展开，而是把每个稳定状态物化为一行半开日期
区间 ``[valid_from, valid_to)``（开放端为 ``None``）；按日查询时用区间谓词匹配。
这样后补验收或协议更正只改变有依据的期间，长脊线也不会产生海量重复行。

三种粒度各自分段，彼此不汇总成一个总数：

* ``home_state_period``：物理房屋粒度，市场待售/已交接/产权争议三段面积守恒；
* ``equity_period``：协议权益粒度，按有效期间表达权益面积（允许部分共有）；
* ``unit_supply_period``：运营单元粒度，区分转换面积、合格可出租面积、实际租赁
  占用与可出租空置，并单独暴露未满足资格的面积。

关键规则（与 docs/source-contract.md 对应）：

* 租约可提前签订，但只有交接覆盖 + 改造验收 + 安全验收 + 用途许可都满足时才计入
  可出租供给；正式占用与可出租空置分开计数；
* 产权争议范围保留占用事实与争议状态，不回到市场可售，也不算稳定租赁供给；
* 退租不恢复商品房待售，收购解除不结束已生效租约——引擎里这三条事实完全解耦。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Optional

from .model import (
    Issue,
    LeaseWindow,
    NormalizedFacts,
)
from .rational import Interval, intersect_intervals, interval_area, merge_intervals, subtract_intervals


@dataclass
class HomeStatePeriod:
    valid_from: date
    valid_to: Optional[date]
    home: str
    project: str
    state: str  # market_pending | handed_over | disputed
    interval: Interval
    area_sqm: Decimal


@dataclass
class UnitSupplyPeriod:
    valid_from: date
    valid_to: Optional[date]
    rental_unit: str
    converted_area_sqm: Decimal
    rentable_area_sqm: Decimal
    not_qualified_area_sqm: Decimal
    lease_occupied_area_sqm: Decimal
    rentable_vacant_area_sqm: Decimal
    disputed_area_sqm: Decimal


def date_spine(facts: NormalizedFacts, horizon: Optional[date] = None) -> tuple[date, date]:
    """生成日期脊线。

    大量事实（开放权益、持续交接、未退租租约）没有自然末端，脊线末端取显式
    ``horizon``；缺省为资料中最大事件日加 30 天，保证开放状态有可复核的查询窗口。
    """
    starts: list[date] = []
    ends: list[date] = []
    for period in facts.equity_periods:
        starts.append(period.valid_from)
        ends.append(period.valid_to or period.valid_from)
    for handover in facts.handovers:
        starts.append(handover.event_date)
    for component in facts.components:
        starts.append(component.effective)
    for qual in facts.qualifications:
        starts.append(qual.valid_from)
        ends.append(qual.valid_to or qual.valid_from)
    for lease in facts.leases:
        starts.append(lease.signed)
        ends.append(lease.occupancy_end)
    if not starts:
        today = horizon or date.today()
        return today, today
    latest = max(ends + starts)
    return min(starts), horizon or (latest + timedelta(days=30))


def _collapse_runs(change_days: list[date], snapshot_fn, row_fn):
    """通用分段器：在每个变化日取快照，相邻相同快照压缩成一个 [from, to) 期间。"""
    boundaries = sorted(set(change_days))
    rows = []
    previous_snapshot = None
    segment_start = boundaries[0]
    for index, day in enumerate(boundaries):
        snapshot = snapshot_fn(day)
        if snapshot != previous_snapshot:
            if previous_snapshot is not None:
                rows.append(row_fn(segment_start, day, previous_snapshot))
            segment_start = day
            previous_snapshot = snapshot
    if previous_snapshot is not None:
        rows.append(row_fn(segment_start, None, previous_snapshot))
    return rows


def build_home_state_periods(facts: NormalizedFacts,
                             start: date, end: date) -> list[HomeStatePeriod]:
    """房屋状态分段：交接/争议事件日为边界，三段覆盖在每个期间内守恒。"""
    full = Interval(Fraction(0), Fraction(1))
    events_by_home: dict[str, list[tuple[date, str, Interval]]] = {}
    for handover in facts.handovers:
        events_by_home.setdefault(handover.home, []).append(
            (handover.event_date, handover.kind, handover.interval)
        )
    periods: list[HomeStatePeriod] = []
    for home_id, home in facts.homes.items():
        events = sorted(events_by_home.get(home_id, []), key=lambda item: item[0])
        change_days = [start] + [event[0] for event in events]

        def snapshot(day: date, _events=events, _home_id=home_id):
            received: list[Interval] = []
            disputes: list[Interval] = []
            for event_date, kind, interval in _events:
                if event_date > day:
                    break
                if kind == "received":
                    received = merge_intervals([*received, interval])
                else:
                    disputes = merge_intervals([*disputes, interval])
            pieces: tuple[tuple[str, Interval], ...] = tuple(
                (state, piece)
                for state, intervals in (
                    ("market_pending", subtract_intervals([full], received)),
                    ("handed_over", subtract_intervals(received, disputes)),
                    ("disputed", disputes),
                )
                for piece in intervals
            )
            return pieces

        def make_row(valid_from, valid_to, snapshot_pieces, _home=home, _home_id=home_id):
            return [
                HomeStatePeriod(
                    valid_from, valid_to, _home_id, _home.project, state, piece,
                    interval_area(str(_home.area_sqm), piece),
                )
                for state, piece in snapshot_pieces
            ]

        rows = _collapse_runs(change_days, snapshot, make_row)
        periods.extend(item for batch in rows for item in batch)
    return periods


def _qualification_status(facts: NormalizedFacts) -> dict[str, list[tuple[date, date | None, str]]]:
    by_unit: dict[str, list[tuple[date, date | None, str]]] = {}
    for qual in facts.qualifications:
        by_unit.setdefault(qual.rental_unit, []).append(
            (qual.valid_from, qual.valid_to, qual.kind)
        )
    for items in by_unit.values():
        items.sort(key=lambda item: item[0])
    return by_unit


def _qualification_ok_cached(status, unit: str, day: date) -> bool:
    """安全验收与用途许可在当日均处于有效期内才合格；两者缺一不可。"""
    active_kinds: set[str] = set()
    for valid_from, valid_to, kind in status.get(unit, ()):
        if valid_from <= day and (valid_to is None or day <= valid_to):
            active_kinds.add(kind)
    return {"safety", "use_permit"}.issubset(active_kinds)


def _active_lease_count(leases_by_unit: dict[str, list[LeaseWindow]],
                        unit: str, day: date) -> int:
    return sum(
        1 for lease in leases_by_unit.get(unit, ())
        if lease.starts <= day < lease.occupancy_end
    )


def build_unit_supply_periods(facts: NormalizedFacts,
                              start: date, end: date) -> list[UnitSupplyPeriod]:
    """运营单元供应分段：交接、转换生效、资格有效期与租约占用边界共同切分。"""
    units = sorted({component.rental_unit for component in facts.components})
    components_by_unit: dict[str, list] = {}
    for component in facts.components:
        components_by_unit.setdefault(component.rental_unit, []).append(component)
    handover_events: dict[str, list[tuple[date, str, Interval]]] = {}
    for handover in facts.handovers:
        handover_events.setdefault(handover.home, []).append(
            (handover.event_date, handover.kind, handover.interval)
        )
    for items in handover_events.values():
        items.sort(key=lambda item: item[0])
    qual_status = _qualification_status(facts)
    leases_by_unit: dict[str, list[LeaseWindow]] = {}
    for lease in facts.leases:
        leases_by_unit.setdefault(lease.rental_unit, []).append(lease)

    periods: list[UnitSupplyPeriod] = []
    for unit in units:
        components = sorted(components_by_unit[unit], key=lambda item: item.effective)
        homes_in_unit = sorted({component.home for component in components})
        change_days = {start}
        for component in components:
            change_days.add(component.effective)
        for home_id in homes_in_unit:
            for event_date, _kind, _interval in handover_events.get(home_id, ()):
                change_days.add(event_date)
        for valid_from, valid_to, _kind in qual_status.get(unit, ()):
            change_days.add(valid_from)
            if valid_to is not None:
                change_days.add(valid_to + timedelta(days=1))
        for lease in leases_by_unit.get(unit, ()):
            change_days.add(lease.starts)
            change_days.add(lease.occupancy_end)

        def snapshot(day: date, _unit=unit, _components=components,
                     _homes=homes_in_unit):
            received: dict[str, list[Interval]] = {}
            disputes: dict[str, list[Interval]] = {}
            for home_id in _homes:
                rec: list[Interval] = []
                dis: list[Interval] = []
                for event_date, kind, interval in handover_events.get(home_id, ()):
                    if event_date > day:
                        break
                    if kind == "received":
                        rec = merge_intervals([*rec, interval])
                    else:
                        dis = merge_intervals([*dis, interval])
                received[home_id] = rec
                disputes[home_id] = dis
            converted = Decimal("0")
            eligible_area = Decimal("0")
            not_qualified = Decimal("0")
            disputed_area = Decimal("0")
            qualified = _qualification_ok_cached(qual_status, _unit, day)
            for component in _components:
                if component.effective > day:
                    continue
                converted += component.area_sqm
                home = facts.homes[component.home]
                covered = intersect_intervals([component.interval], received[component.home])
                if not covered:
                    # 改造映射存在但物理交接尚未覆盖：不形成供应，计入未合格面积
                    not_qualified += component.area_sqm
                    continue
                disputed_pieces = intersect_intervals(covered, disputes[component.home])
                clean_pieces = subtract_intervals(covered, disputes[component.home])
                disputed_area += sum(
                    (interval_area(str(home.area_sqm), piece) for piece in disputed_pieces),
                    Decimal("0"),
                )
                clean_area = sum(
                    (interval_area(str(home.area_sqm), piece) for piece in clean_pieces),
                    Decimal("0"),
                )
                if qualified:
                    eligible_area += clean_area
                else:
                    not_qualified += clean_area
            # 占用只在合格可出租面积内确认；重叠租约已在规范化阶段记异常，
            # 即使存在多份重叠租约，占用面积上限仍为可出租面积本身，不重复计数。
            occupied = bool(_active_lease_count(leases_by_unit, _unit, day))
            lease_occupied = eligible_area if occupied else Decimal("0")
            return (
                converted, eligible_area, not_qualified, disputed_area, lease_occupied,
            )

        def make_row(valid_from, valid_to, snap, _unit=unit):
            converted, eligible_area, not_qualified, disputed_area, lease_occupied = snap
            return UnitSupplyPeriod(
                valid_from, valid_to, _unit, converted, eligible_area, not_qualified,
                lease_occupied, eligible_area - lease_occupied, disputed_area,
            )

        periods.extend(_collapse_runs(sorted(change_days), snapshot, make_row))
    return periods


def detect_premature_supply(facts: NormalizedFacts,
                            supply_periods: list[UnitSupplyPeriod]) -> list[Issue]:
    """提前供给：租约已进入占用期，但当日没有任何合格可出租面积承接。"""
    issues: list[Issue] = []
    for lease in facts.leases:
        day = lease.starts
        matching = [
            period for period in supply_periods
            if period.rental_unit == lease.rental_unit
            and period.valid_from <= day
            and (period.valid_to is None or day < period.valid_to)
        ]
        if not matching or matching[0].rentable_area_sqm == 0:
            detail = ("租约起租时运营单元尚无合格可出租面积（交接覆盖、改造验收、"
                      "安全验收或用途许可未同时满足），提前形成占用主张")
            issues.append(Issue(
                "anomaly", "PREMATURE_SUPPLY", "leases", lease.lease_id, "unit",
                lease.rental_unit, None, lease.rental_unit, None, detail, lease.starts,
            ))
    return issues
