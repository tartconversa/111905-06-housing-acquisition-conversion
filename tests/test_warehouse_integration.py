"""阶段事实仓集成测试：以业务场景驱动，覆盖三类重点风险与全部阶段规则。

运行：

    python3 -I -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from housing_warehouse.build import build
from housing_warehouse.query import open_version
from tests import fixtures as fx


class WarehouseIntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source_dir = self.tmp / "source"
        self.output_dir = self.tmp / "output"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, rows: dict):
        fx.write_source(self.source_dir, rows)
        return build(self.source_dir, self.output_dir)

    def query(self):
        return open_version(self.output_dir)

    # ---- 场景一：来源资料端到端（部分共有/部分解除/争议/提前租约）----
    def test_reference_scenario_full_lifecycle(self):
        rows = self._reference_rows()
        result = self.build(rows)
        self.assertEqual(result.anomaly_count, 1)
        with self.query() as q:
            codes = {item["code"] for item in q.issues(scope="anomaly")}
            # 合法的部分解除不报异常；提前起租（安全验收尚未生效）报提前供给
            self.assertEqual(codes, {"PREMATURE_SUPPLY"})

            # 房屋面积逐日守恒：三段状态之和等于物理面积
            for day in ("2026-10-19", "2026-10-20", "2026-11-04", "2026-11-05", "2027-11-01"):
                for item in q.home_area_conservation(date.fromisoformat(day)):
                    self.assertTrue(item["conserved"], (day, item))

            # 10-20 交接后、11-05 争议前：U1 一半市场一半交接
            handover = {item["home"]: item for item in
                        q.handover_by_home(date.fromisoformat("2026-11-01"))}
            self.assertEqual(handover["U1"]["handed_area_sqm"], "50.0000")
            self.assertEqual(handover["U1"]["market_pending_sqm"], "50.0000")
            self.assertEqual(handover["U1"]["disputed_area_sqm"], "0.0000")

            # 11-05 争议：争议范围既不回市场也不算稳定供给
            handover = {item["home"]: item for item in
                        q.handover_by_home(date.fromisoformat("2026-11-05"))}
            self.assertEqual(handover["U1"]["disputed_area_sqm"], "25.0000")
            self.assertEqual(handover["U1"]["handed_area_sqm"], "25.0000")
            self.assertEqual(handover["U1"]["market_pending_sqm"], "50.0000")
            # 收购包权益按协议份额：解除只撤销尚有效权益，11-05 后 AG1 仅剩 25
            equity = {(item["agreement"], item["home"]): item["equity_area_sqm"]
                      for item in q.equity_by_package(date.fromisoformat("2026-11-05"))}
            self.assertEqual(equity[("AG1", "U1")], "25.0000")
            self.assertEqual(equity[("AG2", "U2")], "80.0000")

            # 租赁能力：安全验收 11-04 前不计供给；起租日 11-02 触发提前供给
            supply_1102 = q.unit_supply(date.fromisoformat("2026-11-02"))[0]
            self.assertEqual(supply_1102["rentable_area_sqm"], "0.0000")
            self.assertEqual(supply_1102["not_qualified_area_sqm"], "130.0000")
            supply_1104 = q.unit_supply(date.fromisoformat("2026-11-04"))[0]
            self.assertEqual(supply_1104["rentable_area_sqm"], "130.0000")
            self.assertEqual(supply_1104["lease_occupied_area_sqm"], "130.0000")
            self.assertEqual(supply_1104["rentable_vacant_area_sqm"], "0.0000")

            # 争议后：25 平移出可出租供给且不计占用，转换总面积仍为 130（面积不消失）
            supply_1105 = q.unit_supply(date.fromisoformat("2026-11-05"))[0]
            self.assertEqual(supply_1105["converted_area_sqm"], "130.0000")
            self.assertEqual(supply_1105["rentable_area_sqm"], "105.0000")
            self.assertEqual(supply_1105["disputed_area_sqm"], "25.0000")
            self.assertEqual(supply_1105["lease_occupied_area_sqm"], "105.0000")

            # 到期日后：占用归零转为可出租空置；退租/到期不恢复商品房待售
            supply_end = q.unit_supply(date.fromisoformat("2027-11-01"))[0]
            self.assertEqual(supply_end["lease_occupied_area_sqm"], "0.0000")
            self.assertEqual(supply_end["rentable_vacant_area_sqm"], "105.0000")
            journey = {(item["state"], item["coverage"][0], item["coverage"][1]): item
                       for item in q.home_journey("U1", date.fromisoformat("2027-11-01"))}
            self.assertIn(("disputed", "0", "1/4"), journey)

    # ---- 重点风险 1：重计面积 ----
    def test_double_counted_areas_are_flagged_and_counted_once(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/2"], "2026-10-01"),
            ],
            "handovers": [
                fx.handover("H1", "U1", ["0/1", "1/2"], "2026-10-05"),
                # 与 H1 完全重复的交接
                fx.handover("H2", "U1", ["1/4", "1/2"], "2026-10-06"),
            ],
            "conversion": [
                # 同一房屋覆盖被两个运营单元重复映射
                fx.conversion("CV1", "U1", ["0/1", "1/2"], "R1", "2026-10-10"),
                fx.conversion("CV2", "U1", ["1/4", "1/2"], "R2", "2026-10-10"),
            ],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
            ],
            "leases": [],
            "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            codes = {item["code"] for item in q.issues(scope="anomaly")}
            self.assertIn("HANDOVER_OVERLAP", codes)
            self.assertIn("CONVERSION_OVERLAP", codes)
            # 物理交接按并集只计一次：U1 仍只有 50 平交接、50 平市场
            handover = q.handover_by_home(date.fromisoformat("2026-10-07"))[0]
            self.assertEqual(handover["handed_area_sqm"], "50.0000")
            # 重复转换只在首次映射计面积：R2 不产生任何组件面积
            units = {item["rental_unit"]: item
                     for item in q.unit_supply(date.fromisoformat("2026-10-11"))}
            self.assertEqual(units["R1"]["converted_area_sqm"], "50.0000")
            self.assertNotIn("R2", units)
            # 房屋面积守恒不被重复交接破坏
            conserved = q.home_area_conservation(date.fromisoformat("2026-10-07"))[0]
            self.assertTrue(conserved["conserved"])

    def test_termination_beyond_active_equity_is_flagged(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/2"], "2026-10-01"),
                # 解除范围 [1/4, 3/4) 超出尚有效权益 [0, 1/2)
                fx.terminate("TERM1", "PK1", "U1", ["1/4", "3/4"], "2026-11-01", "AG1"),
            ],
            "handovers": [], "conversion": [], "qualifications": [],
            "leases": [], "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            excess = [i for i in q.issues(scope="anomaly")
                      if i["code"] == "TERMINATION_EXCESS"]
            self.assertEqual(len(excess), 1)
            self.assertEqual(excess[0]["interval_text"], "[1/2,3/4)")
            # 有效部分 [1/4,1/2) 仍被解除，剩余权益只有 [0,1/4) = 25 平
            equity = q.equity_by_package(date.fromisoformat("2026-11-02"))
            self.assertEqual(len(equity), 1)
            self.assertEqual(equity[0]["equity_area_sqm"], "25.0000")

    def test_equity_overlap_between_agreements_is_flagged(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [
                # 两个主体在同一空间上各持权益且区间重叠（共有必须份额不重叠）
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/2"], "2026-10-01"),
                fx.acquire("AG2", "PK2", "U1", ["1/4", "3/4"], "2026-10-02"),
            ],
            "handovers": [], "conversion": [], "qualifications": [],
            "leases": [], "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            codes = [item["code"] for item in q.issues(scope="anomaly")]
            self.assertIn("EQUITY_OVERLAP", codes)

    # ---- 重点风险 2：提前供给 ----
    def test_premature_supply_requires_all_gates(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01")],
            "handovers": [fx.handover("H1", "U1", ["0/1", "1/1"], "2026-10-05")],
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/1"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                # 安全验收晚于租约起租
                fx.qual("Q2", "safety", "R1", "2026-10-20"),
            ],
            # 租约在安全验收之前起租
            "leases": [fx.lease("LS1", "R1", "2026-10-08", "2026-10-12", "2027-10-12")],
            "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            self.assertTrue(any(item["code"] == "PREMATURE_SUPPLY"
                                for item in q.issues(scope="anomaly")))
            # 起租日缺安全验收：没有可出租面积，也不确认任何租赁占用
            supply = q.unit_supply(date.fromisoformat("2026-10-12"))[0]
            self.assertEqual(supply["rentable_area_sqm"], "0.0000")
            self.assertEqual(supply["lease_occupied_area_sqm"], "0.0000")
            self.assertEqual(supply["not_qualified_area_sqm"], "100.0000")
            # 验收完成后才形成供给，占用在可出租面积内确认
            supply = q.unit_supply(date.fromisoformat("2026-10-20"))[0]
            self.assertEqual(supply["rentable_area_sqm"], "100.0000")
            self.assertEqual(supply["lease_occupied_area_sqm"], "100.0000")
            self.assertEqual(supply["rentable_vacant_area_sqm"], "0.0000")

    def test_handover_gate_blocks_supply_even_with_permits(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01")],
            "handovers": [],  # 从未物理交接
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/1"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
            ],
            "leases": [fx.lease("LS1", "R1", "2026-10-11", "2026-10-12", "2027-10-12")],
            "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            supply = q.unit_supply(date.fromisoformat("2026-10-12"))[0]
            self.assertEqual(supply["rentable_area_sqm"], "0.0000")
            self.assertEqual(supply["not_qualified_area_sqm"], "100.0000")

    # ---- 重点风险 3：争议释放 ----
    def test_disputed_area_never_returns_to_market_or_supply(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/2"], "2026-10-01"),
                # 解除 AG1 的另一部分权益——不得影响已交接争议范围的占用事实
                fx.terminate("TERM1", "PK1", "U1", ["1/4", "1/2"], "2026-11-01", "AG1"),
            ],
            "handovers": [
                fx.handover("H1", "U1", ["0/1", "1/2"], "2026-10-05"),
                fx.handover("DIS1", "U1", ["0/1", "1/4"], "2026-10-20",
                            kind="physical_dispute"),
            ],
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/2"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
            ],
            "leases": [fx.lease("LS1", "R1", "2026-10-11", "2026-10-15", "2027-10-15")],
            "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            for day in ("2026-10-20", "2026-11-01", "2026-12-01"):
                states = {item["state"]: item for item in
                          q.home_journey("U1", date.fromisoformat(day))}
                # 争议范围持续保留：不回市场、不消失
                self.assertEqual(states["disputed"]["area_sqm"], "25.0000", day)
                market_pieces = [
                    item["coverage"] for item in
                    q.home_journey("U1", date.fromisoformat(day))
                    if item["state"] == "market_pending"
                ]
                self.assertNotIn(["0", "1/4"], market_pieces)
                supply = q.unit_supply(date.fromisoformat(day))[0]
                self.assertEqual(supply["disputed_area_sqm"], "25.0000", day)
                self.assertLess(Decimal(supply["rentable_area_sqm"]),
                                Decimal(supply["converted_area_sqm"]))
            # 争议范围没有交接之外的释放事件，系统也不产生任何“释放”类异常
            self.assertFalse(any("release" in item["code"].lower()
                                 for item in q.issues()))

    def test_dispute_without_handover_goes_pending(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [],
            "handovers": [
                fx.handover("DIS1", "U1", ["0/1", "1/4"], "2026-10-20",
                            kind="physical_dispute"),
            ],
            "conversion": [], "qualifications": [], "leases": [], "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            codes = {item["code"] for item in q.issues(scope="pending")}
            self.assertIn("DISPUTE_OUTSIDE_HANDOVER", codes)

    # ---- 拆并场景：多房合一 + 一房拆多 ----
    def test_merge_and_split_units_keep_traceable_coverage(self):
        rows = {
            "homes": [fx.home("U1", "100.00"), fx.home("U2", "60.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01"),
                fx.acquire("AG2", "PK1", "U2", ["0/1", "1/1"], "2026-10-01"),
            ],
            "handovers": [
                fx.handover("H1", "U1", ["0/1", "1/1"], "2026-10-05"),
                fx.handover("H2", "U2", ["0/1", "1/1"], "2026-10-05"),
            ],
            "conversion": [
                # U1 拆成两个运营单元
                fx.conversion("CV1", "U1", ["0/1", "1/2"], "R1", "2026-10-10"),
                fx.conversion("CV2", "U1", ["1/2", "1/1"], "R2", "2026-10-10"),
                # U2 并入 R1
                fx.conversion("CV3", "U2", ["0/1", "1/1"], "R1", "2026-10-10"),
            ],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
                fx.qual("Q3", "use_permit", "R2", "2026-10-10"),
                fx.qual("Q4", "safety", "R2", "2026-10-10"),
            ],
            "leases": [], "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            units = {item["rental_unit"]: item
                     for item in q.unit_supply(date.fromisoformat("2026-10-11"))}
            # R1 = U1 的一半(50) + U2 全部(60) = 110；R2 = U1 的另一半 = 50
            self.assertEqual(units["R1"]["converted_area_sqm"], "110.0000")
            self.assertEqual(units["R2"]["converted_area_sqm"], "50.0000")
            # 拆并不增加面积：两单元转换面积之和 = 两房屋物理面积之和
            self.assertEqual(
                Decimal(units["R1"]["converted_area_sqm"])
                + Decimal(units["R2"]["converted_area_sqm"]),
                Decimal("160.0000"),
            )
            # 两房屋各自面积守恒
            for item in q.home_area_conservation(date.fromisoformat("2026-10-11")):
                self.assertTrue(item["conserved"])

    # ---- 待核：身份桥接缺面积覆盖 ----
    def test_bridge_without_coverage_goes_pending_not_force_join(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01")],
            "handovers": [fx.handover("H1", "U1", ["0/1", "1/1"], "2026-10-05")],
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/1"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
                # 指向没有任何面积覆盖的运营单元 R9
                fx.qual("QX", "safety", "R9", "2026-10-10"),
            ],
            # 租约同样指向无覆盖单元，不得按房号/名称强行拼接
            "leases": [fx.lease("LS9", "R9", "2026-10-11", "2026-10-12", "2027-10-12")],
            "payments": [],
        }
        self.build(rows)
        with self.query() as q:
            pending = q.issues(scope="pending")
            self.assertTrue(all(item["code"] == "BRIDGE_NO_COVERAGE" for item in pending))
            self.assertEqual({item["record_id"] for item in pending}, {"QX", "LS9"})
            # R1 的供应推导不受 R9 待核记录影响
            units = {item["rental_unit"]: item
                     for item in q.unit_supply(date.fromisoformat("2026-10-12"))}
            self.assertNotIn("R9", units)
            self.assertEqual(units["R1"]["rentable_area_sqm"], "100.0000")

    # ---- 退租不恢复待售；收购解除不终止租约 ----
    def test_return_and_termination_are_decoupled_facts(self):
        rows = {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01"),
                fx.terminate("T1", "PK1", "U1", ["0/1", "1/1"], "2026-11-10", "AG1"),
            ],
            "handovers": [fx.handover("H1", "U1", ["0/1", "1/1"], "2026-10-05")],
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/1"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", "2026-10-10"),
            ],
            "leases": [fx.lease("LS1", "R1", "2026-10-08", "2026-10-12",
                                "2027-10-12", returned="2026-12-01")],
            "payments": [fx.payment("P1", "AG1", 5000000, "2026-10-03")],
        }
        self.build(rows)
        with self.query() as q:
            # 协议 11-10 全部解除：权益消失，但物理交接事实不变、不回市场
            self.assertEqual(q.equity_by_package(date.fromisoformat("2026-11-15")), [])
            handover = q.handover_by_home(date.fromisoformat("2026-11-15"))[0]
            self.assertEqual(handover["handed_area_sqm"], "100.0000")
            self.assertEqual(handover["market_pending_sqm"], "0.0000")
            # 已生效租约不随协议解除终止，11-15 仍在占用
            supply = q.unit_supply(date.fromisoformat("2026-11-15"))[0]
            self.assertEqual(supply["lease_occupied_area_sqm"], "100.0000")
            self.assertEqual(supply["rentable_vacant_area_sqm"], "0.0000")
            # 12-01 实际退租：占用转空置，房屋仍不回市场
            supply = q.unit_supply(date.fromisoformat("2026-12-02"))[0]
            self.assertEqual(supply["lease_occupied_area_sqm"], "0.0000")
            self.assertEqual(supply["rentable_vacant_area_sqm"], "100.0000")
            handover = q.handover_by_home(date.fromisoformat("2026-12-02"))[0]
            self.assertEqual(handover["market_pending_sqm"], "0.0000")
            # 付款是独立现金事实，可单独核对
            payments = q.payments(agreement="AG1")
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0]["amount_cents"], 5000000)

    # ---- 三粒度不混算 ----
    def test_three_grains_remain_separate(self):
        result = self.build(self._reference_rows())
        with self.query() as q:
            day = date.fromisoformat("2026-11-05")
            recon = q.reconcile_grains(day)
            self.assertEqual(len(recon["package_equity"]), 1)
            self.assertEqual(len(recon["physical_handover"]), 2)
            self.assertEqual(len(recon["rental_supply"]), 1)
            # 每个结果都带 grain 标签，且没有跨粒度总计字段
            for section in ("package_equity", "physical_handover", "rental_supply"):
                self.assertTrue(all("grain" in item for item in recon[section]))
            self.assertIn("禁止相加", recon["note"])
            # 同一日三粒度数字口径不同：权益 105（AG1 解除后 25 + AG2 80）、
            # 物理交接覆盖 130（含争议 25）、合格可出租仅 105
            self.assertEqual(recon["package_equity"][0]["equity_area_sqm"], "105.0000")
            # 物理交接覆盖 130 = 清洁交接 105 + 争议 25；合格可出租仅 105
            handed = sum(Decimal(i["handed_area_sqm"]) for i in recon["physical_handover"])
            disputed = sum(Decimal(i["disputed_area_sqm"]) for i in recon["physical_handover"])
            self.assertEqual(handed + disputed, Decimal("130.0000"))
            self.assertEqual(disputed, Decimal("25.0000"))
            self.assertEqual(recon["rental_supply"][0]["rentable_area_sqm"], "105.0000")
        self.assertIsNotNone(result.version.version_id)

    @staticmethod
    def _reference_rows() -> dict:
        return {
            "homes": [fx.home("U1", "100.00"), fx.home("U2", "80.00")],
            "acquisitions": [
                fx.acquire("AG1", "PK1", "U1", ["0/1", "1/2"], "2026-10-10"),
                fx.acquire("AG2", "PK1", "U2", ["0/1", "1/1"], "2026-10-10"),
                fx.terminate("TERM1", "PK1", "U1", ["0/1", "1/4"], "2026-11-05", "AG1"),
            ],
            "payments": [
                fx.payment("PAY1", "AG1", 30000000, "2026-10-12"),
                fx.payment("PAY2", "AG2", 40000000, "2026-10-15"),
            ],
            "handovers": [
                fx.handover("H1", "U1", ["0/1", "1/2"], "2026-10-20"),
                fx.handover("H2", "U2", ["0/1", "1/1"], "2026-10-20"),
                fx.handover("DIS1", "U1", ["0/1", "1/4"], "2026-11-05",
                            kind="physical_dispute"),
            ],
            "conversion": [
                fx.conversion("CV1", "U1", ["0/1", "1/2"], "R1", "2026-11-01"),
                fx.conversion("CV1", "U2", ["0/1", "1/1"], "R1", "2026-11-01"),
            ],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-11-01"),
                fx.qual("Q2", "safety", "R1", "2026-11-04", received="2026-11-09"),
            ],
            "leases": [fx.lease("LS1", "R1", "2026-10-28", "2026-11-02", "2027-11-01")],
        }


if __name__ == "__main__":
    unittest.main()
