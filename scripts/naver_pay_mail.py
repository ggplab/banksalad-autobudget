#!/usr/bin/env python3
"""네이버페이 주문메일 파서 — 뱅샐 거래에 실제 결제처·품목을 붙인다.

    python3 scripts/naver_pay_mail.py --sample 5   # 파싱 표본 확인
    python3 scripts/naver_pay_mail.py --stats      # 전체 파싱 통계
    python3 scripts/naver_pay_mail.py --match      # DB 거래에 증빙 붙이기

뱅샐 카드전표에는 `네이버페이`·`NHNKCP` 같은 PG 표기만 찍혀서 무엇을 샀는지 알 수 없다.
네이버페이 주문메일에는 **실제 결제처와 품목**이 있으므로 이걸 증빙으로 끌어온다.

## 실측으로 확인한 함정 (전부 여기서 처리한다)

1. **발신 도메인은 `navercorp.com`이다.** `pay.naver.com`·`naver.com`으로 찾으면 0건이라
   "메일 없음"으로 오판한다.
2. **본문이 HTML 전용.** `text/plain`은 "네이버페이 - 결제완료_일반결제" 한 줄짜리
   스텁이라, plain 우선인 `gmail_client.body_of()`가 HTML로 넘어가지 않는다.
3. **Date 헤더가 UTC다.** 헤더에서 날짜를 뽑으면 하루 밀린다. 본문의 결제일자/주문일자를 앵커로 쓴다.
4. **한 주문에 메일이 2~3통 온다** (결제 → 구매확정요청 → 자동구매확정).
   주문번호로 dedup하지 않으면 같은 거래를 두 번 소진한다.
5. **템플릿이 13종+.** 결제·확정 계열만 고르고 취소·발송지연·예약구매·약관안내는 버린다.
6. **금액이 결제수단별로 갈린다.** `신용카드(5개월) 100,600원` / `네이버페이 머니 사용 12,788원` /
   `네이버페이 포인트 사용 312원`. 뱅샐은 이걸 **별도 거래 행으로 쪼개므로**, 메일 1통이 거래 N건(최대 3)에
   대응한다. 1:1 소진을 쓰면 형제 행이 영원히 못 붙는다.
"""

from __future__ import annotations

import argparse
import base64
import html as H
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date as D, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import gmail_client as g  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BUDGET_DB") or REPO / "data" / "budget.db")

# 결제상세를 담은 템플릿만. 취소·발송지연·예약구매·약관안내는 금액 구조가 다르거나 없다.
WANTED_SUBJECT = re.compile(r"결제하신 내역|구매를 확정|자동구매확정")
QUERY = "from:navercorp.com after:{after} before:{before}"

# 결제수단 라인 판정 — 이게 매칭 앵커다. '주문금액'이 아니다.
# 라벨을 열거하지 않고(하나 빠지면 그 메일이 통째로 실패), 라벨-금액 쌍을 전부 훑은 뒤 결제수단이 아닌 것만 뺀다.
PAY_KEYWORDS = re.compile(r"카드|계좌|머니|포인트|간편결제|무통장|휴대폰|페이|상품권")
NOT_PAYMENT = re.compile(r"주문금액|상품금액|최종결제금액|배송비|수량|할인|적립|쿠폰|합계|공급가|부가세")
AMOUNT_ONLY = re.compile(r"^([\d,]+)\s*원?$")

EVIDENCE_COLUMNS = [
    ("naver_merchant", "TEXT"),     # 메일의 실제 결제처
    ("naver_items", "TEXT"),        # 품목명
    ("naver_order_no", "TEXT"),
    ("naver_confidence", "TEXT"),   # matched | ambiguous
]


def _payment_lines(seg: str) -> list[tuple[str, int]]:
    """결제상세 구간에서 (결제수단, 금액) 쌍을 뽑는다.

    템플릿마다 줄바꿈이 다르다 — `카드 간편결제 / 348,320원`처럼 금액과 '원'이 붙기도 하고
    `신용카드(5개월) / 100,600 / 원`처럼 갈리기도 한다. 줄을 걸으며 '라벨 다음에 오는 첫 금액'을 짝지운다.
    """
    lines = [ln.strip() for ln in seg.splitlines() if ln.strip()]
    out: list[tuple[str, int]] = []
    label = None
    for ln in lines:
        m = AMOUNT_ONLY.match(ln)
        if m:
            if label:
                out.append((label, int(m.group(1).replace(",", ""))))
                label = None
            continue
        if ln == "원":
            continue
        if NOT_PAYMENT.search(ln):
            label = None
            continue
        label = ln if PAY_KEYWORDS.search(ln) else None
    return out


@dataclass
class NaverOrder:
    msg_id: str
    order_no: str
    date: str                  # YYYY-MM-DD
    merchant: str
    items: str
    amounts: list[tuple[str, int]] = field(default_factory=list)  # (수단, 금액)
    subject: str = ""

    @property
    def total(self) -> int:
        return sum(a for _, a in self.amounts)


def html_to_text(htm: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", htm, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = H.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def html_body(msg_id: str) -> str:
    """HTML 파트를 강제로 골라 텍스트화한다 (plain 은 한 줄 스텁이라 쓸 수 없다)."""
    d = g.api_get(f"/users/me/messages/{msg_id}", {"format": "full"})
    for part in g._walk_parts(d.get("payload", {})):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return html_to_text(base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace"))
    return ""


def _after(text: str, label: str, maxlen: int = 120) -> str:
    """라벨 다음 줄의 값. 네이버 메일은 라벨과 값이 줄로 갈린다."""
    m = re.search(rf"^\s*{label}\s*\n\s*(.+)$", text, re.M)
    return m.group(1).strip()[:maxlen] if m else ""


def parse_text(text: str, msg_id: str = "", subject: str = "") -> NaverOrder | None:
    """텍스트화된 본문 → NaverOrder. (테스트에서 직접 부른다)"""
    if not text:
        return None
    order_no = _after(text, "결제번호") or _after(text, "주문번호")
    raw_date = _after(text, "결제일자") or _after(text, "주문일자")
    m = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", raw_date)
    if not (order_no and m):
        return None
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    merchant = _after(text, "결제처", 60)
    # 품목 라벨이 템플릿마다 다르다. 하나만 보면 조용히 빈 값이 된다.
    items = ""
    for label in ("상품정보", "발송상품", "주문상품", "상품명"):
        items = _after(text, label, 200)
        if items:
            break
    # 결제상세 구간에서만 금액을 뽑는다 — 바깥에도 '주문금액'이 또 나온다.
    seg = text
    i = text.find("결제상세")
    if i >= 0:
        seg = text[i:i + 900]
    amounts = [(m_, a) for m_, a in _payment_lines(seg) if a > 0]
    seen, uniq = set(), []
    for meth, amt in amounts:
        if (meth, amt) in seen:
            continue
        seen.add((meth, amt))
        uniq.append((meth, amt))
    if not uniq:
        return None
    return NaverOrder(msg_id, order_no, date, merchant, items, uniq, subject)


def parse(msg_id: str, subject: str) -> NaverOrder | None:
    return parse_text(html_body(msg_id), msg_id, subject)


def fetch_all(after: str, before: str, limit: int = 1000) -> list[NaverOrder]:
    g.use("orders")
    ids = g.search(QUERY.format(after=after, before=before), limit=limit)
    orders: dict[str, NaverOrder] = {}
    skipped = parse_fail = 0
    for i, mid in enumerate(ids):
        if i % 100 == 0 and i:
            print(f"  파싱 {i}/{len(ids)} · 주문 {len(orders)}건", file=sys.stderr)
        subj = g.headers_of(mid, ("Subject",)).get("Subject", "")
        if not WANTED_SUBJECT.search(subj):
            skipped += 1
            continue
        o = parse(mid, subj)
        if o is None:
            parse_fail += 1
            continue
        # 주문번호 dedup — 결제처가 있는 쪽(결제완료 템플릿)을 우선 보존한다.
        prev = orders.get(o.order_no)
        if prev is None or (not prev.merchant and o.merchant):
            orders[o.order_no] = o
    print(f"  메일 {len(ids)} · 대상 외 {skipped} · 파싱 실패 {parse_fail} · 고유 주문 {len(orders)}", file=sys.stderr)
    return list(orders.values())


def match_orders(conn: sqlite3.Connection, orders: list[NaverOrder], window: int = 3) -> dict:
    """주문 목록을 DB 거래에 붙인다 (메일 1통 : 거래 N건). 테스트에서 직접 부른다."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col, typ in EVIDENCE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {typ}")
    conn.commit()
    rows = conn.execute("SELECT id, date, content, amount FROM transactions "
                        "WHERE type='지출' AND amount < 0 ORDER BY date").fetchall()
    # (금액 → 그 금액을 가진 (주문, 수단) 목록). 가맹점 문자열은 키에 안 쓴다 — PG 표기가 절반뿐이다.
    idx: dict[int, list] = {}
    for o in orders:
        for meth, amt in o.amounts:
            idx.setdefault(amt, []).append((o, meth))
    matched = ambiguous = 0
    used: set[tuple[str, str]] = set()
    updates = []
    for tx_id, tx_date, _content, amount in rows:
        cands = []
        for o, meth in idx.get(-amount, []):
            if (o.order_no, meth) in used:
                continue
            try:
                if abs((D.fromisoformat(o.date) - D.fromisoformat(tx_date)).days) <= window:
                    cands.append((o, meth))
            except ValueError:
                continue
        if not cands:
            continue
        if len(cands) > 1:
            ambiguous += 1
            updates.append((tx_id, "", "", "", "ambiguous"))
            continue
        o, meth = cands[0]
        used.add((o.order_no, meth))
        matched += 1
        updates.append((tx_id, o.merchant, o.items, o.order_no, "matched"))
    conn.executemany("UPDATE transactions SET naver_merchant=?, naver_items=?, naver_order_no=?, "
                     "naver_confidence=? WHERE id=?", [(m, i, no, c, tid) for tid, m, i, no, c in updates])
    conn.commit()
    return {"orders": len(orders), "matched": matched, "ambiguous": ambiguous}


def match_to_db(db: Path, window: int = 3, after: str | None = None, before: str | None = None) -> dict:
    """메일을 거래에 붙인다. **판정은 하지 않는다 — 증빙만 채운다.**

    귀속을 여기서 자동으로 쓰지 않는 이유: 자동 추정이 attribution을 덮으면 사람이 확정한 것과 구분이 안 된다.
    """
    conn = sqlite3.connect(db)
    if after is None or before is None:
        lo, hi = conn.execute("SELECT min(date), max(date) FROM transactions").fetchone()
        after = after or (D.fromisoformat(lo) - timedelta(days=3)).strftime("%Y/%m/%d")
        before = before or (D.fromisoformat(hi) + timedelta(days=1)).strftime("%Y/%m/%d")
    print(f"메일 검색 범위: {after} ~ {before}", file=sys.stderr)
    orders = fetch_all(after=after, before=before)
    r = match_orders(conn, orders, window)
    conn.close()
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--match", action="store_true", help="거래에 증빙 붙이기")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--after", help="Gmail after: (YYYY/MM/DD) — 기본 DB 최소일-3일")
    ap.add_argument("--before", help="Gmail before: (YYYY/MM/DD) — 기본 DB 최대일+1일")
    args = ap.parse_args()
    g.use("orders")
    if args.match:
        r = match_to_db(args.db, after=args.after, before=args.before)
        print(f"\n주문 {r['orders']}건 → 매칭 {r['matched']}건 · 모호 {r['ambiguous']}건")
        return 0
    if args.sample or args.stats:
        conn = sqlite3.connect(args.db)
        lo, hi = conn.execute("SELECT min(date), max(date) FROM transactions").fetchone()
        conn.close()
        after = args.after or (D.fromisoformat(lo) - timedelta(days=3)).strftime("%Y/%m/%d")
        before = args.before or (D.fromisoformat(hi) + timedelta(days=1)).strftime("%Y/%m/%d")
        if args.sample:
            shown = 0
            for mid in g.search(QUERY.format(after=after, before=before), limit=60):
                subj = g.headers_of(mid, ("Subject",)).get("Subject", "")
                if not WANTED_SUBJECT.search(subj):
                    continue
                o = parse(mid, subj)
                print("=" * 66)
                if o is None:
                    print(f"파싱 실패 · {subj[:40]}")
                else:
                    print(f"{o.date} · 주문 {o.order_no}\n  결제처: {o.merchant or '(없음)'}\n"
                          f"  품목  : {o.items[:70] or '(없음)'}\n  금액  : {o.amounts}  합 {o.total:,}")
                shown += 1
                if shown >= args.sample:
                    break
            return 0
        orders = fetch_all(after, before)
        with_m = sum(1 for o in orders if o.merchant)
        multi = sum(1 for o in orders if len(o.amounts) > 1)
        print(f"\n고유 주문 {len(orders)}건\n  결제처 있음 {with_m}건\n  결제수단 2개 이상 {multi}건\n"
              f"  금액 합계 {sum(o.total for o in orders):,}원")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
