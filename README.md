# 存量住房收购转租的资产阶段仓

商品住房收购权益、物理交接和租赁空间转换的**阶段事实仓**。它把采购（收购包）、
交接（物理房屋）、运营（可出租空间）三边各自登记的来源资料，整理为可互相核对的
资产经历与供应数据集，**只服务资料复核及统计，不办理收购或租赁业务**。

## 解决的问题

三边资料粒度不同，直接相加无法回答一套房处于市场待售、已收购待交接还是已形成
租赁供给。本仓坚持三条原则：

1. **三种粒度不混算**：协议权益（包/协议）、物理交接（房屋）、租赁能力（运营
   单元）各自成表，输出均带 `grain` 标签，只通过原房屋覆盖区间互相追踪，不产生
   跨粒度的“守恒总数”。
2. **阶段不可互相替代**：付款是现金事实、协议按份额与解除表达权益、交接是物理
   事实，分别独立生效；不能任选一个时间替代全部阶段。
3. **覆盖精确可追踪**：`coverage` 是原房屋归一化空间上的有理区间 `[from,to)`，
   全部用 `fractions.Fraction` 精确运算，面积折算才落到 `Decimal`，从根上避免
   浮点误差；拆并、共有、争议都能追踪到具体覆盖。

## 环境

使用 Python 3.11，依赖精确版本记录在 requirements.txt（仅 DuckDB）：

    python3 -m pip install -r requirements.txt

资料目录属于不可信数据，所有命令建议以隔离模式 `python3 -I` 运行，避免资料目录
中的同名模块被解释器加载。

## 可重复操作

### 1. 校验来源资料

    python3 -I tools/inspect_source.py

逐文件核对 manifest 中的行数与 SHA-256，并按 schema 检查字段形状。

### 2. 入仓，生成不可变版本

    python3 -I tools/run_warehouse.py build --source data --output output

同一资料内容（字节指纹相同）重复入仓会复用既有版本，不产生新文件；资料更正后
指纹变化才生成新版本，旧版本原样保留。开放事实的物化窗口可用 `--horizon` 显式
指定（ISO 日期），缺省为资料最大事件日 +30 天：

    python3 -I tools/run_warehouse.py build --source data --output output \
        --horizon 2028-01-31 --note "月度复核"

### 3. 版本管理

    python3 -I tools/run_warehouse.py versions --output output

### 4. 复核查询（默认最新版本，`--version` 可指定历史版本）

    # 某日三粒度并排核对（不做跨粒度加总）
    python3 -I tools/run_warehouse.py query reconcile --day 2026-11-05 --output output

    # 一套房屋的阶段经历；房屋三段面积守恒核对
    python3 -I tools/run_warehouse.py query journey --home U1 --day 2026-11-05 --output output
    python3 -I tools/run_warehouse.py query conservation --day 2026-11-05 --output output

    # 按收购包/协议的权益；按物理房屋的交接；按运营单元的租赁能力
    python3 -I tools/run_warehouse.py query packages --day 2026-11-05 --output output
    python3 -I tools/run_warehouse.py query equity   --day 2026-11-05 --output output
    python3 -I tools/run_warehouse.py query handovers --day 2026-11-05 --output output
    python3 -I tools/run_warehouse.py query supply   --day 2026-11-05 --output output

    # 异常与待核、付款事实、原始记录回溯
    python3 -I tools/run_warehouse.py query issues --output output
    python3 -I tools/run_warehouse.py query payments --output output
    python3 -I tools/run_warehouse.py query raw --dataset acquisitions --row-index 0 --output output

### 5. 测试

    python3 -I -m unittest discover -s tests -v
    python3 -m compileall -q housing_warehouse tools tests

## 资料与模型

来源格式见 [docs/source-contract.md](docs/source-contract.md)，仓内表结构、状态
口径、异常代码与版本机制见 [docs/warehouse-model.md](docs/warehouse-model.md)。
`data/` 内均为本地合成记录，不含真实个人或机构数据。
