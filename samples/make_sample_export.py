#!/usr/bin/env python3
"""결정적(seed 고정) 가공 뱅크샐러드 export xlsx 생성기.

실제 개인정보·실제 거래·실제 계좌명은 전혀 담지 않는다 — 전부 지어낸 샘플이다.
가맹점명은 흔한 프랜차이즈·일반명사만 쓴다.

    python3 samples/make_sample_export.py

`scripts/banksalad_ingest.py`가 기대하는 시트 구조를 그대로 따른다:
  - "뱅샐현황": 빈 시트, 헤더 한 줄만 ("항목","값")
  - "가계부 내역": 실제 거래 데이터. 헤더는 COLUMNS와 정확히 일치해야 한다.

이 스크립트가 만드는 특수 케이스(테스트가 기대하는 것들):
  - 지하철 -1,550원 3행을 같은 날짜·같은 시각·같은 금액·같은 내용으로 넣는다 →
    banksalad_ingest.natural_key()의 발생순번(#1 #2 #3) dedup을 검증하기 위함.
  - 환불 3행은 '지출' 타입인데 금액이 양수다 (뱅샐 원본 부호 보존 규칙).
  - 결제수단에 rules/attribution.example.yaml의 methods에 없는 "모임통장"을 섞어
    default 귀속("불명")이 먹는지 검증할 수 있게 한다.
  - 2026-03-14 / -128000원 / 내용에 "NHNKCP" 포함 행은
    rules/transaction-overrides.example.yaml이 가리키는 오버라이드 대상이다.
"""
import random
from datetime import datetime
from pathlib import Path

import openpyxl

REPO = Path(__file__).resolve().parent.parent
OUT_PATH = REPO / "samples" / "sample-export.xlsx"

COLUMNS = ["날짜", "시간", "타입", "대분류", "소분류", "내용", "금액", "화폐", "결제수단", "메모"]

RNG = random.Random(20260101)  # 결정적 — 매 실행 동일 결과

METHODS = ["내 체크카드", "생활비 통장", "사업자 체크카드", "사업자 통장", "간편결제(포인트)"]
METHOD_OUT_OF_MAP = "모임통장"  # attribution.example.yaml methods에 없음 → default 귀속 검증용


def row(date: str, time: str, typ: str, category: str, subcategory: str,
        content: str, amount: int, method: str, memo: str = "") -> dict:
    return {
        "날짜": datetime.strptime(date, "%Y-%m-%d"),
        "시간": time,
        "타입": typ,
        "대분류": category,
        "소분류": subcategory,
        "내용": content,
        "금액": amount,
        "화폐": "KRW",
        "결제수단": method,
        "메모": memo,
    }


def build_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- 수입 6행 (월급, 매월 25일) ----
    for i, m in enumerate(range(1, 7)):
        rows.append(row(f"2026-{m:02d}-25", "09:00:00", "수입", "급여", "", "월급",
                        3_200_000 + i * 10_000, "생활비 통장"))

    # ---- 이체 6행 (정기적금, 매월 5일) ----
    for m in range(1, 7):
        rows.append(row(f"2026-{m:02d}-05", "07:30:00", "이체", "이체", "", "정기적금",
                        -500_000, "생활비 통장"))

    # ---- 환불 3행 ('지출' 타입, 양수) ----
    rows.append(row("2026-02-14", "13:20:11", "지출", "생활", "", "지마켓 반품환불",
                    15_000, "내 체크카드"))
    rows.append(row("2026-04-08", "10:05:44", "지출", "식비", "", "홈쇼핑 반품환불",
                    8_000, "내 체크카드"))
    rows.append(row("2026-05-20", "16:41:02", "지출", "의류/미용", "", "매장 교환환불",
                    32_000, "사업자 체크카드"))

    # ---- 지하철 dedup 검증용 3행 (같은 날짜·시각·금액·내용) ----
    for _ in range(3):
        rows.append(row("2026-03-03", "08:15:32", "지출", "교통", "", "지하철",
                        -1_550, "내 체크카드"))

    # ---- 쿠팡 6행 ----
    coupang_specs = [
        ("2026-01-10", "생활", "쿠팡 생필품구매", -32_400, "내 체크카드"),
        ("2026-02-05", "편의점/마트/잡화", "쿠팡 잡화구매", -18_900, "내 체크카드"),
        ("2026-02-20", "식비", "쿠팡 식료품구매", -27_300, "간편결제(포인트)"),
        ("2026-03-15", "생활", "쿠팡 세제구매", -21_500, "내 체크카드"),
        ("2026-04-22", "편의점/마트/잡화", "쿠팡 생필품구매", -15_700, "내 체크카드"),
        ("2026-05-30", "생활", "쿠팡 정기배송", -24_800, "내 체크카드"),
    ]
    for d, cat, content, amt, method in coupang_specs:
        rows.append(row(d, "11:42:07", "지출", cat, "", content, amt, method))

    # ---- 쿠팡이츠 2행 ----
    rows.append(row("2026-02-03", "19:12:55", "지출", "식비", "", "쿠팡이츠 주문",
                    -13_500, "내 체크카드"))
    rows.append(row("2026-04-11", "20:03:18", "지출", "식비", "", "쿠팡이츠 주문",
                    -21_900, "내 체크카드"))

    # ---- 컬리 4행 (컬리페이_멤버스 1행 포함) ----
    rows.append(row("2026-02-17", "17:31:14", "지출", "식비", "", "컬리 장보기",
                    -34_217, "내 체크카드"))
    rows.append(row("2026-03-09", "08:02:40", "지출", "식비", "", "컬리 주문",
                    -19_800, "내 체크카드"))
    rows.append(row("2026-04-19", "07:55:21", "지출", "식비", "", "컬리 주문",
                    -28_400, "내 체크카드"))
    rows.append(row("2026-05-01", "00:10:00", "지출", "식비", "", "컬리페이_멤버스",
                    -1_900, "내 체크카드", "구독료"))

    # ---- 네이버페이 / NHNKCP 6행 (오버라이드 대상 1행 포함) ----
    rows.append(row("2026-01-18", "14:22:09", "지출", "문화/여가", "", "네이버페이 결제",
                    -30_000, "내 체크카드"))
    rows.append(row("2026-02-10", "09:47:31", "지출", "생활", "", "네이버페이",
                    -25_000, "간편결제(포인트)"))
    # 오버라이드 대상: rules/transaction-overrides.example.yaml 이 이 행을 가리킨다.
    rows.append(row("2026-03-14", "12:30:00", "지출", "식비", "", "NHNKCP 한식당결제",
                    -128_000, "사업자 체크카드"))
    rows.append(row("2026-04-05", "18:14:52", "지출", "생활", "", "네이버페이",
                    -12_000, "내 체크카드"))
    rows.append(row("2026-05-12", "21:09:03", "지출", "문화/여가", "", "NHNKCP 공연예매",
                    -45_000, "내 체크카드"))
    rows.append(row("2026-06-08", "08:33:17", "지출", "카페/간식", "", "네이버페이",
                    -9_900, "간편결제(포인트)"))

    # ---- 소분류 조합 검증용 2행 ----
    rows.append(row("2026-01-22", "15:00:00", "지출", "생활", "육아", "이마트 기저귀구매",
                    -45_000, "내 체크카드"))
    rows.append(row("2026-03-25", "12:00:00", "지출", "문화/여가", "도서", "교보문고 도서구매",
                    -18_000, "내 체크카드"))

    # ---- 결제수단 default 귀속 검증용 1행 (attribution.example.yaml methods에 없는 수단) ----
    rows.append(row("2026-04-14", "19:30:00", "지출", "식비", "", "회식비 정산",
                    -60_000, METHOD_OUT_OF_MAP))

    # ---- 채움 지출 20행 x 6개월 = 120행 (달마다 빈 달 없이) ----
    filler_by_category = {
        "식비": ["김밥천국", "본죽", "맘스터치", "김치찌개집"],
        "카페/간식": ["스타벅스", "이디야", "메가커피", "컴포즈커피"],
        "편의점/마트/잡화": ["GS25", "CU", "세븐일레븐", "이마트24"],
        "교통": ["지하철", "카카오T", "시내버스", "고속버스"],
        "문화/여가": ["영화관", "볼링장", "노래방", "전시회"],
        "의료/건강": ["동네약국", "정형외과의원", "치과의원", "한의원"],
        "주거/통신": ["도시가스", "한국전력", "SKT", "관리비"],
        "생활": ["다이소", "생활용품점", "철물점", "세탁소"],
        "금융": ["카드이자", "연회비", "보험료", "적립식펀드"],
    }
    categories = list(filler_by_category.keys())

    for m in range(1, 7):
        for i in range(20):
            cat = categories[(m * 20 + i) % len(categories)]
            content = filler_by_category[cat][RNG.randrange(len(filler_by_category[cat]))]
            day = RNG.randrange(1, 27)
            hh, mm, ss = RNG.randrange(7, 23), RNG.randrange(0, 60), RNG.randrange(0, 60)
            amount = -RNG.randrange(2_000, 60_000)
            method = METHODS[RNG.randrange(len(METHODS))]
            rows.append(row(f"2026-{m:02d}-{day:02d}", f"{hh:02d}:{mm:02d}:{ss:02d}",
                            "지출", cat, "", content, amount, method))

    return rows


def write_xlsx(rows: list[dict], out_path: Path) -> None:
    wb = openpyxl.Workbook()

    ws_status = wb.active
    ws_status.title = "뱅샐현황"
    ws_status.append(["항목", "값"])

    ws = wb.create_sheet("가계부 내역")
    ws.append(COLUMNS)
    for r in rows:
        ws.append([r[c] for c in COLUMNS])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main() -> None:
    rows = build_rows()
    write_xlsx(rows, OUT_PATH)
    print(f"{OUT_PATH} 생성 완료 — {len(rows)}행")


if __name__ == "__main__":
    main()
