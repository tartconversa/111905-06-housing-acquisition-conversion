# 来源资料约定

商品住房收购权益、物理交接和租赁空间转换资料。

fraction及coverage采用精确有理数，coverage以原房屋空间的[from,to)有理区间标识，不表示面积数值；不同原房屋区间彼此独立。

acquisitions记录协议对房屋的有效权益，terminate引用agreement并给出解除范围；payment是现金事实，handover是物理交接，不互相替代。

conversion给出原房屋范围到运营单元的覆盖，对同一原房屋的覆盖不可重复；safety与use_permit的scope覆盖运营单元，valid_from/valid_to限定资格。

lease的signed表示已订立，starts/ends是租赁占用期，return是实际退租事实；physical_dispute记录物理已交接但产权存在争议的范围。面积均为平方米字符串。

## 数据载体

每类资料位于 data 中同名的 NDJSON 文件，一行一个原始对象；字段和基本类型见 data/schema.json。data/manifest.json 固定资料行数与字节哈希。资料中的缺失、歧义、未完整交付及冲突声明属于来源事实，不由资料检查程序推断业务结论。

### homes

商品房物理面积。字段：home (string)、area_sqm (string)、project (string)。

### acquisitions

协议与权益。字段：agreement (string)、package (string)、home (string)、coverage (array)、kind (string)、effective (string)、reference (any)。

### payments

实际付款。字段：payment_id (string)、agreement (string)、amount_cents (integer)、date (string)。

### handovers

物理交接。字段：handover_id (string)、home (string)、coverage (array)、date (string)、kind (string)。

### conversion

运营空间组成。字段：conversion_id (string)、home (string)、coverage (array)、rental_unit (string)、effective (string)。

### qualifications

运营资格。字段：proof_id (string)、kind (string)、scope (string)、valid_from (string)、valid_to (any)、received (string)。

### leases

租约与占用期间。字段：lease_id (string)、unit (string)、signed (string)、starts (string)、ends (string)、returned (any)。

