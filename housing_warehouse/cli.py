"""命令行入口：入仓、版本管理与复核查询。

推荐以隔离模式运行（避免资料目录中的同名模块被解释器加载）：

    python3 -I tools/run_warehouse.py build --source data --output output
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .build import build
from .query import list_versions, open_version


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _cmd_build(args: argparse.Namespace) -> int:
    horizon = date.fromisoformat(args.horizon) if args.horizon else None
    result = build(args.source, args.output, note=args.note or "", horizon=horizon)
    _print({
        "action": "build",
        "reused_existing_version": result.reused,
        "version_id": result.version.version_id,
        "content_fingerprint": result.version.content_fingerprint,
        "built_at": result.version.built_at,
        "duckdb_path": str(result.version.duckdb_path),
        "spine": [result.spine[0].isoformat(), result.spine[1].isoformat()],
        "anomaly_count": result.anomaly_count,
        "pending_count": result.pending_count,
        "note": result.version.note,
    })
    return 0


def _cmd_versions(args: argparse.Namespace) -> int:
    versions = list_versions(args.output)
    _print({
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
    })
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    with open_version(args.output, args.version) as query:
        day = date.fromisoformat(args.day) if args.day else None
        if args.query_name == "meta":
            payload = query.meta()
        elif args.query_name == "journey":
            payload = query.home_journey(args.home, day)
        elif args.query_name == "conservation":
            payload = query.home_area_conservation(day)
        elif args.query_name == "packages":
            payload = query.package_totals(day)
        elif args.query_name == "equity":
            payload = query.equity_by_package(day)
        elif args.query_name == "handovers":
            payload = query.handover_by_home(day)
        elif args.query_name == "supply":
            payload = query.unit_supply(day, args.unit)
        elif args.query_name == "reconcile":
            payload = query.reconcile_grains(day)
        elif args.query_name == "payments":
            payload = query.payments(agreement=args.agreement)
        elif args.query_name == "issues":
            payload = query.issues(scope=args.scope, code=args.code)
        elif args.query_name == "raw":
            payload = query.raw_record(args.dataset, args.row_index)
        else:  # pragma: no cover - argparse 已限定取值
            raise SystemExit(f"未知查询：{args.query_name}")
    _print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warehouse",
        description="存量住房收购转租阶段事实仓：入仓与复核查询（不办理收购或租赁业务）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="接入资料并生成不可变入仓版本")
    build_cmd.add_argument("--source", type=Path, default=Path("data"))
    build_cmd.add_argument("--output", type=Path, default=Path("output"))
    build_cmd.add_argument("--note", default="")
    build_cmd.add_argument(
        "--horizon", default=None,
        help="开放事实的物化截止日 ISO 日期；缺省为资料最大事件日 +30 天",
    )
    build_cmd.set_defaults(func=_cmd_build)

    versions_cmd = sub.add_parser("versions", help="列出全部不可变版本")
    versions_cmd.add_argument("--output", type=Path, default=Path("output"))
    versions_cmd.set_defaults(func=_cmd_versions)

    query_cmd = sub.add_parser("query", help="对指定版本执行复核查询")
    query_cmd.add_argument("query_name", choices=[
        "meta", "journey", "conservation", "packages", "equity", "handovers",
        "supply", "reconcile", "payments", "issues", "raw",
    ])
    query_cmd.add_argument("--output", type=Path, default=Path("output"))
    query_cmd.add_argument("--version", default=None, help="版本号，缺省使用最新版本")
    query_cmd.add_argument("--day", default=None, help="ISO 日期，如 2026-11-05")
    query_cmd.add_argument("--home", default=None)
    query_cmd.add_argument("--unit", default=None)
    query_cmd.add_argument("--dataset", default=None)
    query_cmd.add_argument("--agreement", default=None)
    query_cmd.add_argument("--row-index", type=int, default=0, dest="row_index")
    query_cmd.add_argument("--scope", default=None, choices=["anomaly", "pending"])
    query_cmd.add_argument("--code", default=None)
    query_cmd.set_defaults(func=_cmd_query)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
