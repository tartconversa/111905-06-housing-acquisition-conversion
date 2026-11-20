"""资料接入：字节/行数校验、NDJSON 解析与基础形状检查。

严格遵守来源边界：这里只验证载体完整性和字段形状，资料中的缺失、歧义、未完整
交付及冲突声明原样保留为来源事实，业务推断全部留给后续阶段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rational import Interval

DATASETS = (
    "homes",
    "acquisitions",
    "payments",
    "handovers",
    "conversion",
    "qualifications",
    "leases",
)


class SourceError(ValueError):
    """来源资料载体或形状不合约定。"""


@dataclass
class ParsedSource:
    """一次接入得到的全部原始对象，按资料集组织。"""

    directory: Path
    rows: dict[str, list[dict[str, Any]]]
    file_hashes: dict[str, str]
    file_row_counts: dict[str, int]

    def content_fingerprint(self) -> str:
        """全部资料字节的稳定指纹，作为不可变版本标识的基础。"""
        joined = "\n".join(
            f"{name}:{self.file_hashes[name]}" for name in DATASETS if name in self.file_hashes
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _verify_manifest(directory: Path) -> tuple[dict[str, str], dict[str, int]]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    hashes: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    for entry in manifest["files"]:
        path = directory / entry["path"]
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            raise SourceError(f"资料哈希不符：{entry['path']}")
        hashes[entry["dataset"]] = digest
        row_counts[entry["dataset"]] = entry["rows"]
    return hashes, row_counts


def _check_shape(name: str, rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    fields = set(schema["datasets"][name]["fields"])
    primitives = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for index, row in enumerate(rows, 1):
        if set(row) != fields:
            raise SourceError(f"字段不符：{name}.ndjson:{index}")
        for field_name, kind in schema["datasets"][name]["fields"].items():
            value = row[field_name]
            if kind == "any":
                continue
            expected = primitives[kind]
            if not isinstance(value, expected) or (kind == "integer" and isinstance(value, bool)):
                raise SourceError(f"字段类型不符：{name}.ndjson:{index}:{field_name}")


def parse_coverage(raw: Any, *, dataset: str, record_id: str) -> list[Interval]:
    """把资料中的 coverage 解析为区间列表。

    资料约定 coverage 是单个 ``[from, to)`` 有理区间的两个端点（长度恰为 2 的
    字符串数组）；列表形式保留扩展空间，但当前每份记录只声明一个区间。
    """
    if not isinstance(raw, list) or not raw:
        raise SourceError(f"{dataset}:{record_id} 的 coverage 必须是非空数组")
    if len(raw) == 2 and all(isinstance(endpoint, str) for endpoint in raw):
        return [Interval.parse(raw)]
    intervals: list[Interval] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise SourceError(
                f"{dataset}:{record_id} 的 coverage 必须是 [from, to) 两个有理端点"
            )
        intervals.append(Interval.parse(pair))
    return intervals


def load_source(directory: str | Path) -> ParsedSource:
    """接入并校验一个资料目录，返回解析后的来源对象。"""
    directory = Path(directory)
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    hashes, manifest_rows = _verify_manifest(directory)
    parsed: dict[str, list[dict[str, Any]]] = {}
    for name in DATASETS:
        path = directory / f"{name}.ndjson"
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(rows) != manifest_rows[name]:
            raise SourceError(f"资料行数不符：{name}.ndjson")
        _check_shape(name, rows, schema)
        parsed[name] = rows
    return ParsedSource(
        directory=directory,
        rows=parsed,
        file_hashes=hashes,
        file_row_counts=manifest_rows,
    )
