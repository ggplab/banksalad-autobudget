#!/usr/bin/env python3
"""적재(ingest) + 태깅(tagger) 파이프라인 통합 테스트 — 샘플 xlsx 사용.

`.venv/bin/python -m unittest discover -s tests` 로 돈다.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "samples"))

import banksalad_ingest  # noqa: E402
import expense_tagger  # noqa: E402
import make_sample_export  # noqa: E402


class TestIngestAndTagging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.xlsx_path = Path(cls.tmpdir.name) / "sample-export.xlsx"
        if not cls.xlsx_path.exists():
            rows = make_sample_export.build_rows()
            make_sample_export.write_xlsx(rows, cls.xlsx_path)
        cls.db_path = Path(cls.tmpdir.name) / "budget.db"

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_double_ingest_is_idempotent_and_preserves_dup_rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(banksalad_ingest.SCHEMA)

        total1, new1, upd1 = banksalad_ingest.ingest(self.xlsx_path, conn)
        self.assertGreater(total1, 0)
        self.assertEqual(new1, total1)

        total2, new2, upd2 = banksalad_ingest.ingest(self.xlsx_path, conn)
        self.assertEqual(total2, total1)
        self.assertEqual(new2, 0)

        # 지하철 -1,550원 3행이 발생순번(#1 #2 #3)으로 모두 보존됐는지 확인
        subway_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE content='지하철' AND amount=-1550 "
            "AND date='2026-03-03' AND time='08:15:32'").fetchall()]
        self.assertEqual(len(subway_ids), 3)
        suffixes = sorted(i.split("#")[1] for i in subway_ids)
        self.assertEqual(suffixes, ["1", "2", "3"])

        # 환불 행(지출 타입 양수)이 존재하는지 확인
        refund = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE type='지출' AND amount > 0").fetchone()[0]
        self.assertGreater(refund, 0)

        conn.close()

    def test_tagging_pipeline(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(banksalad_ingest.SCHEMA)
        banksalad_ingest.ingest(self.xlsx_path, conn)
        expense_tagger.ensure_schema(conn)

        expense_tagger.apply_merchant_rules(conn)
        expense_tagger.apply_method_defaults(conn)
        expense_tagger.apply_banksalad_fallback(conn)
        expense_tagger.apply_overrides(conn)

        # 모든 지출 행에 purpose가 채워졌는지
        missing = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE type='지출' AND purpose IS NULL").fetchone()[0]
        self.assertEqual(missing, 0)

        # 오버라이드 대상 행 (2026-03-14 / -128000 / NHNKCP)
        override_row = conn.execute(
            "SELECT purpose, attribution, purpose_source, attrib_source FROM transactions "
            "WHERE date='2026-03-14' AND amount=-128000 AND content LIKE '%NHNKCP%'").fetchone()
        self.assertIsNotNone(override_row)
        purpose, attribution, purpose_source, attrib_source = override_row
        self.assertEqual(purpose, "사업-접대")
        self.assertEqual(attribution, "사업")
        self.assertEqual(purpose_source, "override")
        self.assertEqual(attrib_source, "override")

        # 결제수단 "모임통장" (attribution.example.yaml methods에 없음) → default 귀속
        cfg = expense_tagger.load_attribution()
        joint_account_attrib = conn.execute(
            "SELECT attribution FROM transactions WHERE method='모임통장'").fetchone()
        self.assertIsNotNone(joint_account_attrib)
        self.assertEqual(joint_account_attrib[0], cfg["default"])

        conn.close()


if __name__ == "__main__":
    unittest.main()
