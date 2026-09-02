#!/usr/bin/env python3
"""메일 파서 단위 테스트 — 전부 가공 픽스처(실제 개인정보 없음).

`.venv/bin/python -m unittest discover -s tests` 로 돈다.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import banksalad_ingest  # noqa: E402
import expense_tagger  # noqa: E402
import naver_pay_mail  # noqa: E402


class TestCoupangParser(unittest.TestCase):
    def test_parse_coupang_email(self):
        text = (
            "구매 상세내역\n"
            "판매자 샘플몰 두루마리 휴지 30롤\n12,900원\n"
            "쿠팡와우카드 / 일시불 12,900원\n"
            "총 결제금액 12,900원\n"
        )
        result = expense_tagger.parse_coupang_email(text)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertEqual(result["card_amount"], 12900)


class TestCoupangEatsParser(unittest.TestCase):
    def test_parse_coupang_eats_email(self):
        text = (
            "주문상품명\n"
            "김밥천국 참치김밥 외 1\n"
            "결제금액\n"
            "13,500원\n"
            "결제일시\n"
            "2026년 02월 03일 19시 30분\n"
        )
        result = expense_tagger.parse_coupang_eats_email(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["card_amount"], 13500)
        self.assertEqual(result["paid_at"], "2026-02-03")


class TestCoupangMembershipParser(unittest.TestCase):
    def test_parse_coupang_membership_email(self):
        text = (
            "쿠팡 와우 멤버십 월회비 안내\n"
            "결제금액 7,890원\n"
            "적용기간 2026. 02. 01 ~ 2026. 02. 28\n"
        )
        result = expense_tagger.parse_coupang_membership_email(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["card_amount"], 7890)
        self.assertEqual(result["paid_at"], "2026-02-01")


class TestKurlyParser(unittest.TestCase):
    def test_parse_kurly_email(self):
        text = (
            "주문번호 :  1234567890\n"
            "결제금액 :  34,217원\n"
            "· 결제일시  2026-02-17 17:31:14\n"
            "구매상품 정보\n"
            "[샘플] 두부 300g\n"
            "2개\n"
            "[샘플] 계란 10구\n"
            "1개\n"
            "상품금액 32,000원\n"
        )
        result = expense_tagger.parse_kurly_email(text)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["items"][0].endswith("×2"))
        self.assertEqual(result["card_amount"], 34217)
        self.assertEqual(result["paid_at"], "2026-02-17")


class TestKurlyMembershipParser(unittest.TestCase):
    def test_parse_kurly_membership_email(self):
        text = (
            "컬리멤버스 정기결제 안내\n"
            "결제일 2026-05-01\n"
            "결제금액 1,900원\n"
        )
        result = expense_tagger.parse_kurly_membership_email(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["card_amount"], 1900)
        self.assertEqual(result["paid_at"], "2026-05-01")


class TestNaverPayParser(unittest.TestCase):
    def test_parse_text(self):
        text = (
            "결제번호\n"
            "20260210ABC\n"
            "결제일자\n"
            "2026.02.10\n"
            "결제처\n"
            "샘플스토어\n"
            "상품정보\n"
            "무선 마우스\n"
            "결제상세\n"
            "주문금액\n"
            "30,000원\n"
            "카드 간편결제\n"
            "25,000원\n"
            "네이버페이 포인트 사용\n"
            "5,000원\n"
        )
        order = naver_pay_mail.parse_text(text)
        self.assertIsNotNone(order)
        self.assertEqual(order.amounts, [("카드 간편결제", 25000), ("네이버페이 포인트 사용", 5000)])
        self.assertEqual(order.total, 30000)
        methods = [m for m, _ in order.amounts]
        self.assertNotIn("주문금액", methods)

    def test_match_orders(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(banksalad_ingest.SCHEMA)
        now = "2026-02-10T00:00:00+09:00"
        conn.executemany(
            """INSERT INTO transactions
               (id,date,time,type,category,subcategory,content,amount,currency,method,memo,
                source_file,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("tx1", "2026-02-10", "09:00:00", "지출", "생활", "", "네이버페이", -25000,
                 "KRW", "간편결제(포인트)", "", "test.xlsx", now, now),
                ("tx2", "2026-02-10", "09:00:01", "지출", "생활", "", "네이버페이", -5000,
                 "KRW", "간편결제(포인트)", "", "test.xlsx", now, now),
            ],
        )
        conn.commit()

        order = naver_pay_mail.NaverOrder(
            msg_id="m1", order_no="20260210ABC", date="2026-02-10",
            merchant="샘플스토어", items="무선 마우스",
            amounts=[("카드 간편결제", 25000), ("네이버페이 포인트 사용", 5000)],
        )
        result = naver_pay_mail.match_orders(conn, [order])
        self.assertEqual(result["matched"], 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
