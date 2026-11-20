"""存量住房收购转租的阶段事实仓。

三类粒度彼此独立、仅通过原房屋覆盖区间可追踪：

* 协议权益（收购包/协议粒度）：``equity_period``、``daily_equity``
* 物理交接（物理房屋粒度）：``handover_period``、``daily_home_state``
* 租赁能力（运营单元粒度）：``unit_component``、``daily_unit_supply``

本仓只服务资料复核与统计，不办理收购或租赁业务。
"""

from .build import BuildResult, build
from .query import (
    WarehouseQuery,
    list_versions,
    open_version,
)

__all__ = [
    "BuildResult",
    "build",
    "WarehouseQuery",
    "list_versions",
    "open_version",
]
