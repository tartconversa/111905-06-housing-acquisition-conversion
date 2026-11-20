"""测试夹具：在临时目录构造符合来源约定的合成资料。

每个夹具自带 manifest.json（行数 + 字节哈希）与 schema.json（复制真实约定），
因此接入校验路径与生产完全一致。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATASETS = (
    "homes",
    "acquisitions",
    "payments",
    "handovers",
    "conversion",
    "qualifications",
    "leases",
)


def write_source(base: str | Path, rows: dict[str, list[dict]]) -> Path:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "data" / "schema.json", base / "schema.json")
    manifest = {"description": "测试合成资料", "files": []}
    for name in DATASETS:
        records = rows.get(name, [])
        content = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        )
        path = base / f"{name}.ndjson"
        data = content.encode("utf-8")
        path.write_bytes(data)
        manifest["files"].append({
            "path": f"{name}.ndjson",
            "dataset": name,
            "rows": len(records),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    (base / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return base


def home(home_id: str, area: str = "100.00", project: str = "P1") -> dict:
    return {"home": home_id, "area_sqm": area, "project": project}


def acquire(agreement: str, package: str, home_id: str, coverage: list[str],
            effective: str, reference=None) -> dict:
    return {
        "agreement": agreement, "package": package, "home": home_id,
        "coverage": coverage, "kind": "acquire", "effective": effective,
        "reference": reference,
    }


def terminate(record_id: str, package: str, home_id: str, coverage: list[str],
              effective: str, reference: str) -> dict:
    return {
        "agreement": record_id, "package": package, "home": home_id,
        "coverage": coverage, "kind": "terminate", "effective": effective,
        "reference": reference,
    }


def handover(record_id: str, home_id: str, coverage: list[str], day: str,
             kind: str = "received") -> dict:
    return {
        "handover_id": record_id, "home": home_id, "coverage": coverage,
        "date": day, "kind": kind,
    }


def conversion(record_id: str, home_id: str, coverage: list[str],
               unit: str, effective: str) -> dict:
    return {
        "conversion_id": record_id, "home": home_id, "coverage": coverage,
        "rental_unit": unit, "effective": effective,
    }


def qual(proof_id: str, kind: str, unit: str, valid_from: str,
         valid_to=None, received: str | None = None) -> dict:
    return {
        "proof_id": proof_id, "kind": kind, "scope": unit,
        "valid_from": valid_from, "valid_to": valid_to,
        "received": received or valid_from,
    }


def lease(lease_id: str, unit: str, signed: str, starts: str, ends: str,
          returned=None) -> dict:
    return {
        "lease_id": lease_id, "unit": unit, "signed": signed,
        "starts": starts, "ends": ends, "returned": returned,
    }


def payment(payment_id: str, agreement: str, amount: int, day: str) -> dict:
    return {
        "payment_id": payment_id, "agreement": agreement,
        "amount_cents": amount, "date": day,
    }
