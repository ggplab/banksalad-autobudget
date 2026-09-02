#!/usr/bin/env python3
"""지출 태깅 — 뱅샐 DB의 지출 거래에 용도(purpose)·귀속(attribution) 태그를 부여한다.

신호 우선순위(필드별):
  용도:   건별 오버라이드 > 쿠팡 품목 > 가맹점 룰 > LLM 제안 > 뱅샐 분류 매핑
  귀속:   건별 오버라이드 > 가맹점 룰 > 결제수단 기본값 > '불명'

사용법:
  python3 scripts/expense_tagger.py schema      # DB에 태깅 컬럼 추가
  python3 scripts/expense_tagger.py coupang     # 쿠팡 메일 파싱 + 매칭 (Gmail 토큰 필요)
  python3 scripts/expense_tagger.py kurly       # 컬리 메일 파싱 + 매칭 (Gmail 토큰 필요)
  python3 scripts/expense_tagger.py rules       # 가맹점 룰 적용
  python3 scripts/expense_tagger.py method      # 결제수단 기본 귀속
  python3 scripts/expense_tagger.py overrides   # 건별 오버라이드
  python3 scripts/expense_tagger.py apply       # 우선순위 최종 적용
  python3 scripts/expense_tagger.py llm         # 뱅샐 fallback 행만 LLM 재추론
  python3 scripts/expense_tagger.py stats       # 태깅 현황 출력

실행 순서: rules → apply → llm → (필요시 재-apply, 안전)
  llm 단계의 대상(purpose_source='banksalad')은 apply 안의 뱅샐 fallback이 만든다.
  그래서 llm은 apply '앞'이 아니라 '뒤'다. 재-apply해도 llm 결과는 살아남는다 —
  룰은 매칭될 때만 덮고(룰 > llm, 문서화된 우선순위대로), 뱅샐 fallback은
  purpose IS NULL만 채우기 때문이다.

설정 파일(rules/):
  merchant_rules.yaml          가맹점 정규식 → 용도·귀속 (없으면 *.example.yaml 을 읽는다)
  transaction-overrides.yaml   건별 판정 (date+amount+content 키)
  attribution.yaml             결제수단 → 귀속 기본값, 귀속 라벨 목록
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BUDGET_DB") or REPO / "data" / "budget.db")
RULES_DIR = Path(os.environ.get("BUDGET_RULES_DIR") or REPO / "rules")


def rules_file(name: str) -> Path:
    """rules/<name>.yaml 이 있으면 그것, 없으면 rules/<name>.example.yaml (샘플 설정으로 돌아간다)."""
    real = RULES_DIR / f"{name}.yaml"
    return real if real.exists() else RULES_DIR / f"{name}.example.yaml"


RULES_PATH = rules_file("merchant_rules")
OVERRIDES_PATH = rules_file("transaction-overrides")
ATTRIBUTION_PATH = rules_file("attribution")

# --------------------------------------------------------------------------- schema

TAGGING_COLUMNS = [
    ("purpose", "TEXT"),          # 용도 16분류
    ("attribution", "TEXT"),      # 귀속: rules/attribution.yaml 의 labels 중 하나
    ("purpose_source", "TEXT"),   # 근거: coupang/rule/llm/banksalad-sub/banksalad/override
    ("attrib_source", "TEXT"),    # 근거: rule/method/override
    ("coupang_items", "TEXT"),    # 쿠팡 품목명 (;로 이어붙임)
    ("coupang_confidence", "TEXT"),  # matched/matched-eats/matched-membership
    ("kurly_items", "TEXT"),      # 컬리 품목명 (;로 이어붙임)
    ("kurly_confidence", "TEXT"),  # matched/matched-membership
    ("llm_confidence", "REAL"),   # LLM 용도 추론 확신도 0.0~1.0 (purpose_source='llm'일 때만)
]

PURPOSES = [
    "육아", "식비", "생활용품", "주거·공과", "의료", "교육·도서",
    "교통·차량", "문화·여가", "의류·미용", "경조사",
    "사업-장비", "사업-SaaS", "사업-접대", "사업-외주",
    "금융·이체", "기타",
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col, typ in TAGGING_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {typ}")
            print(f"  컬럼 추가: {col} {typ}")
    conn.commit()


# --------------------------------------------------------------------------- 경량 yaml 파서
# PyYAML 없이 돈다 — 이 프로젝트의 설정 파일 형식(리스트 of 매핑, 2단 중첩)에만 맞춘다.

def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #", 1)[0].strip() if not v.startswith("#") else ""


def load_simple_yaml(path: Path) -> dict:
    """`key: value` 와 `key:` 아래 `  sub: value` 2단 매핑만 읽는다 (attribution.yaml 용)."""
    out: dict = {}
    cur_key = None
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            k, _, v = line.partition(":")
            k = k.strip()
            v = _strip_quotes(v)
            if v.startswith("[") and v.endswith("]"):
                out[k] = [_strip_quotes(x) for x in v[1:-1].split(",") if x.strip()]
                cur_key = None
            elif v:
                out[k] = v
                cur_key = None
            else:
                out[k] = {}
                cur_key = k
        elif cur_key is not None:
            if line.startswith("- "):
                if not isinstance(out[cur_key], list):
                    out[cur_key] = []
                out[cur_key].append(_strip_quotes(line[2:]))
            else:
                k, _, v = line.partition(":")
                out[cur_key][_strip_quotes(k)] = _strip_quotes(v)
    return out


# --------------------------------------------------------------------------- attribution config

def load_attribution() -> dict:
    """rules/attribution.yaml → {"labels": [...], "default": str, "methods": {결제수단: 라벨}}"""
    d = load_simple_yaml(ATTRIBUTION_PATH)
    labels = d.get("labels") or ["개인", "사업", "불명"]
    return {
        "labels": list(labels),
        "default": d.get("default") or labels[-1],
        "methods": dict(d.get("methods") or {}),
    }


# --------------------------------------------------------------------------- mail search range

def _mail_date_range(conn: sqlite3.Connection) -> tuple[str, str]:
    """거래 DB 날짜 범위로 Gmail 검색용 after/before를 만든다 (YYYY/MM/DD).

    쿠팡·컬리 메일 검색 둘 다 이 범위를 쓴다 — 하드코딩된 날짜 리터럴 대신
    DB의 min(date)-3일 ~ max(date)+1일(Gmail before는 배타적)을 실측한다.
    """
    row = conn.execute("SELECT min(date), max(date) FROM transactions").fetchone()
    min_date, max_date = row[0], row[1]
    if not min_date or not max_date:
        raise ValueError("transactions 테이블에 date가 없어 메일 검색 범위를 만들 수 없음")
    after = (datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y/%m/%d")
    before = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y/%m/%d")
    return after, before


def _gmail():
    sys.path.insert(0, str(REPO / "scripts"))
    import gmail_client as g
    g.use("orders")
    return g


# --------------------------------------------------------------------------- coupang

def parse_coupang_email(text: str) -> dict | None:
    """쿠팡 주문확인 메일 본문에서 품목명과 카드 결제액을 추출한다.

    Returns: {items: [str], card_amount: int, total_amount: int}
    """
    items = []
    # "구매 상세내역" 헤더로 섹션을 나누고, "판매자" 다음 첫 가격 앞까지가 품목명이다
    sections = re.split(r'구매 상세내역', text)
    for section in sections[1:]:
        m = re.search(r'판매자\s+(.+?)\s+[\d,]+원', section, re.S)
        if m:
            item_name = re.sub(r'\s+', ' ', m.group(1)).strip()
            if item_name and len(item_name) > 2:
                items.append(item_name)
    if not items:
        return None

    # 카드 결제액 (총액이 아니라 카드로 나간 금액 — 쿠팡캐시 차감분을 빼야 뱅샐 행과 맞는다)
    card_amount = None
    card_patterns = [
        r'(?:쿠팡와우카드|KB국민|쿠페이|현대카드|신한|비씨|삼성|롯데|하나|우리|농협)[^\n]*?/\s*일시불\s+[\s\n]*([\d,]+)\s*원',
        r'(?:쿠팡와우카드|KB국민|쿠페이|현대카드)[^\n]*?\s+([\d,]+)\s*원',
    ]
    for pat in card_patterns:
        m = re.search(pat, text)
        if m:
            card_amount = int(m.group(1).replace(',', ''))
            break
    if card_amount is None:
        m = re.search(r'일시불\s+[\s\n]*([\d,]+)\s*원', text)
        if m:
            card_amount = int(m.group(1).replace(',', ''))

    total_amount = None
    m = re.search(r'총\s*결제금액\s+[\s\n]*([\d,]+)\s*원', text)
    if m:
        total_amount = int(m.group(1).replace(',', ''))
    if card_amount is None and total_amount is not None and '쿠팡캐시' not in text:
        card_amount = total_amount
    if card_amount is None:
        m = re.search(r'결제금액\s+[\s\n]*([\d,]+)\s*원\s*쿠팡', text)
        if m:
            card_amount = int(m.group(1).replace(',', ''))

    return {"items": items, "card_amount": card_amount, "total_amount": total_amount}


_MONTHS = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
           "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}


def _header_date(date_str: str) -> str | None:
    """`Sat, 25 Jul 2026 19:39:10 +0900` → 2026-07-25"""
    m = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})', date_str or "")
    return f"{m.group(3)}-{_MONTHS[m.group(2)]}-{int(m.group(1)):02d}" if m else None


def match_coupang(conn: sqlite3.Connection) -> dict:
    """쿠팡 주문메일을 파싱해 DB 거래와 매칭한다."""
    g = _gmail()
    after, before = _mail_date_range(conn)
    query = f'from:coupang.com subject:"주문하신 내역" after:{after} before:{before}'
    print(f"쿠팡 메일 검색: {query}")
    msg_ids = g.search(query, limit=1000)
    print(f"  메일 {len(msg_ids)}건 발견")

    parsed, parse_fail = [], 0
    for i, mid in enumerate(msg_ids):
        if i % 50 == 0 and i > 0:
            print(f"  파싱 중... {i}/{len(msg_ids)}")
        hdr, text = g.body_of(mid)
        result = parse_coupang_email(text)
        if result is None:
            parse_fail += 1
            continue
        result["paid_at"] = _header_date(hdr.get("Date", ""))
        result["msg_id"] = mid
        parsed.append(result)
    print(f"  파싱 성공: {len(parsed)}건, 실패: {parse_fail}건")

    txs = conn.execute(
        "SELECT id, date, amount, content, coupang_items FROM transactions "
        "WHERE type='지출' AND amount < 0 AND content LIKE '%쿠팡%'"
    ).fetchall()
    print(f"  DB 쿠팡 지출(환불 제외): {len(txs)}건")
    stats = _match_parsed_emails(conn, parsed, txs, "coupang_items", "coupang_confidence", "matched")
    result = {"emails_total": len(msg_ids), "parsed": len(parsed), "parse_fail": parse_fail, **stats,
              "match_rate": f"{stats['matched']/len(msg_ids)*100:.1f}%" if msg_ids else "N/A"}
    print("\n쿠팡 매칭 결과:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def _parse_flexible_date(s: str | None) -> str | None:
    """`2026년 08월 12일` / `2026. 08. 13` / `2026-08-24` 세 형식을 YYYY-MM-DD로 통일."""
    if not s:
        return None
    for pat in (r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', r'(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})',
                r'(\d{4})-(\d{1,2})-(\d{1,2})'):
        m = re.search(pat, s)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _label_next_line(lines: list[str], label: str) -> str | None:
    """`라벨\\n값` 형식(NHN KCP 영수증 메일 등)에서 라벨 다음 첫 비어있지 않은 줄을 값으로 뽑는다."""
    for i, line in enumerate(lines):
        if line.strip() == label:
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return lines[j].strip()
    return None


def parse_coupang_eats_email(text: str) -> dict | None:
    """쿠팡이츠 NHN KCP 영수증 메일 본문에서 주문상품명·결제금액·결제일시를 추출한다.

    라벨 줄 다음 줄이 값인 포맷(표 형태 텍스트 추출 결과)이라 정규식보다
    줄 단위 라벨-다음줄 매칭이 안전하다.
    """
    if "주문상품명" not in text or "결제금액" not in text:
        return None
    lines = text.splitlines()
    item = _label_next_line(lines, "주문상품명")
    amount_str = _label_next_line(lines, "결제금액")
    if not item or not amount_str:
        return None
    m = re.search(r'[\d,]+', amount_str)
    if not m:
        return None
    return {
        "items": [item],
        "card_amount": int(m.group(0).replace(',', '')),
        "paid_at": _parse_flexible_date(_label_next_line(lines, "결제일시")),
        "order_no": _label_next_line(lines, "주문번호"),
    }


def parse_coupang_membership_email(text: str) -> dict | None:
    """쿠팡 와우 멤버십 월회비 메일 본문에서 결제금액·적용기간 시작일을 추출한다."""
    m_amt = re.search(r'결제금액\s*[:\s]*([\d,]+)\s*원', text)
    m_period = re.search(r'적용기간\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})', text)
    if not m_amt or not m_period:
        return None
    return {
        "items": ["와우 멤버십 월회비 (구독료)"],
        "card_amount": int(m_amt.group(1).replace(',', '')),
        "paid_at": f"{m_period.group(1)}-{int(m_period.group(2)):02d}-{int(m_period.group(3)):02d}",
    }


def _match_parsed_emails(conn, parsed: list[dict], txs: list[tuple],
                         items_col: str, confidence_col: str, confidence_value: str) -> dict:
    """파싱된 메일 목록을 후보 거래와 매칭해 items_col/confidence_col을 채운다.

    (메일 날짜 ±3일, |amount| == card_amount 정확 일치, 1:1).
    `txs`의 마지막 컬럼은 기존 items_col 값이다 — 이미 채워진 행은 후보에서 제외해
    재실행해도 멱등하다(덮어쓰지 않는다).
    """
    candidates_all = [(tx_id, tx_date, tx_amount)
                      for tx_id, tx_date, tx_amount, _content, existing in txs if not existing]
    matched = ambiguous = unmatched = 0
    used: set = set()
    for p in parsed:
        if p.get("card_amount") is None or p.get("paid_at") is None:
            unmatched += 1
            continue
        target = datetime.strptime(p["paid_at"], "%Y-%m-%d")
        cands = [tx_id for tx_id, tx_date, tx_amount in candidates_all
                 if tx_id not in used
                 and abs(datetime.strptime(tx_date, "%Y-%m-%d") - target).days <= 3
                 and abs(tx_amount) == p["card_amount"]]
        if len(cands) == 1:
            used.add(cands[0])
            conn.execute(f"UPDATE transactions SET {items_col}=?, {confidence_col}=? WHERE id=?",
                         ("; ".join(p["items"]), confidence_value, cands[0]))
            matched += 1
        elif len(cands) > 1:
            ambiguous += 1
        else:
            unmatched += 1
    conn.commit()
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


def _match_mail_kind(conn, query: str, parser, like: str, items_col: str, conf_col: str,
                     conf_value: str, title: str) -> dict:
    g = _gmail()
    after, before = _mail_date_range(conn)
    q = f"{query} after:{after} before:{before}"
    print(f"{title} 메일 검색: {q}")
    msg_ids = g.search(q, limit=1000)
    print(f"  메일 {len(msg_ids)}건 발견")
    parsed, parse_fail = [], 0
    for mid in msg_ids:
        hdr, text = g.body_of(mid)
        r = parser(text)
        if r is None:
            parse_fail += 1
            continue
        if not r.get("paid_at"):
            r["paid_at"] = _header_date(hdr.get("Date", ""))
        r["msg_id"] = mid
        parsed.append(r)
    print(f"  파싱 성공: {len(parsed)}건, 실패: {parse_fail}건")
    txs = conn.execute(
        f"SELECT id, date, amount, content, {items_col} FROM transactions "
        f"WHERE type='지출' AND amount < 0 AND content LIKE ?", (like,)).fetchall()
    print(f"  DB 후보 지출: {len(txs)}건")
    stats = _match_parsed_emails(conn, parsed, txs, items_col, conf_col, conf_value)
    result = {"emails_total": len(msg_ids), "parsed": len(parsed), "parse_fail": parse_fail, **stats}
    print(f"\n{title} 매칭 결과:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def match_coupang_eats(conn: sqlite3.Connection) -> dict:
    """쿠팡이츠(NHN KCP) 결제 영수증 메일을 파싱해 DB 거래와 매칭한다."""
    return _match_mail_kind(conn, 'from:kcp.co.kr subject:"쿠팡이츠의 결제 내역"', parse_coupang_eats_email,
                            "%쿠팡이츠%", "coupang_items", "coupang_confidence", "matched-eats", "쿠팡이츠")


def match_coupang_membership(conn: sqlite3.Connection) -> dict:
    """쿠팡 와우 멤버십 월회비 메일을 파싱해 DB 거래와 매칭한다."""
    return _match_mail_kind(conn, 'from:coupang.com subject:"와우 멤버십 월회비"', parse_coupang_membership_email,
                            "%쿠팡%", "coupang_items", "coupang_confidence", "matched-membership", "쿠팡 와우 멤버십")


# --------------------------------------------------------------------------- kurly

def parse_kurly_email(text: str) -> dict | None:
    """컬리 주문확인 메일 본문에서 품목명과 결제금액을 추출한다.

    Returns: {items: [str], card_amount: int|None, order_no: str|None, paid_at: str|None(YYYY-MM-DD)}
    """
    section_m = re.search(r'구매상품 정보(.*?)상품금액', text, re.S)
    section = section_m.group(1) if section_m else ""
    lines = [l.strip() for l in section.splitlines() if l.strip()]

    items, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith('['):
            name, qty, j = line, 1, i + 1
            while j < n and not lines[j].startswith('['):
                qm = re.match(r'^(\d+)\s*개$', lines[j])
                if qm:
                    qty = int(qm.group(1))
                    j += 1
                    break
                j += 1
            items.append(f"{name} ×{qty}" if qty >= 2 else name)
            i = j
        else:
            i += 1
    if not items:
        return None

    m = re.search(r'결제금액\s*:\s*([\d,]+)\s*원', text)
    card_amount = int(m.group(1).replace(',', '')) if m else None
    m = re.search(r'주문번호\s*:\s*(\S+)', text)
    order_no = m.group(1) if m else None
    m = re.search(r'결제일시\s+(\d{4}-\d{2}-\d{2})', text)
    paid_at = m.group(1) if m else None
    return {"items": items, "card_amount": card_amount, "order_no": order_no, "paid_at": paid_at}


def match_kurly(conn: sqlite3.Connection) -> dict:
    """컬리 주문확인 메일을 파싱해 DB 거래와 매칭한다. (쿠팡과 동일 알고리즘)"""
    return _match_mail_kind(conn, 'from:kurly.com subject:"주문이 정상적으로 접수"', parse_kurly_email,
                            "%컬리%", "kurly_items", "kurly_confidence", "matched", "컬리")


def parse_kurly_membership_email(text: str) -> dict | None:
    """컬리멤버스 정기결제 메일 본문에서 결제일·결제금액을 추출한다."""
    if "컬리멤버스" not in text:
        return None
    m_date = re.search(r'결제일\s*(\d{4}-\d{2}-\d{2})', text)
    m_amt = re.search(r'결제금액\s*([\d,]+)\s*원', text)
    if not m_date or not m_amt:
        return None
    return {"items": ["컬리멤버스 정기결제 (구독료)"],
            "card_amount": int(m_amt.group(1).replace(',', '')), "paid_at": m_date.group(1)}


def match_kurly_membership(conn: sqlite3.Connection) -> dict:
    return _match_mail_kind(conn, 'from:kurly.com subject:"컬리멤버스 정기결제"', parse_kurly_membership_email,
                            "%컬리%", "kurly_items", "kurly_confidence", "matched-membership", "컬리멤버스 정기결제")


# --------------------------------------------------------------------------- merchant rules

def load_rules(path: Path) -> list[tuple[re.Pattern, str | None, str | None]]:
    """YAML에서 가맹점 룰을 로드한다. (regex, purpose, attribution)"""
    if not path.exists():
        return []
    rules = []
    blocks = re.split(r'\n(?=- pattern:)', path.read_text(encoding="utf-8"))
    for block in blocks:
        pm = re.search(r'pattern:\s*["\'](.+?)["\']', block)
        purm = re.search(r'purpose:\s*["\']?([^"\'#\n]+)', block)
        atm = re.search(r'attribution:\s*["\']?([^"\'#\n]+)', block)
        if pm:
            try:
                pat = re.compile(pm.group(1).replace("\\\\", "\\"), re.IGNORECASE)
            except re.error:
                continue
            rules.append((pat, purm.group(1).strip() if purm else None, atm.group(1).strip() if atm else None))
    return rules


def apply_merchant_rules(conn: sqlite3.Connection) -> int:
    """가맹점 룰을 적용한다. 더 센 신호(coupang·override)로 채워진 필드는 건드리지 않는다."""
    rules = load_rules(RULES_PATH)
    if not rules:
        print("가맹점 룰 없음")
        return 0
    rows = conn.execute("SELECT id, content FROM transactions WHERE type='지출'").fetchall()
    updated = 0
    for tx_id, content in rows:
        for pat, purpose, attrib in rules:
            if pat.search(content or ""):
                sets, params = [], {"id": tx_id}
                if purpose:
                    # yaml이 진실이다. 룰이 매칭되면 자기 값을 쓴다 (COALESCE 가 아니다).
                    # COALESCE면 뱅샐 fallback이 먼저 값을 박았을 때 룰이 영원히 무시되고,
                    # yaml을 고쳐도 재적용이 안 된다. 더 센 신호만 지킨다: coupang(품목 직접 증빙),
                    # override(사람이 건별 판정).
                    keep = "purpose_source IN ('coupang', 'override')"
                    sets.append(f"purpose = CASE WHEN {keep} THEN purpose ELSE :purpose END")
                    sets.append(f"purpose_source = CASE WHEN {keep} THEN purpose_source ELSE 'rule' END")
                    params["purpose"] = purpose
                if attrib:
                    keep = "attrib_source IN ('override')"
                    sets.append(f"attribution = CASE WHEN {keep} THEN attribution ELSE :attrib END")
                    sets.append(f"attrib_source = CASE WHEN {keep} THEN attrib_source ELSE 'rule' END")
                    params["attrib"] = attrib
                if sets:
                    conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=:id", params)
                    updated += 1
                break  # first matching rule wins
    conn.commit()
    print(f"가맹점 룰 적용: {updated}건")
    return updated


def apply_overrides(conn: sqlite3.Connection) -> int:
    """건별 판정 오버라이드. 가장 센 신호라 무조건 덮어쓴다.

    가맹점 룰은 이름으로만 매칭해서, PG 표기 아래 성격이 반대인 거래가 섞이면
    원리상 못 가른다. 사람이 개별 거래를 보고 내린 판정이므로 어떤 자동 추정보다 우선한다.
    """
    if not OVERRIDES_PATH.exists():
        return 0
    entries: list[dict] = []
    cur: dict | None = None
    key = None
    for raw in OVERRIDES_PATH.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#") or not raw.strip():
            continue
        line = raw.rstrip()
        if line.startswith("- match:"):
            if cur:
                entries.append(cur)
            cur, key = {"match": {}}, "match"
            continue
        if cur is None:
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if indent >= 4 and key == "match":
            cur["match"][k] = v
        else:
            key = None
            if k in ("purpose", "attribution", "note"):
                cur[k] = v
    if cur:
        entries.append(cur)

    updated = 0
    for e in entries:
        m = e.get("match", {})
        if not m.get("date") or "amount" not in m:
            print(f"[overrides] match에 date·amount가 필요하다 — 건너뜀: {m}")
            continue
        rows = conn.execute(
            "SELECT id FROM transactions WHERE date=:d AND amount=:a AND content LIKE :c",
            {"d": m["date"], "a": int(m["amount"]), "c": f"%{m.get('content','')}%"}).fetchall()
        if not rows:
            print(f"[overrides] 매칭 0건 — 데이터가 바뀌었나? {m}")
            continue
        for (tx_id,) in rows:
            sets, params = [], {"id": tx_id}
            if e.get("purpose"):
                sets += ["purpose = :purpose", "purpose_source = 'override'"]
                params["purpose"] = e["purpose"]
            if e.get("attribution"):
                sets += ["attribution = :attrib", "attrib_source = 'override'"]
                params["attrib"] = e["attribution"]
            if sets:
                conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=:id", params)
                updated += 1
    conn.commit()
    print(f"건별 오버라이드 적용: {updated}건 (정의 {len(entries)}개)")
    return updated


def apply_method_defaults(conn: sqlite3.Connection) -> int:
    """결제수단별 기본 귀속(rules/attribution.yaml)을 적용한다. 이미 채워진 건 건드리지 않는다.
    어느 수단에도 안 걸린 행은 `default` 라벨로 채운다."""
    cfg = load_attribution()
    updated = 0
    for method, attrib in cfg["methods"].items():
        cur = conn.execute(
            "UPDATE transactions SET attribution=?, attrib_source='method' "
            "WHERE type='지출' AND method=? AND attribution IS NULL", (attrib, method))
        updated += cur.rowcount
    cur = conn.execute(
        "UPDATE transactions SET attribution=?, attrib_source='method' "
        "WHERE type='지출' AND attribution IS NULL", (cfg["default"],))
    updated += cur.rowcount
    conn.commit()
    print(f"결제수단 기본 귀속 적용: {updated}건")
    return updated


# --------------------------------------------------------------------------- coupang purpose from items

COUPANG_PURPOSE_KEYWORDS = {
    "육아": ["이유식", "분유", "기저귀", "젖병", "아기", "유아", "칫솔.*단계", "베이비", "영유아",
             "하기스", "키즈", "소아", "유모차", "빨대컵", "턱받이", "물티슈.*아기", "수유", "젖꼭지", "노리개"],
    "식비": ["김치", "라면", "즉석", "반찬", "고기", "쌀", "커피", "과일", "야채", "채소", "음료", "생수",
             "우유", "요거트", "빵", "소스", "간식", "견과", "샐러드", "버터", "치즈", "두부", "계란", "과자",
             "떡볶이", "피자", "치킨", "햄", "소시지", "국수", "식용유", "참기름", "고추장", "된장", "간장",
             "설탕", "소금", "식초", "마요네즈", "케첩", "카레", "조미료", "비비고", "오뚜기", "풀무원", "프로틴"],
    "생활용품": ["세제", "휴지", "비누", "샴푸", "칫솔", "치약", "세정", "청소", "수세미", "쓰레기", "봉투",
                "행주", "빨래", "섬유유연제", "주방", "화장지", "키친타월", "물티슈", "핸드솝", "락스", "리필",
                "건전지", "전구", "방향제", "탈취제", "방충", "살충", "멀티탭", "테이프", "바디워시", "세탁"],
    "의료": ["약", "밴드", "마스크.*KF", "체온계", "소독", "의약품", "영양제", "비타민", "유산균", "오메가"],
    "사업-장비": ["USB", "허브", "케이블", "충전", "어댑터", "키보드", "마우스", "모니터", "SSD", "메모리",
                "노트북", "태블릿", "크로마키", "배경천", "마이크", "웹캠"],
    "의류·미용": ["옷", "티셔츠", "바지", "양말", "속옷", "신발", "화장품", "스킨", "로션", "선크림", "토너",
                "클렌징", "향수", "런닝화"],
    "교육·도서": ["책", "도서", "교재"],
    "문화·여가": ["캠핑", "등산", "아웃도어"],
}


def infer_purpose_from_items(items_str: str) -> str | None:
    """쿠팡 품목명에서 용도를 추론한다."""
    if not items_str:
        return None
    for purpose, keywords in COUPANG_PURPOSE_KEYWORDS.items():
        for kw in keywords:
            if re.search(kw, items_str, re.IGNORECASE):
                return purpose
    return None


def apply_coupang_purpose(conn: sqlite3.Connection) -> int:
    """쿠팡 매칭된 건에 품목 기반 용도를 부여한다."""
    rows = conn.execute(
        "SELECT id, coupang_items FROM transactions "
        "WHERE coupang_confidence LIKE 'matched%' AND coupang_items IS NOT NULL").fetchall()
    updated = 0
    for tx_id, items in rows:
        purpose = infer_purpose_from_items(items)
        if purpose:
            conn.execute("UPDATE transactions SET purpose=?, purpose_source='coupang' "
                         "WHERE id=? AND (purpose IS NULL OR purpose_source NOT IN ('override'))",
                         (purpose, tx_id))
            updated += 1
    conn.commit()
    print(f"쿠팡 품목 용도 적용: {updated}건 (매칭 {len(rows)}건 중)")
    return updated


# --------------------------------------------------------------------------- banksalad category mapping

# (대분류, 소분류) 결정적 조합 — 대분류만 보면 신호를 놓치는 경우만 둔다.
# 여기서 정해진 행은 purpose_source='banksalad-sub'로 남아 llm 재추론 대상에서 빠진다.
BANKSALAD_SUBCATEGORY_PURPOSE_MAP = {
    ("생활", "육아"): "육아",
    ("문화/여가", "도서"): "교육·도서",
}

BANKSALAD_PURPOSE_MAP = {
    "식비": "식비", "식사": "식비", "카페/간식": "식비",
    "편의점/마트/잡화": "생활용품", "생활": "생활용품",
    "주거/통신": "주거·공과", "통신/세금/공동비용": "주거·공과",
    "교통": "교통·차량", "자동차": "교통·차량",
    "의료/건강": "의료",
    "문화/여가": "문화·여가", "술/유흥": "문화·여가", "여행/숙박": "문화·여가",
    "교육": "교육·도서",
    "의류/미용": "의류·미용", "의복/미용": "의류·미용",
    "경조/선물": "경조사", "경조사/행사": "경조사",
    "금융": "금융·이체",
    "사업": "기타", "용돈": "기타", "미분류": "기타", "기타": "기타", "반려동물": "기타",
    "시터": "육아", "육아": "육아",
}


def apply_banksalad_fallback(conn: sqlite3.Connection) -> int:
    """뱅샐 분류를 용도 fallback으로 매핑한다 (최하위 우선순위)."""
    sub_updated = 0
    for (bs_cat, bs_sub), purpose in BANKSALAD_SUBCATEGORY_PURPOSE_MAP.items():
        cur = conn.execute(
            "UPDATE transactions SET purpose=?, purpose_source='banksalad-sub' "
            "WHERE type='지출' AND category=? AND subcategory=? AND purpose IS NULL", (purpose, bs_cat, bs_sub))
        sub_updated += cur.rowcount
    updated = 0
    for bs_cat, purpose in BANKSALAD_PURPOSE_MAP.items():
        cur = conn.execute(
            "UPDATE transactions SET purpose=?, purpose_source='banksalad' "
            "WHERE type='지출' AND category=? AND purpose IS NULL", (purpose, bs_cat))
        updated += cur.rowcount
    # 매핑에 없는 대분류는 '기타'
    cur = conn.execute("UPDATE transactions SET purpose='기타', purpose_source='banksalad' "
                       "WHERE type='지출' AND purpose IS NULL")
    updated += cur.rowcount
    conn.commit()
    print(f"뱅샐 소분류 결정 용도 적용: {sub_updated}건")
    print(f"뱅샐 대분류 fallback 용도 적용: {updated}건")
    return sub_updated + updated


# --------------------------------------------------------------------------- llm

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_BACKENDS = ("openrouter", "claude", "codex", "gemini", "none")
LLM_BACKEND = os.environ.get("BUDGET_LLM_BACKEND", "openrouter")
LLM_MODEL = os.environ.get("BUDGET_LLM_MODEL", "")   # 비우면 백엔드별 기본값
LLM_MIN_CONFIDENCE = float(os.environ.get("BUDGET_LLM_MIN_CONF", "0.6"))  # 미만이면 뱅샐 fallback 유지
LLM_BATCH_SIZE = 25
DEFAULT_MODELS = {"openrouter": "anthropic/claude-sonnet-5", "claude": "sonnet",
                  "codex": "", "gemini": ""}   # codex·gemini 는 CLI 기본 모델

LLM_SYSTEM = """당신은 한국 가계부 거래의 '용도'를 분류한다.

허용 라벨은 아래 %d개뿐이다. 이 밖의 값은 절대 쓰지 마라:
%s

입력의 category/subcategory는 가계부 앱(뱅크샐러드)이 자동으로 붙인 대분류·소분류다.
**정답이 아니라 참고 신호**다 — 가맹점명이 더 구체적인 정보를 주면 가맹점명을 따르라.
(예: 빵집이 '카페/간식'으로 분류돼 있어도 실제 용도는 '식비'다.
 subcategory가 '육아'·'도서'처럼 구체적이면 그건 강한 신호다.)

confidence 규칙 — 이게 이 작업의 핵심이다:
- 가맹점명으로 업종이 분명하면 0.8~0.95
- 업종은 알겠으나 용도가 갈릴 수 있으면 0.5~0.7
- **판매자를 특정할 수 없으면 0.4 이하.** 결제대행(PG)·인증대행·포인트사·
  플랫폼 표기(예: KICC, NHNKCP, 다날, 간편결제)는 그 아래 무엇이 결제됐는지 이름만으로 알 수 없다.
  추측해서 높은 confidence를 주지 마라.
- 모르면 모른다고 하는 편이 낫다. 낮은 confidence 는 반영되지 않고 기존 분류가 유지된다.

출력은 JSON 객체 하나뿐. 설명·서문·코드펜스 금지.
{"results":[{"id":<입력의 id 정수>,"purpose":"<허용 라벨>","confidence":<0.0~1.0>}]}
입력의 모든 id에 대해 정확히 하나씩 낸다.""" % (len(PURPOSES), " / ".join(PURPOSES))


def _llm_targets(conn: sqlite3.Connection, redo: bool) -> list[dict]:
    """LLM 재추론 대상을 (가맹점, 대분류, 소분류) 단위로 묶는다 — 행 단위로 부르면 같은 가맹점을 반복 질의한다."""
    sources = ("banksalad", "llm") if redo else ("banksalad",)
    ph = ",".join("?" * len(sources))
    rows = conn.execute(
        f"SELECT content, category, subcategory, COUNT(*), AVG(-amount) "
        f"FROM transactions WHERE type='지출' AND purpose_source IN ({ph}) "
        f"GROUP BY content, category, subcategory ORDER BY COUNT(*) DESC, content", sources).fetchall()
    return [{"id": i, "content": r[0] or "", "category": r[1] or "", "subcategory": r[2] or "",
             "n": r[3], "avg": int(r[4] or 0)} for i, r in enumerate(rows)]


def _llm_call_openrouter(user: str, model: str, api_key: str, timeout: int = 120) -> str:
    payload = json.dumps({"model": model, "temperature": 0,
                          "messages": [{"role": "system", "content": LLM_SYSTEM},
                                       {"role": "user", "content": user}]}, ensure_ascii=False)
    req = urllib.request.Request(OPENROUTER_URL, data=payload.encode(), method="POST",
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json",
                                          "X-Title": "banksalad-budget-organizer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"]


def _cli_prompt(user: str) -> str:
    return LLM_SYSTEM + "\n\n입력(JSON 배열):\n" + user + "\n\n출력은 JSON 객체 하나만."


def _run_cli(cmd: list[str], timeout: int, stdin: str | None = None) -> str:
    if shutil.which(cmd[0]) is None:
        raise OSError(f"CLI 없음: {cmd[0]} (PATH 확인)")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    if res.returncode != 0:
        raise OSError(f"{cmd[0]} exit {res.returncode}: {res.stderr.strip()[:200]}")
    return res.stdout


def _llm_call_claude(user: str, model: str, timeout: int = 240) -> str:
    """Claude Code CLI 헤드리스 호출(`claude -p`). 도구를 막아 순수 판정기로 쓴다."""
    out = _run_cli([os.environ.get("CLAUDE_BIN", "claude"), "-p", _cli_prompt(user),
                    *(["--model", model] if model else []), "--tools", "", "--output-format", "json"], timeout)
    env = json.loads(out)
    if env.get("is_error"):
        raise OSError(f"claude -p is_error: {str(env.get('result'))[:200]}")
    return env.get("result") or ""


def _llm_call_codex(user: str, model: str, timeout: int = 240) -> str:
    """OpenAI Codex CLI 헤드리스 호출(`codex exec`). 마지막 메시지를 파일로 받는다."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        out_path = f.name
    _run_cli([os.environ.get("CODEX_BIN", "codex"), "exec", "--skip-git-repo-check",
              *(["-m", model] if model else []), "-o", out_path, _cli_prompt(user)], timeout)
    text = Path(out_path).read_text(encoding="utf-8")
    Path(out_path).unlink(missing_ok=True)
    return text


def _llm_call_gemini(user: str, model: str, timeout: int = 240) -> str:
    """Google Gemini CLI 헤드리스 호출 (위치 인자 프롬프트, `-p` 는 deprecated).
    Workspace 계정은 GOOGLE_CLOUD_PROJECT env 가 필요하다 — CLI 가 그 오류를 내면 .env 에 넣는다."""
    return _run_cli([os.environ.get("GEMINI_BIN", "gemini"), *(["-m", model] if model else []),
                     "-o", "text", _cli_prompt(user)], timeout)


def _parse_llm_reply(text: str, batch: list[dict]) -> dict[int, tuple[str, float]]:
    """응답을 id로 매칭해 검증한다. 위치(zip)로 맞추지 않는다 — 한 건이 빠지면 라벨이 밀린다."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    valid_ids = {b["id"] for b in batch}
    out: dict[int, tuple[str, float]] = {}
    for item in data.get("results", []):
        try:
            iid, purpose, conf = int(item["id"]), str(item["purpose"]).strip(), float(item["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        if iid not in valid_ids or iid in out or purpose not in PURPOSES or not 0.0 <= conf <= 1.0:
            continue
        out[iid] = (purpose, conf)
    return out


def apply_llm_purpose(conn: sqlite3.Connection, *, limit: int | None = None, dry_run: bool = False,
                      redo: bool = False, min_conf: float = LLM_MIN_CONFIDENCE, model: str = LLM_MODEL,
                      batch_size: int = LLM_BATCH_SIZE, backend: str = LLM_BACKEND) -> dict:
    """뱅샐 대분류만 보고 찍힌 행을 LLM으로 재추론한다.

    대상은 purpose_source='banksalad'뿐이다. rule/coupang/override는 건드리지 않는다 —
    결정적 신호를 확률적 추론으로 덮는 건 퇴행이다.
    반환은 요약 dict — `written` 외에 **`batches_failed`** 를 꼭 본다. 배치 실패는 예외를 삼키고
    계속 진행하므로(기존 분류 유지가 안전) 전 배치가 죽어도 exit 0 이다.
    """
    model = model or DEFAULT_MODELS.get(backend, "")
    summary = {"targets": 0, "rows_covered": 0, "batches": 0, "batches_failed": 0, "written": 0,
               "low_conf": 0, "dry_run": dry_run, "backend": backend, "model": model, "aborted": None}
    if backend not in LLM_BACKENDS:
        summary["aborted"] = f"unknown backend {backend} (허용: {LLM_BACKENDS})"
        print("중단:", summary["aborted"])
        return summary
    if backend == "none":
        print("LLM 백엔드 none — 건너뜀")
        return summary
    stray = {r[0] for r in conn.execute(
        "SELECT DISTINCT purpose FROM transactions WHERE purpose IS NOT NULL")} - set(PURPOSES)
    if stray:
        summary["aborted"] = f"stray purposes: {sorted(stray)}"
        print("중단: DB에 PURPOSES 밖 라벨이 있다 →", sorted(stray))
        return summary

    targets = _llm_targets(conn, redo)
    if limit:
        targets = targets[:limit]
    summary["targets"] = len(targets)
    if not targets:
        print("LLM 재추론 대상 없음")
        return summary
    summary["rows_covered"] = sum(t["n"] for t in targets)
    print(f"대상: {len(targets)}개 가맹점 조합 / {summary['rows_covered']}건  (백엔드 {backend}, 모델 {model or '기본'})")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if backend == "openrouter" and not api_key:
        summary["aborted"] = "OPENROUTER_API_KEY 없음"
        print("중단: OPENROUTER_API_KEY 없음 (.env 확인)")
        return summary

    call = {
        "openrouter": lambda u: _llm_call_openrouter(u, model, api_key),
        "claude": lambda u: _llm_call_claude(u, model),
        "codex": lambda u: _llm_call_codex(u, model),
        "gemini": lambda u: _llm_call_gemini(u, model),
    }[backend]

    proposals: dict[int, tuple[str, float]] = {}
    for start in range(0, len(targets), batch_size):
        summary["batches"] += 1
        batch = targets[start:start + batch_size]
        user = json.dumps([{"id": b["id"], "merchant": b["content"], "category": b["category"],
                            "subcategory": b["subcategory"], "count": b["n"], "avg_amount": b["avg"]}
                           for b in batch], ensure_ascii=False)
        got = {}
        for attempt in (1, 2):
            try:
                got = _parse_llm_reply(call(user), batch)
                break
            except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, IndexError,
                    ValueError, subprocess.TimeoutExpired) as e:
                print(f"  배치 {start//batch_size + 1} 시도 {attempt} 실패: {type(e).__name__}: {e}")
                if attempt == 2:
                    print("  → 이 배치 폐기 (기존 banksalad 분류 유지)")
                    summary["batches_failed"] += 1
                else:
                    time.sleep(3)
        print(f"  배치 {start//batch_size + 1}/{(len(targets)-1)//batch_size + 1}: "
              f"{len(got)}건 수용, {len(batch) - len(got)}건 폐기")
        proposals.update(got)

    by_id = {t["id"]: t for t in targets}
    keep_src = ("banksalad", "llm") if redo else ("banksalad",)
    ph = ",".join("?" * len(keep_src))
    written = low_conf = 0
    for iid, (purpose, conf) in sorted(proposals.items()):
        t = by_id[iid]
        if conf < min_conf:
            low_conf += t["n"]
            if dry_run:
                print(f"  [skip conf={conf:.2f}] {t['content']} ({t['category']}/{t['subcategory']}) → {purpose}")
            continue
        if dry_run:
            print(f"  [{conf:.2f}] {t['content']} ({t['category']}/{t['subcategory']}, {t['n']}건) → {purpose}")
            written += t["n"]
            continue
        cur = conn.execute(
            f"UPDATE transactions SET purpose=?, purpose_source='llm', llm_confidence=? "
            f"WHERE type='지출' AND purpose_source IN ({ph}) AND content IS ? AND category IS ? AND subcategory IS ?",
            (purpose, conf, *keep_src, t["content"] or None, t["category"] or None, t["subcategory"] or None))
        written += cur.rowcount
    summary["written"], summary["low_conf"] = written, low_conf
    if dry_run:
        print(f"\n[dry-run] 반영 예정 {written}건 / 저신뢰 보류 {low_conf}건 (임계값 {min_conf}) — DB 미변경")
        return summary
    conn.commit()
    print(f"\nLLM 용도 적용: {written}건 / 저신뢰 보류 {low_conf}건 (임계값 {min_conf})")
    return summary


# --------------------------------------------------------------------------- stats

def print_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM transactions WHERE type='지출'").fetchone()[0] or 1
    print("\n=== 태깅 현황 ===")
    print(f"지출 총건: {total}")
    for label, col in (("용도", "purpose"), ("귀속", "attribution")):
        n = conn.execute(f"SELECT COUNT(*) FROM transactions WHERE type='지출' AND {col} IS NOT NULL").fetchone()[0]
        print(f"{label} 태깅: {n} ({n/total*100:.1f}%)")
        print(f"\n{label} 분포:")
        for r in conn.execute(f"SELECT {col}, COUNT(*), SUM(amount) FROM transactions "
                              f"WHERE type='지출' AND {col} IS NOT NULL GROUP BY {col} ORDER BY COUNT(*) DESC"):
            print(f"  {r[0]:15} {r[1]:5}건  {r[2]:>13,}원")
    for label, col in (("용도 근거", "purpose_source"), ("귀속 근거", "attrib_source")):
        print(f"\n{label}:")
        for r in conn.execute(f"SELECT {col}, COUNT(*) FROM transactions WHERE type='지출' AND {col} IS NOT NULL "
                              f"GROUP BY {col} ORDER BY COUNT(*) DESC"):
            print(f"  {r[0]:15} {r[1]:5}건")


# --------------------------------------------------------------------------- main

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    if cmd == "schema":
        pass
    elif cmd == "coupang":
        match_coupang(conn)
        match_coupang_eats(conn)
        match_coupang_membership(conn)
        apply_coupang_purpose(conn)
    elif cmd == "kurly":
        match_kurly(conn)
        match_kurly_membership(conn)
    elif cmd == "rules":
        apply_merchant_rules(conn)
    elif cmd == "method":
        apply_method_defaults(conn)
    elif cmd == "overrides":
        apply_overrides(conn)
    elif cmd == "apply":
        print("=== 1. 쿠팡 품목 용도 ===")
        apply_coupang_purpose(conn)
        print("\n=== 2. 가맹점 룰 ===")
        apply_merchant_rules(conn)
        print("\n=== 3. 뱅샐 분류 fallback ===")
        apply_banksalad_fallback(conn)
        print("\n=== 4. 결제수단 귀속 ===")
        apply_method_defaults(conn)
        print("\n=== 5. 건별 오버라이드 ===")   # 마지막 — 사람이 건별로 내린 판정이라 위 모든 자동 신호를 덮는다
        apply_overrides(conn)
    elif cmd == "llm":
        argv = sys.argv[2:]

        def _opt(name, cast, default):
            return cast(argv[argv.index(name) + 1]) if name in argv else default

        summary = apply_llm_purpose(
            conn, limit=_opt("--limit", int, None), dry_run="--dry-run" in argv, redo="--redo" in argv,
            min_conf=_opt("--min-conf", float, LLM_MIN_CONFIDENCE), model=_opt("--model", str, LLM_MODEL),
            batch_size=_opt("--batch", int, LLM_BATCH_SIZE), backend=_opt("--backend", str, LLM_BACKEND))
        out = _opt("--json-out", str, None)
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    elif cmd == "stats":
        print_stats(conn)
    else:
        print(f"알 수 없는 명령: {cmd}")
        sys.exit(1)
    conn.close()


if __name__ == "__main__":
    main()
