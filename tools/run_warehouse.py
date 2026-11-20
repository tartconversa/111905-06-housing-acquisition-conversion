#!/usr/bin/env python3
"""隔离模式下的仓命令薄入口。

资料目录属于不可信数据，运行时不能让它出现在模块搜索路径上；本脚本显式把仓库
根目录加入 ``sys.path``，从而可以配合 ``python3 -I`` 运行：

    python3 -I tools/run_warehouse.py build --source data --output output
    python3 -I tools/run_warehouse.py query reconcile --day 2026-11-05
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from housing_warehouse.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
