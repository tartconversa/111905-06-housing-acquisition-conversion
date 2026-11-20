# 阶段事实仓模型说明

本仓把来源资料整理为三层：原始层（来源对象原样落盘）、规范化事实层、SCD-2 期间
事实层。所有面积以平方米 `Decimal(18,4)` 存储，覆盖区间以有理分数字符串存储。

## 覆盖区间与面积

* `coverage` 是原房屋归一化空间上的 `[from,to)` 区间（约定位于 `[0,1)`），标识
  空间范围而**不是面积数值**；不同原房屋的区间彼此独立，禁止跨房屋比较或合并。
* 区间运算（并、交、差、合并）全部使用 `Fraction` 精确完成；面积 = 房屋物理面积
  × 区间长度，折算时才转 `Decimal`。
* 同一房屋的重复覆盖按**并集只计一次**，直接求和与并集长度之差用于识别重计面积。

## 三种粒度（禁止相加）

### 1. 协议权益粒度 —— 表 `equity_period` / 查询 `packages`、`equity`

* 收购包是拟定范围；实际权益按协议中的覆盖份额在 `effective` 日生效。
* `terminate` 记录通过 `reference` 定位被解除的协议，只撤销其**尚有效**的权益，
  按生效/解除事件日切分为若干 `[valid_from, valid_to)` 期间；`valid_to` 为空
  表示开放。
* 同一房屋可由两个主体各持不重叠份额（部分共有）；份额重叠记 `EQUITY_OVERLAP`
  异常。解除超出尚有效权益的部分记 `TERMINATION_EXCESS`，有效部分仍照常解除。

### 2. 物理交接粒度 —— 表 `handover_fact`、`home_state_period` / 查询 `handovers`、`journey`

* 每个房屋在任一日的面积被完整切分为三段，三者之和恒等于房屋物理面积
  （`conservation` 查询逐日核对）：
  * `market_pending`：尚未物理交接，仍在市场待售；
  * `handed_over`：已物理交接且无产权争议；
  * `disputed`：已物理交接但存在产权争议。
* 交接重复记 `HANDOVER_OVERLAP`，按并集计一次。争议范围没有对应交接记录时进
  待核 `DISPUTE_OUTSIDE_HANDOVER`。
* 争议只叠加状态、不撤销交接：争议范围**不回市场、不消失，也不计入稳定租赁
  供给**。收购解除不改变物理交接事实。

### 3. 运营空间粒度 —— 表 `unit_component`、`unit_supply_period` / 查询 `supply`

* 转换映射给出原房屋覆盖到运营单元的组成；同一原房屋覆盖不可重复映射，重复部分
  记 `CONVERSION_OVERLAP` 且不计面积。多个原房屋可合为一套租赁空间，一套原房屋
  也可拆成多个运营单元；拆并不增加面积（转换面积之和不超过来源房屋面积）。
* 每个运营单元在每个期间输出：

  | 字段 | 口径 |
  | --- | --- |
  | `converted_area_sqm` | 已生效转换覆盖的总面积（含未合格、争议） |
  | `rentable_area_sqm` | 合格可出租面积 |
  | `not_qualified_area_sqm` | 已转换但交接未覆盖或资格未满足的面积 |
  | `lease_occupied_area_sqm` | 正式租赁占用（只在可出租面积内确认） |
  | `rentable_vacant_area_sqm` | 可出租空置 = 可出租 − 占用 |
  | `disputed_area_sqm` | 转换覆盖中处于产权争议的面积 |

* **供给门槛（同时满足才计入可出租）**：物理交接覆盖该组件范围、转换已生效、
  安全验收（`safety`）与用途许可（`use_permit`）在当日均处于有效期。资格按
  `valid_from/valid_to` 表达窗口，缺任一类型即不合格。
* 租约可提前签订（`signed` 早于 `starts`）；起租日没有合格可出租面积承接时记
  `PREMATURE_SUPPLY` 异常，且当日不确认任何租赁占用。
* 占用期为 `[starts, returned 或 ends)`；重叠租约记 `LEASE_OVERLAP`，占用面积
  上限仍为可出租面积本身，不重复计数。退租只把占用转为可出租空置，**不恢复商品
  房待售**；协议解除也**不结束已生效租约**。

## 期间物化（SCD-2）

状态只在来源事件日变化，因此不逐日展开，而把每个稳定状态存为一行半开区间
`[valid_from, valid_to)`。按日查询用 `valid_from <= ? and (valid_to is null or
? < valid_to)` 匹配。后补验收或协议更正只会在有依据的事件日改变期间边界；脊线
末端由 `--horizon` 控制，缺省为最大事件日 +30 天。

## 异常与待核（表 `issue_log`）

* `anomaly`：能入表但违反业务约束的来源事实（重计、超额解除、提前供给、租约
  重叠等），事实仍保留，统计按保守口径处理。
* `pending`：结构性无法可靠入表的记录（引用不存在的房屋、身份桥接缺少面积覆盖
  `BRIDGE_NO_COVERAGE`、日期顺序非法等），不按房号相同强行拼接，留待人工复核。

| code | 含义 |
| --- | --- |
| `EQUITY_OVERLAP` | 协议间或协议内权益覆盖重叠（重计面积） |
| `TERMINATION_EXCESS` | 解除范围超出协议尚有效权益 |
| `HANDOVER_OVERLAP` | 物理交接覆盖重复 |
| `CONVERSION_OVERLAP` | 同一原房屋覆盖被重复映射到运营单元 |
| `LEASE_OVERLAP` | 同一运营单元租约占用期重叠 |
| `PREMATURE_SUPPLY` | 租约起租时无合格可出租面积承接 |
| `BRIDGE_NO_COVERAGE` | 资格/租约指向没有面积覆盖的运营单元 |
| `DISPUTE_OUTSIDE_HANDOVER` | 争议范围缺少对应物理交接 |
| `UNKNOWN_HOME` / `UNKNOWN_KIND` / `BAD_DATE_ORDER` / `BAD_AREA` / `PACKAGE_CONFLICT` / `MISSING_TERMINATION_REFERENCE` | 其他来源形状或引用问题 |

## 不可变版本与可复核性

* 每次入仓对全部资料字节计算内容指纹，版本号为 `vNNNN-<fingerprint>`，DuckDB
  文件存于 `output/<version_id>/warehouse.duckdb`，`output/registry.json` 记录
  全部版本。
* 指纹相同即复用版本；资料更正生成新版本，**旧版本永不覆盖或删除**，报告引用过
  的版本可随时用 `--version` 重新打开复核。
* 每个版本的 `raw_records` 表原样保留来源对象（含 JSON 文本），任何结论都可
  回溯到具体资料行；资料字节与 manifest 不符时接入直接拒绝。
