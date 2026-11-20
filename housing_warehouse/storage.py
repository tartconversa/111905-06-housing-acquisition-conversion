"""DuckDB 本地分析存储与不可变版本管理。

每次入仓生成一个内容寻址的新版本目录，旧版本永不覆盖或删除，报告引用过的版本
随时可以重新打开复核。版本内同时保留三层数据：

1. ``raw_records``：来源对象原样落盘（JSON 文本 + 字节哈希），任何结论可回溯到资料；
2. 规范化事实表：权益期间、交接事实、单元组成、资格窗口、租约窗口、付款；
3. 期间事实表：三粒度各自的 SCD-2 分段面积，以及异常与待核清单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import duckdb

from .engine import (
    HomeStatePeriod,
    UnitSupplyPeriod,
)
from .ingest import DATASETS, ParsedSource
from .model import (
    Issue,
    NormalizedFacts,
)
from .rational import fraction_text

SCHEMA_STATEMENTS = [
    # ---- 元数据 ----
    """create table warehouse_meta(
        version_id varchar primary key,
        content_fingerprint varchar,
        built_at varchar,
        source_directory varchar,
        spine_start date,
        spine_end date,
        note varchar
    )""",
    """create table source_files(
        dataset varchar primary key,
        file_name varchar,
        sha256 varchar,
        row_count integer
    )""",
    # ---- 原始层：来源对象原样保留 ----
    """create table raw_records(
        dataset varchar,
        row_index integer,
        record_id varchar,
        payload_json varchar,
        primary key (dataset, row_index)
    )""",
    # ---- 房屋 ----
    """create table home(
        home varchar primary key,
        project varchar,
        area_sqm decimal(18, 4)
    )""",
    # ---- 协议权益期间（收购包/协议粒度）----
    """create table equity_period(
        package varchar,
        agreement varchar,
        home varchar,
        cov_start varchar,
        cov_end varchar,
        valid_from date,
        valid_to date,
        equity_area_sqm decimal(18, 4)
    )""",
    # ---- 物理交接事实（房屋粒度）----
    """create table handover_fact(
        handover_id varchar,
        home varchar,
        kind varchar,
        cov_start varchar,
        cov_end varchar,
        event_date date,
        area_sqm decimal(18, 4)
    )""",
    # ---- 运营单元组成 ----
    """create table unit_component(
        rental_unit varchar,
        home varchar,
        cov_start varchar,
        cov_end varchar,
        area_sqm decimal(18, 4),
        effective date
    )""",
    # ---- 资格窗口 ----
    """create table qual_window(
        proof_id varchar,
        kind varchar,
        rental_unit varchar,
        valid_from date,
        valid_to date,
        received date
    )""",
    # ---- 租约窗口 ----
    """create table lease_window(
        lease_id varchar,
        rental_unit varchar,
        signed date,
        starts date,
        ends date,
        returned date,
        occupancy_end date
    )""",
    # ---- 付款事实 ----
    """create table payment_fact(
        payment_id varchar,
        agreement varchar,
        amount_cents bigint,
        event_date date
    )""",
    # ---- 按期间：物理房屋状态（SCD-2 半开区间，valid_to 为空表示开放）----
    """create table home_state_period(
        valid_from date,
        valid_to date,
        home varchar,
        project varchar,
        state varchar,
        cov_start varchar,
        cov_end varchar,
        area_sqm decimal(18, 4)
    )""",
    # ---- 按期间：运营单元供应能力 ----
    """create table unit_supply_period(
        valid_from date,
        valid_to date,
        rental_unit varchar,
        converted_area_sqm decimal(18, 4),
        rentable_area_sqm decimal(18, 4),
        not_qualified_area_sqm decimal(18, 4),
        lease_occupied_area_sqm decimal(18, 4),
        rentable_vacant_area_sqm decimal(18, 4),
        disputed_area_sqm decimal(18, 4)
    )""",
    # ---- 异常与待核 ----
    """create table issue_log(
        scope varchar,
        code varchar,
        dataset varchar,
        record_id varchar,
        grain varchar,
        grain_key varchar,
        home varchar,
        rental_unit varchar,
        interval_text varchar,
        detail varchar,
        observed_date date
    )""",
]


@dataclass
class VersionInfo:
    version_id: str
    content_fingerprint: str
    built_at: str
    duckdb_path: Path
    note: str


def _record_id(dataset: str, row: dict[str, Any]) -> str:
    for key in ("payment_id", "handover_id", "conversion_id", "proof_id", "lease_id",
                "agreement", "home"):
        if key in row:
            return str(row[key])
    return ""


class WarehouseStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))

    def close(self) -> None:
        self.con.close()

    def initialize(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self.con.execute(statement)

    def write_version(self, *, source: ParsedSource, facts: NormalizedFacts,
                      home_periods: list[HomeStatePeriod],
                      unit_periods: list[UnitSupplyPeriod], version_id: str,
                      fingerprint: str, built_at: str,
                      spine: tuple[date, date], note: str) -> None:
        self._write_meta(source, version_id, fingerprint, built_at, spine, note)
        self._write_raw(source)
        self._write_facts(facts)
        self._write_periods(home_periods, unit_periods)
        self._write_issues(facts.issues)

    def _write_meta(self, source: ParsedSource, version_id: str, fingerprint: str,
                    built_at: str, spine: tuple[date, date], note: str) -> None:
        self.con.execute(
            "insert into warehouse_meta values (?, ?, ?, ?, ?, ?, ?)",
            [version_id, fingerprint, built_at, str(source.directory),
             spine[0], spine[1], note],
        )
        self.con.executemany(
            "insert into source_files values (?, ?, ?, ?)",
            [
                (name, f"{name}.ndjson", source.file_hashes[name], source.file_row_counts[name])
                for name in DATASETS
            ],
        )

    def _write_raw(self, source: ParsedSource) -> None:
        payload: list[tuple[str, int, str, str]] = []
        for name in DATASETS:
            for index, row in enumerate(source.rows[name]):
                payload.append((name, index, _record_id(name, row),
                                json.dumps(row, ensure_ascii=False, sort_keys=True)))
        self.con.executemany("insert into raw_records values (?, ?, ?, ?)", payload)

    def _write_facts(self, facts: NormalizedFacts) -> None:
        self.con.executemany(
            "insert into home values (?, ?, ?)",
            [(home.home, home.project, home.area_sqm) for home in facts.homes.values()],
        )
        equity_rows = [
            (p.package, p.agreement, p.home, fraction_text(p.interval.start),
             fraction_text(p.interval.end), p.valid_from, p.valid_to,
             facts.homes[p.home].area_sqm * p.interval.length.numerator
             / p.interval.length.denominator)
            for p in facts.equity_periods
        ]
        handover_rows = [
            (h.handover_id, h.home, h.kind, fraction_text(h.interval.start),
             fraction_text(h.interval.end), h.event_date,
             facts.homes[h.home].area_sqm * h.interval.length.numerator
             / h.interval.length.denominator)
            for h in facts.handovers
        ]
        component_rows = [
            (c.rental_unit, c.home, fraction_text(c.interval.start),
             fraction_text(c.interval.end), c.area_sqm, c.effective)
            for c in facts.components
        ]
        qual_rows = [
            (q.proof_id, q.kind, q.rental_unit, q.valid_from, q.valid_to, q.received)
            for q in facts.qualifications
        ]
        lease_rows = [
            (l.lease_id, l.rental_unit, l.signed, l.starts, l.ends, l.returned,
             l.occupancy_end) for l in facts.leases
        ]
        payment_rows = [
            (p.payment_id, p.agreement, p.amount_cents, p.event_date)
            for p in facts.payments
        ]
        for sql, rows in (
            ("insert into equity_period values (?, ?, ?, ?, ?, ?, ?, ?)", equity_rows),
            ("insert into handover_fact values (?, ?, ?, ?, ?, ?, ?)", handover_rows),
            ("insert into unit_component values (?, ?, ?, ?, ?, ?)", component_rows),
            ("insert into qual_window values (?, ?, ?, ?, ?, ?)", qual_rows),
            ("insert into lease_window values (?, ?, ?, ?, ?, ?, ?)", lease_rows),
            ("insert into payment_fact values (?, ?, ?, ?)", payment_rows),
        ):
            if rows:
                self.con.executemany(sql, rows)

    def _write_periods(self, home_periods: list[HomeStatePeriod],
                       unit_periods: list[UnitSupplyPeriod]) -> None:
        batches = (
            ("insert into home_state_period values (?, ?, ?, ?, ?, ?, ?, ?)",
             [(p.valid_from, p.valid_to, p.home, p.project, p.state,
               fraction_text(p.interval.start), fraction_text(p.interval.end), p.area_sqm)
              for p in home_periods]),
            ("insert into unit_supply_period values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [(p.valid_from, p.valid_to, p.rental_unit, p.converted_area_sqm,
               p.rentable_area_sqm, p.not_qualified_area_sqm, p.lease_occupied_area_sqm,
               p.rentable_vacant_area_sqm, p.disputed_area_sqm) for p in unit_periods]),
        )
        for sql, rows in batches:
            if rows:
                self.con.executemany(sql, rows)

    def _write_issues(self, issues: list[Issue]) -> None:
        if not issues:
            return
        self.con.executemany(
            "insert into issue_log values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i.scope, i.code, i.dataset, i.record_id, i.grain, i.key, i.home, i.unit,
              i.interval_text, i.detail, i.observed_date) for i in issues],
        )


def registry_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / "registry.json"


def read_registry(output_dir: str | Path) -> list[VersionInfo]:
    path = registry_path(output_dir)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        VersionInfo(
            version_id=item["version_id"],
            content_fingerprint=item["content_fingerprint"],
            built_at=item["built_at"],
            duckdb_path=Path(item["duckdb_path"]),
            note=item.get("note", ""),
        )
        for item in payload["versions"]
    ]


def write_registry(output_dir: str | Path, versions: list[VersionInfo]) -> None:
    path = registry_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "versions": [
            {
                "version_id": item.version_id,
                "content_fingerprint": item.content_fingerprint,
                "built_at": item.built_at,
                "duckdb_path": str(item.duckdb_path),
                "note": item.note,
            }
            for item in versions
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def find_version(versions: list[VersionInfo], version_id: Optional[str]) -> VersionInfo:
    if not versions:
        raise FileNotFoundError("尚无入仓版本，请先执行 build")
    if version_id is None:
        return versions[-1]
    for item in versions:
        if item.version_id == version_id:
            return item
    raise KeyError(f"版本不存在：{version_id}")
