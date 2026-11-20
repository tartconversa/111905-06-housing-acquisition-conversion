"""入仓编排：接入 → 规范化 → 按日推导 → 不可变版本落盘。

同一资料重复入仓得到相同的内容指纹，复用既有版本而不是制造新文件；资料更正后
指纹变化，生成新版本，旧版本原样保留——后补验收或协议更正只改变有依据的期间，
已用于报告的版本仍可复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .engine import (
    build_home_state_periods,
    build_unit_supply_periods,
    date_spine,
    detect_premature_supply,
)
from .ingest import load_source
from .model import normalize
from .storage import (
    VersionInfo,
    WarehouseStorage,
    read_registry,
    write_registry,
)


@dataclass
class BuildResult:
    version: VersionInfo
    anomaly_count: int
    pending_count: int
    spine: tuple[date, date]
    reused: bool


def build(source_dir: str | Path, output_dir: str | Path, *, note: str = "",
          horizon: Optional[date] = None) -> BuildResult:
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    source = load_source(source_dir)
    fingerprint = source.content_fingerprint()

    versions = read_registry(output_dir)
    for existing in versions:
        if existing.content_fingerprint == fingerprint:
            # 内容未变：版本不可变，直接复用，保证报告口径可重复。
            return BuildResult(
                version=existing,
                anomaly_count=_count_issues(existing, "anomaly"),
                pending_count=_count_issues(existing, "pending"),
                spine=_spine(existing),
                reused=True,
            )

    facts = normalize(source)
    spine = date_spine(facts, horizon)
    home_periods = build_home_state_periods(facts, *spine)
    unit_periods = build_unit_supply_periods(facts, *spine)
    facts.issues.extend(detect_premature_supply(facts, unit_periods))

    built_at = datetime.now().isoformat(timespec="seconds")
    version_id = f"v{len(versions) + 1:04d}-{fingerprint}"
    duckdb_path = output_dir / version_id / "warehouse.duckdb"
    storage = WarehouseStorage(duckdb_path)
    try:
        storage.initialize()
        storage.write_version(
            source=source,
            facts=facts,
            home_periods=home_periods,
            unit_periods=unit_periods,
            version_id=version_id,
            fingerprint=fingerprint,
            built_at=built_at,
            spine=spine,
            note=note,
        )
    finally:
        storage.close()

    info = VersionInfo(
        version_id=version_id,
        content_fingerprint=fingerprint,
        built_at=built_at,
        duckdb_path=duckdb_path,
        note=note,
    )
    versions.append(info)
    write_registry(output_dir, versions)
    return BuildResult(
        version=info,
        anomaly_count=len(facts.anomalies()),
        pending_count=len(facts.pending()),
        spine=spine,
        reused=False,
    )


def _count_issues(version: VersionInfo, scope: str) -> int:
    import duckdb

    con = duckdb.connect(str(version.duckdb_path), read_only=True)
    try:
        return con.execute(
            "select count(*) from issue_log where scope = ?", [scope]
        ).fetchone()[0]
    finally:
        con.close()


def _spine(version: VersionInfo) -> tuple[date, date]:
    import duckdb

    con = duckdb.connect(str(version.duckdb_path), read_only=True)
    try:
        row = con.execute("select spine_start, spine_end from warehouse_meta").fetchone()
        return row[0], row[1]
    finally:
        con.close()
