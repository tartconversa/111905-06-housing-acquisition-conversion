"""不可变版本与可复核性测试。"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from housing_warehouse.build import build
from housing_warehouse.query import list_versions, open_version
from tests import fixtures as fx


class VersioningCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source_dir = self.tmp / "source"
        self.output_dir = self.tmp / "output"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self, safety_from: str = "2026-10-20"):
        return {
            "homes": [fx.home("U1", "100.00")],
            "acquisitions": [fx.acquire("AG1", "PK1", "U1", ["0/1", "1/1"], "2026-10-01")],
            "handovers": [fx.handover("H1", "U1", ["0/1", "1/1"], "2026-10-05")],
            "conversion": [fx.conversion("CV1", "U1", ["0/1", "1/1"], "R1", "2026-10-10")],
            "qualifications": [
                fx.qual("Q1", "use_permit", "R1", "2026-10-10"),
                fx.qual("Q2", "safety", "R1", safety_from),
            ],
            "leases": [fx.lease("LS1", "R1", "2026-10-08", "2026-10-12", "2027-10-12")],
            "payments": [],
        }

    def test_identical_inputs_reuse_version(self):
        fx.write_source(self.source_dir, self._rows())
        first = build(self.source_dir, self.output_dir, note="报告 A")
        second = build(self.source_dir, self.output_dir, note="报告 B")
        self.assertTrue(second.reused)
        self.assertEqual(first.version.version_id, second.version.version_id)
        self.assertEqual(len(list_versions(self.output_dir)), 1)

    def test_correction_creates_new_version_but_old_remains_reviewable(self):
        # 版本 1：安全验收 10-20，10-12 起租属于提前供给
        fx.write_source(self.source_dir, self._rows(safety_from="2026-10-20"))
        v1 = build(self.source_dir, self.output_dir, note="后补验收前")
        with open_version(self.output_dir, v1.version.version_id) as q:
            supply_before = q.unit_supply(date.fromisoformat("2026-10-12"))[0]
            self.assertEqual(supply_before["rentable_area_sqm"], "0.0000")

        # 版本 2：来源更正为安全验收 10-10（有依据地改变资格期间）
        fx.write_source(self.source_dir, self._rows(safety_from="2026-10-10"))
        v2 = build(self.source_dir, self.output_dir, note="后补验收后")
        self.assertFalse(v2.reused)
        self.assertNotEqual(v1.version.version_id, v2.version.version_id)
        self.assertEqual(len(list_versions(self.output_dir)), 2)

        # 旧版本仍可按版本号复核，结论不因新资料而改变
        with open_version(self.output_dir, v1.version.version_id) as q:
            old = q.unit_supply(date.fromisoformat("2026-10-12"))[0]
            self.assertEqual(old["rentable_area_sqm"], "0.0000")
            self.assertEqual(q.meta()["note"], "后补验收前")
            # 原始层仍可回溯到具体来源记录
            raw = q.raw_record("qualifications", 1)
            self.assertEqual(raw["proof_id"], "Q2")
        with open_version(self.output_dir, v2.version.version_id) as q:
            new = q.unit_supply(date.fromisoformat("2026-10-12"))[0]
            self.assertEqual(new["rentable_area_sqm"], "100.0000")

    def test_tampered_source_is_rejected(self):
        fx.write_source(self.source_dir, self._rows())
        build(self.source_dir, self.output_dir)
        # 篡改资料字节但不更新 manifest：接入层必须拒绝
        ndjson = self.source_dir / "homes.ndjson"
        ndjson.write_text(ndjson.read_text(encoding="utf-8").replace("100.00", "120.00"),
                          encoding="utf-8")
        with self.assertRaises(ValueError):
            build(self.source_dir, self.output_dir)


if __name__ == "__main__":
    unittest.main()
