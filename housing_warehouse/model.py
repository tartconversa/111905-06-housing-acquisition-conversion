"""规范化事实与有效期间推导。

接入后的原始对象在这里被整理为带精确覆盖区间的事实，并推导出各自的有效期间：

* 权益期间：收购生效区间减去对应协议被解除的部分，解除只撤销其尚有效的权益；
* 交接事实：物理交接与产权争议分开保留，争议不改变已交接事实；
* 运营单元组成：同一原房屋覆盖不可重复，重复部分记异常且不重复计面积；
* 资格窗口与租约窗口：按各自有效期/占用期表达，开放端为 ``None``。

资料冲突不静默纠正：结构性无法入表的进 ``pending_review``，能入表但违反业务
约束的进 ``anomaly``，来源事实本身始终保留在原始层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from .ingest import ParsedSource, parse_coverage
from .rational import (
    Interval,
    fraction_text,
    intersect_intervals,
    interval_area,
    merge_intervals,
    subtract_intervals,
)


def _d(value: str) -> date:
    return date.fromisoformat(value)


@dataclass
class Issue:
    scope: str  # anomaly | pending
    code: str
    dataset: str
    record_id: str
    grain: str
    key: str
    home: Optional[str]
    unit: Optional[str]
    interval_text: Optional[str]
    detail: str
    observed_date: Optional[date]


@dataclass
class Home:
    home: str
    area_sqm: Decimal
    project: str


@dataclass
class EquityPeriod:
    agreement: str
    package: str
    home: str
    interval: Interval
    valid_from: date
    valid_to: Optional[date]  # None 表示开放


@dataclass
class HandoverFact:
    handover_id: str
    home: str
    interval: Interval
    event_date: date
    kind: str  # received | physical_dispute


@dataclass
class UnitComponent:
    rental_unit: str
    home: str
    interval: Interval
    area_sqm: Decimal
    effective: date


@dataclass
class QualWindow:
    proof_id: str
    kind: str
    rental_unit: str
    valid_from: date
    valid_to: Optional[date]
    received: date


@dataclass
class LeaseWindow:
    lease_id: str
    rental_unit: str
    signed: date
    starts: date
    ends: date
    returned: Optional[date]

    @property
    def occupancy_end(self) -> date:
        """实际占用期末端：有退租回执按退租日，否则按合同到期日。"""
        return self.returned or self.ends


@dataclass
class PaymentFact:
    payment_id: str
    agreement: str
    amount_cents: int
    event_date: date


@dataclass
class NormalizedFacts:
    homes: dict[str, Home]
    equity_periods: list[EquityPeriod]
    handovers: list[HandoverFact]
    components: list[UnitComponent]
    qualifications: list[QualWindow]
    leases: list[LeaseWindow]
    payments: list[PaymentFact]
    issues: list[Issue] = field(default_factory=list)

    def anomalies(self) -> list[Issue]:
        return [item for item in self.issues if item.scope == "anomaly"]

    def pending(self) -> list[Issue]:
        return [item for item in self.issues if item.scope == "pending"]


def _interval_text(intervals: list[Interval]) -> str:
    return ";".join(f"[{fraction_text(i.start)},{fraction_text(i.end)})" for i in intervals)


def normalize(source: ParsedSource) -> NormalizedFacts:
    issues: list[Issue] = []
    homes = _homes(source, issues)
    payments = _payments(source)
    equity = _equity(source, homes, issues)
    handovers = _handovers(source, homes, issues)
    components = _conversion(source, homes, issues)
    known_units = {item.rental_unit for item in components}
    qualifications = _qualifications(source, known_units, issues)
    leases = _leases(source, known_units, issues)
    return NormalizedFacts(
        homes=homes,
        equity_periods=equity,
        handovers=handovers,
        components=components,
        qualifications=qualifications,
        leases=leases,
        payments=payments,
        issues=issues,
    )


def _homes(source: ParsedSource, issues: list[Issue]) -> dict[str, Home]:
    homes: dict[str, Home] = {}
    for row in source.rows["homes"]:
        home_id = row["home"]
        area = Decimal(row["area_sqm"])
        if area <= 0:
            issues.append(
                Issue("pending", "BAD_AREA", "homes", home_id, "home", home_id, home_id, None,
                      None, "房屋面积必须为正数", None)
            )
            continue
        if home_id in homes:
            issues.append(
                Issue("anomaly", "DUPLICATE_HOME", "homes", home_id, "home", home_id, home_id,
                      None, None, "同一房屋出现多条面积记录", None)
            )
            continue
        homes[home_id] = Home(home_id, area, row["project"])
    return homes


def _payments(source: ParsedSource) -> list[PaymentFact]:
    return [
        PaymentFact(row["payment_id"], row["agreement"], int(row["amount_cents"]), _d(row["date"]))
        for row in source.rows["payments"]
    ]


def _equity(source: ParsedSource, homes: dict[str, Home], issues: list[Issue]) -> list[EquityPeriod]:
    # events[(target_agreement, home)] = [(date, kind, intervals, record_id, package, reference)]
    # terminate 记录自身有独立编号，但作用于 reference 指向的协议，必须归入该协议的事件流
    events: dict[tuple[str, str], list[tuple[date, str, list[Interval], str, str, Any]]] = {}
    agreement_package: dict[str, str] = {}
    for row in source.rows["acquisitions"]:
        record_id = row["agreement"]
        home_id = row["home"]
        if home_id not in homes:
            issues.append(Issue("pending", "UNKNOWN_HOME", "acquisitions", record_id, "home",
                                home_id, home_id, None, _interval_text(parse_coverage(row["coverage"])),
                                "协议引用了不存在的房屋，权益不参与推导", _d(row["effective"])))
            continue
        intervals = parse_coverage(row["coverage"], dataset="acquisitions", record_id=record_id)
        kind = row["kind"]
        if kind == "acquire":
            package = row["package"]
            if record_id in agreement_package and agreement_package[record_id] != package:
                issues.append(Issue("anomaly", "PACKAGE_CONFLICT", "acquisitions", record_id,
                                    "agreement", record_id, home_id, None, _interval_text(intervals),
                                    f"同一协议分属不同收购包：{agreement_package[record_id]} / {package}",
                                    _d(row["effective"])))
            agreement_package.setdefault(record_id, package)
            target_agreement = record_id
        elif kind == "terminate":
            referenced = row.get("reference")
            if not referenced:
                issues.append(Issue("pending", "MISSING_TERMINATION_REFERENCE", "acquisitions",
                                    record_id, "agreement", record_id, home_id, None,
                                    _interval_text(intervals),
                                    "解除记录缺少 reference，无法定位被解除的协议",
                                    _d(row["effective"])))
                continue
            target_agreement = str(referenced)
        else:
            issues.append(Issue("pending", "UNKNOWN_KIND", "acquisitions", record_id,
                                "agreement", record_id, home_id, None, _interval_text(intervals),
                                f"未知 acquisitions.kind: {kind}", _d(row["effective"])))
            continue
        events.setdefault((target_agreement, home_id), []).append(
            (_d(row["effective"]), kind, merge_intervals(intervals), record_id,
             row["package"], row.get("reference"))
        )

    periods: list[EquityPeriod] = []
    # 先做协议内生效/解除扫描
    for (agreement, home_id), entries in sorted(events.items()):
        package = agreement_package.get(agreement, entries[0][4])
        entries.sort(key=lambda item: item[0])
        # 同日事件先聚合，再按事件日之间的稳定区间生成期间
        day_events: dict[date, list[tuple[str, list[Interval], str]]] = {}
        for event_date, kind, intervals, record_id, _pkg, _ref in entries:
            day_events.setdefault(event_date, []).append((kind, intervals, record_id))
        event_days = sorted(day_events)
        # 第一阶段：逐日应用事件，保存每个事件日收市后的权益快照
        snapshots: list[tuple[date, list[Interval]]] = []
        active: list[Interval] = []
        for event_date in event_days:
            for kind, intervals, record_id in day_events[event_date]:
                if kind == "acquire":
                    overlap = intersect_intervals(active, intervals)
                    if overlap:
                        issues.append(Issue("anomaly", "EQUITY_OVERLAP", "acquisitions", record_id,
                                            "agreement", agreement, home_id, None,
                                            _interval_text(overlap),
                                            "同一协议在生效范围内重复声明权益（重计面积）",
                                            event_date))
                    active = merge_intervals([*active, *intervals])
                elif kind == "terminate":
                    excess = subtract_intervals(intervals, active)
                    if excess:
                        issues.append(Issue("anomaly", "TERMINATION_EXCESS", "acquisitions",
                                            record_id, "agreement", agreement, home_id, None,
                                            _interval_text(excess),
                                            f"解除范围超出协议 {_ref or agreement} 尚有效的权益",
                                            event_date))
                    active = subtract_intervals(active, intervals)
                else:
                    issues.append(Issue("pending", "UNKNOWN_KIND", "acquisitions", record_id,
                                        "agreement", agreement, home_id, None,
                                        _interval_text(intervals),
                                        f"未知 acquisitions.kind: {kind}", event_date))
            snapshots.append((event_date, list(active)))
        # 第二阶段：相邻事件日之间状态稳定，按快照生成 [d_i, d_{i+1}) 期间，末段开放
        for index, (valid_from, state) in enumerate(snapshots):
            valid_to = event_days[index + 1] if index + 1 < len(event_days) else None
            for interval in state:
                periods.append(EquityPeriod(
                    agreement, package, home_id, interval, valid_from, valid_to))
    _check_cross_agreement_overlap(periods, issues)
    return periods


def _check_cross_agreement_overlap(periods: list[EquityPeriod], issues: list[Issue]) -> None:
    by_home: dict[str, list[EquityPeriod]] = {}
    for period in periods:
        by_home.setdefault(period.home, []).append(period)
    for home_id, items in by_home.items():
        for idx, left in enumerate(items):
            for right in items[idx + 1:]:
                if left.agreement == right.agreement:
                    continue
                if left.valid_to is not None and right.valid_from >= left.valid_to:
                    continue
                if right.valid_to is not None and left.valid_from >= right.valid_to:
                    continue
                hit = left.interval.intersection(right.interval)
                if hit is not None:
                    issues.append(Issue(
                        "anomaly", "EQUITY_OVERLAP", "acquisitions",
                        f"{left.agreement}|{right.agreement}", "home", home_id, home_id, None,
                        _interval_text([hit]),
                        f"协议 {left.agreement} 与 {right.agreement} 在同一房屋空间上权益重叠",
                        max(left.valid_from, right.valid_from),
                    ))


def _handovers(source: ParsedSource, homes: dict[str, Home], issues: list[Issue]) -> list[HandoverFact]:
    facts: list[HandoverFact] = []
    received_union: dict[str, list[Interval]] = {}
    dispute_union: dict[str, list[Interval]] = {}
    for row in source.rows["handovers"]:
        record_id = row["handover_id"]
        home_id = row["home"]
        if home_id not in homes:
            issues.append(Issue("pending", "UNKNOWN_HOME", "handovers", record_id, "home",
                                home_id, home_id, None,
                                _interval_text(parse_coverage(row["coverage"])),
                                "交接记录引用了不存在的房屋", _d(row["date"])))
            continue
        intervals = parse_coverage(row["coverage"], dataset="handovers", record_id=record_id)
        event_date = _d(row["date"])
        kind = row["kind"]
        if kind not in {"received", "physical_dispute"}:
            issues.append(Issue("pending", "UNKNOWN_KIND", "handovers", record_id, "home",
                                home_id, home_id, None, _interval_text(intervals),
                                f"未知 handovers.kind: {kind}", event_date))
            continue
        for interval in intervals:
            facts.append(HandoverFact(record_id, home_id, interval, event_date, kind))
        if kind == "received":
            overlap = intersect_intervals(intervals, received_union.get(home_id, []))
            if overlap:
                issues.append(Issue("anomaly", "HANDOVER_OVERLAP", "handovers", record_id,
                                    "home", home_id, home_id, None, _interval_text(overlap),
                                    "物理交接覆盖重复（重计面积），按并集只计一次", event_date))
            received_union[home_id] = merge_intervals([*received_union.get(home_id, []), *intervals])
        else:
            outside = subtract_intervals(intervals, received_union.get(home_id, []))
            if outside:
                issues.append(Issue("pending", "DISPUTE_OUTSIDE_HANDOVER", "handovers", record_id,
                                    "home", home_id, home_id, None, _interval_text(outside),
                                    "产权争议范围没有对应的物理交接记录，保留来源事实待核",
                                    event_date))
            dispute_union[home_id] = merge_intervals([*dispute_union.get(home_id, []), *intervals])
    return facts


def _conversion(source: ParsedSource, homes: dict[str, Home], issues: list[Issue]) -> list[UnitComponent]:
    components: list[UnitComponent] = []
    claimed: dict[str, list[Interval]] = {}
    for row in source.rows["conversion"]:
        record_id = row["conversion_id"]
        home_id = row["home"]
        if home_id not in homes:
            issues.append(Issue("pending", "UNKNOWN_HOME", "conversion", record_id, "home",
                                home_id, home_id, row["rental_unit"],
                                _interval_text(parse_coverage(row["coverage"])),
                                "转换记录引用了不存在的房屋，身份桥接缺少面积覆盖，进入待核",
                                _d(row["effective"])))
            continue
        intervals = parse_coverage(row["coverage"], dataset="conversion", record_id=record_id)
        unit = row["rental_unit"]
        effective = _d(row["effective"])
        overlap = intersect_intervals(intervals, claimed.get(home_id, []))
        if overlap:
            issues.append(Issue("anomaly", "CONVERSION_OVERLAP", "conversion", record_id,
                                "home", home_id, home_id, unit, _interval_text(overlap),
                                "同一原房屋覆盖被重复映射到运营单元（重计面积），重复部分不计入供应",
                                effective))
        unique = subtract_intervals(intervals, claimed.get(home_id, []))
        claimed[home_id] = merge_intervals([*claimed.get(home_id, []), *intervals])
        for interval in unique:
            components.append(UnitComponent(
                rental_unit=unit,
                home=home_id,
                interval=interval,
                area_sqm=interval_area(str(homes[home_id].area_sqm), interval),
                effective=effective,
            ))
    return components


def _qualifications(source: ParsedSource, known_units: set[str],
                    issues: list[Issue]) -> list[QualWindow]:
    windows: list[QualWindow] = []
    for row in source.rows["qualifications"]:
        record_id = row["proof_id"]
        unit = row["scope"]
        valid_from = _d(row["valid_from"])
        valid_to = _d(row["valid_to"]) if row["valid_to"] else None
        received = _d(row["received"])
        if valid_to is not None and valid_from > valid_to:
            issues.append(Issue("pending", "BAD_DATE_ORDER", "qualifications", record_id,
                                "unit", unit, None, unit, None,
                                "资格有效期早于起始日", valid_from))
            continue
        if unit not in known_units:
            issues.append(Issue("pending", "BRIDGE_NO_COVERAGE", "qualifications", record_id,
                                "unit", unit, None, unit, None,
                                "资格指向的运营单元没有任何面积覆盖，身份桥接待核", received))
            continue
        windows.append(QualWindow(record_id, row["kind"], unit, valid_from, valid_to, received))
    return windows


def _leases(source: ParsedSource, known_units: set[str], issues: list[Issue]) -> list[LeaseWindow]:
    leases: list[LeaseWindow] = []
    for row in source.rows["leases"]:
        record_id = row["lease_id"]
        unit = row["unit"]
        signed = _d(row["signed"])
        starts = _d(row["starts"])
        ends = _d(row["ends"])
        returned = _d(row["returned"]) if row["returned"] else None
        if not (signed <= starts <= ends) or (returned is not None and returned < starts):
            issues.append(Issue("pending", "BAD_DATE_ORDER", "leases", record_id, "unit", unit,
                                None, unit, None, "租约签署/起租/到期/退租日期顺序不合约定", signed))
            continue
        if unit not in known_units:
            issues.append(Issue("pending", "BRIDGE_NO_COVERAGE", "leases", record_id, "unit",
                                unit, None, unit, None,
                                "租约指向的运营单元没有任何面积覆盖，身份桥接待核", signed))
            continue
        leases.append(LeaseWindow(record_id, unit, signed, starts, ends, returned))
    _check_lease_overlap(leases, issues)
    return leases


def _check_lease_overlap(leases: list[LeaseWindow], issues: list[Issue]) -> None:
    by_unit: dict[str, list[LeaseWindow]] = {}
    for lease in leases:
        by_unit.setdefault(lease.rental_unit, []).append(lease)
    for unit, items in by_unit.items():
        for idx, left in enumerate(items):
            for right in items[idx + 1:]:
                if left.starts < right.occupancy_end and right.starts < left.occupancy_end:
                    issues.append(Issue("anomaly", "LEASE_OVERLAP", "leases",
                                        f"{left.lease_id}|{right.lease_id}", "unit", unit, None,
                                        unit, None, "同一运营单元存在占用期重叠的租约",
                                        max(left.starts, right.starts)))
