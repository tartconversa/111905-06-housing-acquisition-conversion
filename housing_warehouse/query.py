"""面向复核与统计的只读查询层。

三种粒度各自汇总，输出中显式标注 grain，调用方无法把三包数字误加成一个看似
守恒的总数；``reconcile_grains`` 把同一日期的三粒度结果并排呈现，仅用于互相
核对，不做跨粒度求和。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import duckdb

from .storage import VersionInfo, find_version, read_registry


def list_versions(output_dir: str | Path) -> list[VersionInfo]:
    return read_registry(output_dir)


def open_version(output_dir: str | Path, version_id: Optional[str] = None) -> "WarehouseQuery":
    info = find_version(read_registry(output_dir), version_id)
    return WarehouseQuery(info)


class WarehouseQuery:
    def __init__(self, version: VersionInfo):
        self.version = version
        self.con = duckdb.connect(str(version.duckdb_path), read_only=True)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "WarehouseQuery":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def meta(self) -> dict:
        row = self.con.execute(
            "select version_id, content_fingerprint, built_at, source_directory, "
            "spine_start, spine_end, note from warehouse_meta"
        ).fetchone()
        return {
            "version_id": row[0],
            "content_fingerprint": row[1],
            "built_at": row[2],
            "source_directory": row[3],
            "spine_start": row[4].isoformat(),
            "spine_end": row[5].isoformat(),
            "note": row[6],
        }

    # ---- 物理房屋粒度：一套房的逐日经历（按期间表反查当日状态）----
    def home_journey(self, home: str, day: Optional[date] = None) -> list[dict]:
        if day is None:
            sql = (
                "select valid_from, valid_to, home, project, state, cov_start, cov_end, area_sqm "
                "from home_state_period where home = ? "
                "order by valid_from, state, cov_start"
            )
            params: list = [home]
        else:
            sql = (
                "select valid_from, valid_to, home, project, state, cov_start, cov_end, area_sqm "
                "from home_state_period where home = ? and valid_from <= ? "
                "and (valid_to is null or ? < valid_to) "
                "order by state, cov_start"
            )
            params = [home, day, day]
        rows = self.con.execute(sql, params).fetchall()
        result = []
        for r in rows:
            result.append({
                "grain": "home",
                "valid_from": r[0].isoformat(),
                "valid_to": r[1].isoformat() if r[1] else None,
                "day": day.isoformat() if day else r[0].isoformat(),
                "home": r[2],
                "project": r[3],
                "state": r[4],
                "coverage": [r[5], r[6]],
                "area_sqm": str(r[7]),
            })
        return result

    def home_area_conservation(self, day: date) -> list[dict]:
        """逐日房屋三段面积守恒核对：三段之和必须等于房屋物理面积。"""
        rows = self.con.execute(
            """
            select d.home, h.area_sqm as home_area,
                   sum(d.area_sqm) as staged_area,
                   sum(case when d.state = 'market_pending' then d.area_sqm else 0 end) as market,
                   sum(case when d.state = 'handed_over' then d.area_sqm else 0 end) as handed,
                   sum(case when d.state = 'disputed' then d.area_sqm else 0 end) as disputed
            from home_state_period d join home h on h.home = d.home
            where d.valid_from <= ? and (d.valid_to is null or ? < d.valid_to)
            group by d.home, h.area_sqm
            order by d.home
            """,
            [day, day],
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "grain": "home",
                "day": day.isoformat(),
                "home": r[0],
                "home_area_sqm": str(r[1]),
                "staged_area_sqm": str(r[2]),
                "market_pending_sqm": str(r[3]),
                "handed_over_sqm": str(r[4]),
                "disputed_sqm": str(r[5]),
                "conserved": bool(r[2] == r[1]),
            })
        return result

    # ---- 协议权益粒度：按收购包/协议汇总 ----
    @staticmethod
    def _active_clause(column: str = "valid") -> str:
        return "valid_from <= ? and (valid_to is null or ? < valid_to)"

    def equity_by_package(self, day: date) -> list[dict]:
        rows = self.con.execute(
            f"""
            select package, agreement, home, sum(equity_area_sqm) as area
            from equity_period where {self._active_clause()}
            group by package, agreement, home
            order by package, agreement, home
            """,
            [day, day],
        ).fetchall()
        return [
            {
                "grain": "agreement_equity",
                "day": day.isoformat(),
                "package": r[0],
                "agreement": r[1],
                "home": r[2],
                "equity_area_sqm": str(r[3]),
            }
            for r in rows
        ]

    def package_totals(self, day: date) -> list[dict]:
        rows = self.con.execute(
            f"""
            select package, sum(equity_area_sqm) as total,
                   count(distinct agreement) as agreements,
                   count(distinct home) as homes
            from equity_period where {self._active_clause()}
            group by package order by package
            """,
            [day, day],
        ).fetchall()
        return [
            {
                "grain": "package",
                "day": day.isoformat(),
                "package": r[0],
                "equity_area_sqm": str(r[1]),
                "agreement_count": r[2],
                "home_count": r[3],
            }
            for r in rows
        ]

    # ---- 物理交接粒度：按房屋汇总 ----
    def handover_by_home(self, day: date) -> list[dict]:
        rows = self.con.execute(
            f"""
            select d.home, d.project,
                   sum(case when d.state = 'handed_over' then d.area_sqm else 0 end),
                   sum(case when d.state = 'disputed' then d.area_sqm else 0 end),
                   sum(case when d.state = 'market_pending' then d.area_sqm else 0 end)
            from home_state_period d
            where d.valid_from <= ? and (d.valid_to is null or ? < d.valid_to)
            group by d.home, d.project order by d.home
            """,
            [day, day],
        ).fetchall()
        return [
            {
                "grain": "physical_handover",
                "day": day.isoformat(),
                "home": r[0],
                "project": r[1],
                "handed_area_sqm": str(r[2]),
                "disputed_area_sqm": str(r[3]),
                "market_pending_sqm": str(r[4]),
            }
            for r in rows
        ]

    # ---- 运营空间粒度：租赁能力 ----
    def unit_supply(self, day: date, unit: Optional[str] = None) -> list[dict]:
        sql = (
            "select valid_from, valid_to, rental_unit, converted_area_sqm, rentable_area_sqm, "
            "not_qualified_area_sqm, lease_occupied_area_sqm, rentable_vacant_area_sqm, "
            "disputed_area_sqm from unit_supply_period "
            "where valid_from <= ? and (valid_to is null or ? < valid_to)"
        )
        params: list = [day, day]
        if unit is not None:
            sql += " and rental_unit = ?"
            params.append(unit)
        sql += " order by rental_unit"
        rows = self.con.execute(sql, params).fetchall()
        return [
            {
                "grain": "rental_unit",
                "day": day.isoformat(),
                "valid_from": r[0].isoformat(),
                "valid_to": r[1].isoformat() if r[1] else None,
                "rental_unit": r[2],
                "converted_area_sqm": str(r[3]),
                "rentable_area_sqm": str(r[4]),
                "not_qualified_area_sqm": str(r[5]),
                "lease_occupied_area_sqm": str(r[6]),
                "rentable_vacant_area_sqm": str(r[7]),
                "disputed_area_sqm": str(r[8]),
            }
            for r in rows
        ]

    def reconcile_grains(self, day: date) -> dict:
        """同一日期三粒度并排核对；明确不做跨粒度加总。"""
        return {
            "day": day.isoformat(),
            "note": "三种粒度口径不同，禁止相加；仅通过原房屋覆盖区间互相追踪。",
            "package_equity": self.package_totals(day),
            "physical_handover": self.handover_by_home(day),
            "rental_supply": self.unit_supply(day),
            "home_conservation": self.home_area_conservation(day),
        }

    # ---- 付款事实（现金事实，独立于权益与交接）----
    def payments(self, agreement: Optional[str] = None) -> list[dict]:
        sql = (
            "select payment_id, agreement, amount_cents, event_date "
            "from payment_fact"
        )
        params: list = []
        if agreement is not None:
            sql += " where agreement = ?"
            params.append(agreement)
        sql += " order by event_date, payment_id"
        rows = self.con.execute(sql, params).fetchall()
        return [
            {
                "grain": "payment",
                "payment_id": r[0],
                "agreement": r[1],
                "amount_cents": r[2],
                "event_date": r[3].isoformat(),
            }
            for r in rows
        ]

    # ---- 异常与待核 ----
    def issues(self, scope: Optional[str] = None, code: Optional[str] = None) -> list[dict]:
        sql = "select scope, code, dataset, record_id, grain, grain_key, home, rental_unit, " \
              "interval_text, detail, observed_date from issue_log"
        clauses: list[str] = []
        params: list = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by scope, code, record_id"
        rows = self.con.execute(sql, params).fetchall()
        return [
            {
                "scope": r[0],
                "code": r[1],
                "dataset": r[2],
                "record_id": r[3],
                "grain": r[4],
                "grain_key": r[5],
                "home": r[6],
                "rental_unit": r[7],
                "interval_text": r[8],
                "detail": r[9],
                "observed_date": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ]

    def raw_record(self, dataset: str, row_index: int) -> dict:
        import json

        row = self.con.execute(
            "select payload_json from raw_records where dataset = ? and row_index = ?",
            [dataset, row_index],
        ).fetchone()
        if row is None:
            raise KeyError(f"原始记录不存在：{dataset}[{row_index}]")
        return json.loads(row[0])
