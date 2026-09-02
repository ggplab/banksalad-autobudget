#!/usr/bin/env python3
"""산출물 생성 + 도착지 전달 — 태깅된 DB 를 사람이 보는 형태로 내보낸다.

    python3 scripts/export_budget.py --out dist/                      # csv·json·html 생성
    python3 scripts/export_budget.py --out dist/ --target email,discord

생성물 (항상):
    dist/budget.csv        전 거래 + 태그 (스프레드시트로 열면 끝)
    dist/summary.json      월별·용도별·귀속별 집계 (다른 도구에 먹이기 좋다)
    dist/budget.html       단일 파일 리포트 (브라우저로 열기, 아티팩트로 올리기, 메일로 보내기)

도착지 (--target, 콤마 구분, 기본 html):
    html      로컬 HTML 파일만 (기본)
    email     SMTP 로 리포트 메일 발송 (BUDGET_SMTP_* · BUDGET_EMAIL_TO)
    discord   Discord webhook 으로 요약 카드 (BUDGET_DISCORD_WEBHOOK_URL)
    hook      임의 스크립트 실행 (BUDGET_DEPLOY_HOOK <dist경로>) — 서버 배포·클라우드 업로드 등 자기 방식
    artifact  AI 코딩 에이전트 세션이 dist/budget.html 을 라이브 아티팩트로 올린다 (docs/start-prompt.md)
"""
from __future__ import annotations

import argparse
import csv
import html as H
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BUDGET_DB") or REPO / "data" / "budget.db")
TARGETS = ("html", "email", "discord", "hook", "artifact")

TX_COLUMNS = ["date", "time", "type", "category", "subcategory", "content", "amount", "method", "memo",
              "purpose", "purpose_source", "attribution", "attrib_source",
              "coupang_items", "kurly_items", "naver_merchant", "naver_items", "llm_confidence"]


# --------------------------------------------------------------------------- 집계

def _cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}


def summarize(conn: sqlite3.Connection) -> dict:
    """양수 도메인(spend = -amount)에서만 계산한다. 음수를 계산 계층에 두면 부호 버그가 재발한다."""
    cols = _cols(conn)
    has = lambda c: c in cols  # noqa: E731
    q = conn.execute
    lo, hi = q("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    monthly = [{"month": r[0], "n": r[1], "spend": r[2] or 0, "refund": r[3] or 0, "income": r[4] or 0}
               for r in q("SELECT substr(date,1,7) m, "
                          "SUM(CASE WHEN type='지출' AND amount<0 THEN 1 ELSE 0 END), "
                          "SUM(CASE WHEN type='지출' AND amount<0 THEN -amount ELSE 0 END), "
                          "SUM(CASE WHEN type='지출' AND amount>0 THEN amount ELSE 0 END), "
                          "SUM(CASE WHEN type='수입' THEN amount ELSE 0 END) "
                          "FROM transactions GROUP BY m ORDER BY m")]

    def breakdown(col: str, month: str | None = None) -> list[dict]:
        if not has(col):
            return []
        where = "type='지출' AND amount<0" + (" AND substr(date,1,7)=?" if month else "")
        args = (month,) if month else ()
        return [{"label": r[0] or "(없음)", "n": r[1], "spend": r[2]}
                for r in q(f"SELECT {col}, COUNT(*), SUM(-amount) FROM transactions WHERE {where} "
                           f"GROUP BY {col} ORDER BY SUM(-amount) DESC", args)]

    last_month = monthly[-1]["month"] if monthly else None
    top_merchants = [{"content": r[0], "n": r[1], "spend": r[2]}
                     for r in q("SELECT content, COUNT(*), SUM(-amount) FROM transactions "
                                "WHERE type='지출' AND amount<0 GROUP BY content ORDER BY SUM(-amount) DESC LIMIT 15")]

    def attach_rate(like: str, col: str) -> list[int] | None:
        if not has(col) or not last_month:
            return None
        hit, n = q(f"SELECT SUM(CASE WHEN {col} IS NOT NULL AND {col}<>'' THEN 1 ELSE 0 END), COUNT(*) "
                   f"FROM transactions WHERE type='지출' AND amount<0 AND substr(date,1,7)=? AND content LIKE ?",
                   (last_month, like)).fetchone()
        return [hit or 0, n]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "range": [lo, hi],
        "tx_count": q("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "monthly": monthly,
        "last_month": last_month,
        "purpose_total": breakdown("purpose"),
        "purpose_last_month": breakdown("purpose", last_month),
        "attribution_total": breakdown("attribution"),
        "purpose_source": breakdown("purpose_source"),
        "top_merchants": top_merchants,
        "evidence": {"coupang": attach_rate("%쿠팡%", "coupang_items"),
                     "kurly": attach_rate("%컬리%", "kurly_items"),
                     "naver": attach_rate("%네이버%", "naver_items")},
    }


# --------------------------------------------------------------------------- 파일 생성

def write_csv(conn: sqlite3.Connection, path: Path) -> int:
    cols = [c for c in TX_COLUMNS if c in _cols(conn)]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM transactions ORDER BY date, time").fetchall()
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM — 엑셀에서 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return len(rows)


def _won(v) -> str:
    return f"{int(v or 0):,}원"


def render_html(s: dict, recent: list[tuple], recent_cols: list[str]) -> str:
    def table(headers: list[str], rows: list[list]) -> str:
        th = "".join(f"<th>{H.escape(str(h))}</th>" for h in headers)
        trs = "".join("<tr>" + "".join(f"<td>{H.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
        return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"

    monthly = table(["월", "지출 건수", "지출", "환불", "수입"],
                    [[m["month"], m["n"], _won(m["spend"]), _won(m["refund"]), _won(m["income"])] for m in s["monthly"]])
    purpose_lm = table(["용도", "건수", "금액"], [[p["label"], p["n"], _won(p["spend"])] for p in s["purpose_last_month"]])
    purpose_all = table(["용도", "건수", "금액"], [[p["label"], p["n"], _won(p["spend"])] for p in s["purpose_total"]])
    attrib = table(["귀속", "건수", "금액"], [[p["label"], p["n"], _won(p["spend"])] for p in s["attribution_total"]])
    src = table(["용도 근거", "건수", "금액"], [[p["label"], p["n"], _won(p["spend"])] for p in s["purpose_source"]])
    merchants = table(["가맹점", "건수", "금액"], [[m["content"], m["n"], _won(m["spend"])] for m in s["top_merchants"]])
    ev = " · ".join(f"{k} {v[0]}/{v[1]}" for k, v in s["evidence"].items() if v) or "(주문메일 보강 미사용)"
    recent_t = table(recent_cols, [[("" if c is None else c) for c in r] for r in recent])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>가계부 리포트 {H.escape(s['range'][0] or '')} ~ {H.escape(s['range'][1] or '')}</title>
<style>
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;margin:0;padding:24px;background:#f7f7f5;color:#222}}
main{{max-width:1000px;margin:0 auto}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.meta{{color:#666;font-size:13px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} @media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}} th,td{{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;white-space:nowrap}}
td:nth-child(n+2){{text-align:right}} th{{background:#fafafa;font-weight:600}} .wrap{{overflow-x:auto}}
.kpi{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}} .kpi div{{background:#fff;padding:12px 16px;border:1px solid #e5e5e5;border-radius:8px;min-width:140px}}
.kpi b{{display:block;font-size:20px}} .kpi span{{color:#666;font-size:12px}}
</style></head><body><main>
<h1>가계부 리포트</h1>
<div class="meta">{H.escape(s['range'][0] or '')} ~ {H.escape(s['range'][1] or '')} · 거래 {s['tx_count']:,}건 · 생성 {s['generated_at']}</div>
<div class="kpi">
 <div><b>{_won(s['monthly'][-1]['spend']) if s['monthly'] else '-'}</b><span>{H.escape(s['last_month'] or '')} 지출</span></div>
 <div><b>{s['monthly'][-1]['n'] if s['monthly'] else '-'}건</b><span>{H.escape(s['last_month'] or '')} 지출 건수</span></div>
 <div><b>{_won(sum(m['spend'] for m in s['monthly']))}</b><span>전체 기간 지출</span></div>
</div>
<h2>월별</h2><div class="wrap">{monthly}</div>
<div class="grid">
 <section><h2>{H.escape(s['last_month'] or '')} 용도별</h2><div class="wrap">{purpose_lm}</div></section>
 <section><h2>전체 용도별</h2><div class="wrap">{purpose_all}</div></section>
 <section><h2>귀속별</h2><div class="wrap">{attrib}</div></section>
 <section><h2>용도 근거 (자동 태깅이 어디서 왔나)</h2><div class="wrap">{src}</div></section>
</div>
<h2>가맹점 TOP 15</h2><div class="wrap">{merchants}</div>
<h2>정제 현황 ({H.escape(s['last_month'] or '')})</h2><p class="meta">주문메일 품목 부착률 — {H.escape(ev)}</p>
<h2>최근 거래 {len(recent)}건</h2><div class="wrap">{recent_t}</div>
<p class="meta">전체 거래는 budget.csv, 집계는 summary.json · banksalad-autobudget</p>
</main></body></html>"""


def generate(db: Path, out: Path, recent_n: int = 100) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    s = summarize(conn)
    n_csv = write_csv(conn, out / "budget.csv")
    (out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    cols = [c for c in ("date", "content", "amount", "method", "purpose", "attribution", "coupang_items",
                        "kurly_items", "naver_items") if c in _cols(conn)]
    recent = conn.execute(f"SELECT {', '.join(cols)} FROM transactions WHERE type='지출' "
                          f"ORDER BY date DESC, time DESC LIMIT {int(recent_n)}").fetchall()
    conn.close()
    html_path = out / "budget.html"
    html_path.write_text(render_html(s, recent, cols), encoding="utf-8")
    return {"out": str(out), "csv_rows": n_csv, "html_kb": round(html_path.stat().st_size / 1024),
            "range": s["range"], "last_month": s["last_month"],
            "last_month_spend": s["monthly"][-1]["spend"] if s["monthly"] else 0, "summary": s}


# --------------------------------------------------------------------------- 도착지

def card_text(s: dict) -> str:
    lm = s["monthly"][-1] if s["monthly"] else None
    lines = [f"기간 {s['range'][0]} ~ {s['range'][1]} · 거래 {s['tx_count']:,}건"]
    if lm:
        lines.append(f"{lm['month']} 지출 {_won(lm['spend'])} ({lm['n']}건) · 환불 {_won(lm['refund'])}")
    top = ", ".join(f"{p['label']} {_won(p['spend'])}" for p in s["purpose_last_month"][:5])
    if top:
        lines.append(f"용도 TOP: {top}")
    return "\n".join(lines)


def deliver_email(out: Path, s: dict) -> dict:
    host = os.environ.get("BUDGET_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("BUDGET_SMTP_PORT", "587"))
    user, pw, to = (os.environ.get("BUDGET_SMTP_USER"), os.environ.get("BUDGET_SMTP_PASSWORD"),
                    os.environ.get("BUDGET_EMAIL_TO"))
    if not (user and pw and to):
        return {"ok": False, "error": "BUDGET_SMTP_USER / BUDGET_SMTP_PASSWORD / BUDGET_EMAIL_TO 필요"}
    msg = EmailMessage()
    msg["Subject"] = f"[가계부] {s['last_month']} 리포트"
    msg["From"], msg["To"] = user, to
    msg.set_content(card_text(s))
    msg.add_alternative((out / "budget.html").read_text(encoding="utf-8"), subtype="html")
    msg.add_attachment((out / "budget.csv").read_bytes(), maintype="text", subtype="csv", filename="budget.csv")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, pw)
        smtp.send_message(msg)
    return {"ok": True, "to": to}


def deliver_discord(s: dict, text: str | None = None, title: str = "가계부 리포트") -> dict:
    url = os.environ.get("BUDGET_DISCORD_WEBHOOK_URL")
    if not url:
        return {"ok": False, "error": "BUDGET_DISCORD_WEBHOOK_URL 필요"}
    body = json.dumps({"embeds": [{"title": title, "description": (text or card_text(s))[:3900],
                                   "color": 0x2ECC71}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"ok": 200 <= r.status < 300, "status": r.status}


def deliver_hook(out: Path) -> dict:
    hook = os.environ.get("BUDGET_DEPLOY_HOOK")
    if not hook:
        return {"ok": False, "error": "BUDGET_DEPLOY_HOOK 필요 (실행할 스크립트 경로)"}
    r = subprocess.run([hook, str(out)], capture_output=True, text=True, timeout=600)
    return {"ok": r.returncode == 0, "exit": r.returncode, "tail": (r.stdout + r.stderr)[-400:]}


def deliver(targets: list[str], out: Path, s: dict) -> dict:
    results = {}
    for t in targets:
        if t == "html":
            results[t] = {"ok": True, "path": str(out / "budget.html")}
        elif t == "email":
            results[t] = _safe(deliver_email, out, s)
        elif t == "discord":
            results[t] = _safe(deliver_discord, s)
        elif t == "hook":
            results[t] = _safe(deliver_hook, out)
        elif t == "artifact":
            results[t] = {"ok": True, "hint": f"AI 에이전트 세션에서 {out / 'budget.html'} 을 아티팩트로 게시 (docs/start-prompt.md)"}
        else:
            results[t] = {"ok": False, "error": f"알 수 없는 target {t} (허용: {TARGETS})"}
    return results


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # 도착지 하나가 죽어도 나머지는 전달한다
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--target", default=os.environ.get("BUDGET_TARGETS", "html"))
    args = ap.parse_args()
    r = generate(args.db, args.out)
    targets = [t.strip() for t in args.target.split(",") if t.strip()]
    d = deliver(targets, args.out, r["summary"])
    print(f"생성: {args.out}/budget.{{csv,html}} + summary.json — csv {r['csv_rows']}행, html {r['html_kb']}KB")
    for k, v in d.items():
        print(f"  {k}: {'OK' if v.get('ok') else 'FAIL'} {v.get('error') or v.get('path') or v.get('hint') or ''}")
    return 0 if all(v.get("ok") for v in d.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
