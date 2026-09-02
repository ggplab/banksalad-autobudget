#!/usr/bin/env python3
"""가계부 파이프라인 오케스트레이터 — 뱅샐 export 1통 → DB → 태깅 → 품목 보강 → LLM → 산출물 → 도착지.

## 한 줄로 전부 (n8n 없이)
    python3 pipeline.py run --file ~/Downloads/export.xlsx            # 파일 드롭
    python3 pipeline.py run --from-gmail                               # Gmail 에서 새 export 자동 수신
    python3 pipeline.py run --retag                                    # export 없이 태깅부터 (룰 yaml 고친 뒤)
    python3 pipeline.py run --file X.xlsx --target html,discord        # 도착지 지정
    python3 pipeline.py run --from tag --skip llm                      # 단계 슬라이싱

## 단계별로 (n8n·cron·다른 오케스트레이터가 부를 때 — 각 단계가 노드 1개)
    python3 pipeline.py fetch-export [--file X | --from-gmail | --retag] [--message-id ID]
    python3 pipeline.py ingest [--xlsx X]
    python3 pipeline.py tag
    python3 pipeline.py match-coupang | match-kurly | match-naver [--days 45]
    python3 pipeline.py apply
    python3 pipeline.py llm [--backend openrouter|claude|codex|gemini|none] [--limit N]
    python3 pipeline.py export [--target html,email,discord,hook,artifact] [--out dist/]
    python3 pipeline.py finish [--dry-run]
    python3 pipeline.py status | unlock | notify --text "..."

stdout 은 단계마다 **JSON 한 줄**(외부 오케스트레이터가 파싱), 진행 로그는 stderr.

## 실행 상태(run.json)와 단락
`fetch-export` 가 run.json 을 만들고, 실패한 단계는 `failed` 를 기록한 뒤 exit≠0. 이후 단계는 run.failed 를 보고
`{"skipped":true}` 로 즉시 통과한다(exit 0). n8n SSH 노드처럼 종료코드를 무시하는 오케스트레이터에서도
낡은 DB 위에 하류가 도는 사고를 막는다. `finish` 는 실패 여부와 무관하게 알림을 보내고 락을 푼다.

## 파이프라인 락
두 실행이 겹치면(15분 폴링 사이에 export 2통) 두 번째 ingest 가 첫 번째의 tag 와 apply 사이에 끼어든다.
`fetch-export` 가 락을 잡고 `finish` 가 푼다. 90분 넘은 락은 stale 로 보고 덮어쓴다.

## 소프트 실패
주문메일 보강(match-*)은 Gmail 토큰이 없으면 `skipped`, LLM 은 실패해도 `ok:false` 만 남기고 파이프라인을 계속한다 —
결정적 단계의 산출물은 LLM 없이도 유효하다.

## 계산 로직을 재구현하지 않는다
banksalad_ingest · expense_tagger · naver_pay_mail · export_budget 의 함수를 import 해 부른다.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_env_file(path: Path) -> None:
    """repo 의 .env 를 os.environ 에 싣는다 (이미 있는 값은 안 덮는다). 모듈 import 전에 해야 한다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file(Path(os.environ.get("BUDGET_ENV_FILE") or ROOT / ".env"))

DATA_DIR = Path(os.environ.get("BUDGET_DATA_DIR") or ROOT / "data")
DB_PATH = Path(os.environ.get("BUDGET_DB") or DATA_DIR / "budget.db")
os.environ.setdefault("BUDGET_DB", str(DB_PATH))   # 하위 모듈이 같은 DB 를 보게
STATE_DIR = Path(os.environ.get("BUDGET_STATE_DIR") or DATA_DIR / "state")
EXPORTS_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
OUT_DIR = Path(os.environ.get("BUDGET_OUT_DIR") or ROOT / "dist")
RUN_FILE, SEEN_FILE, LOCK_FILE = STATE_DIR / "run.json", STATE_DIR / "seen.json", STATE_DIR / "lock"
PREV_AGG_FILE = STATE_DIR / "prev-agg.json"
BACKUP_KEEP = 30
LOCK_STALE_SEC = 90 * 60
MIN_ROWS = int(os.environ.get("BUDGET_MIN_ROWS", "10"))      # 이보다 적으면 빈 export 로 의심
EXPORT_SENDER = os.environ.get("BUDGET_EXPORT_SENDER", "export-noreply@banksalad.com")

STAGES = ["fetch-export", "ingest", "tag", "match-coupang", "match-kurly", "match-naver",
          "apply", "llm", "export", "finish"]
STAGE_LABELS = {
    "fetch-export": "📥 export 수신", "ingest": "🗄️ 적재", "tag": "🏷️ 결정 태깅",
    "match-coupang": "📦 쿠팡", "match-kurly": "🥬 컬리", "match-naver": "🟢 네이버페이",
    "apply": "✅ apply", "llm": "🤖 LLM 용도추론", "export": "📊 산출물·전달", "finish": "🏁 마무리",
}
CURRENT_CMD = ""


# ── 공통 ────────────────────────────────────────────────────────────────
def emit(payload: dict) -> int:
    json.dump({"node": CURRENT_CMD, **payload}, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def log(msg: str) -> None:
    sys.stderr.write(f"[{CURRENT_CMD}] {msg}\n")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def load_run() -> dict:
    return load_json(RUN_FILE)


def save_run(run: dict) -> None:
    save_json(RUN_FILE, run)


def stage_ok(payload: dict) -> int:
    run = load_run()
    run.setdefault("stages", {})[CURRENT_CMD] = {"ok": True, "at": now_iso(),
                                                 **{k: v for k, v in payload.items() if k != "summary"}}
    save_run(run)
    return emit({"ok": True, **{k: v for k, v in payload.items() if k != "summary"}})


def stage_fail(error: str, **extra) -> int:
    run = load_run()
    run["failed"] = run.get("failed") or CURRENT_CMD
    run.setdefault("stages", {})[CURRENT_CMD] = {"ok": False, "error": error, "at": now_iso(), **extra}
    save_run(run)
    emit({"ok": False, "error": error, **extra})
    return 1


def stage_skip(because: str) -> int:
    run = load_run()
    run.setdefault("stages", {})[CURRENT_CMD] = {"skipped": True, "because": because}
    save_run(run)
    return emit({"ok": True, "skipped": True, "because": because})


def short_circuit(*, explicit_input: bool = False) -> int | None:
    """상류 실패 시 즉시 통과. 실행 상태가 없으면(fetch-export 가 안 돌았으면) 그것도 실패다."""
    run = load_run()
    if not run:
        return stage_fail("run.json 없음 — fetch-export 가 먼저 돌아야 한다")
    if run.get("failed"):
        return stage_skip(run["failed"])
    if run.get("found") is False and CURRENT_CMD == "ingest" and not explicit_input:
        return stage_skip("retag")     # 재태깅 경로 — 적재할 xlsx 가 없다
    return None


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


@contextlib.contextmanager
def quiet():
    """라이브러리 print 를 stderr 로 돌린다 — stdout 은 JSON 한 줄이어야 한다."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def gmail_available(profile: str) -> bool:
    try:
        import gmail_client as g
        return g.available(profile)
    except Exception:
        return False


# ── fetch-export ────────────────────────────────────────────────────────
def lock_acquire(run_id: str) -> tuple[bool, str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SEC:
            return False, f"다른 실행이 진행 중 ({int(age // 60)}분 경과, 락 {LOCK_FILE.read_text().strip()[:60]})"
        log(f"stale 락 {int(age // 60)}분 — 덮어쓴다")
    LOCK_FILE.write_text(f"{run_id} {now_iso()}\n", encoding="utf-8")
    return True, ""


def lock_release() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def unzip_export(raw: bytes, name_hint: str = "") -> tuple[Path, dict]:
    """뱅샐 zip(비번 보호) → data/exports/<xlsx>. 비번은 BUDGET_ZIP_PASSWORD (앱에서 export 할 때 정한 것)."""
    pw = os.environ.get("BUDGET_ZIP_PASSWORD")
    if not pw:
        raise RuntimeError("BUDGET_ZIP_PASSWORD 없음 (.env)")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = [i.filename for i in zf.infolist() if i.filename.lower().endswith(".xlsx")]
    if len(names) != 1:
        raise RuntimeError(f"zip 안 xlsx 가 1개가 아니다: {[i.filename for i in zf.infolist()]}")
    try:
        xbytes = zf.read(names[0], pwd=pw.encode("utf-8"))
    except RuntimeError as e:  # zipfile 은 비번 오류를 RuntimeError('Bad password ...') 로 낸다
        raise RuntimeError(f"zip 해제 실패 (비번?): {e}") from e
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    xlsx = EXPORTS_DIR / Path(names[0]).name
    xlsx.write_bytes(xbytes)
    m = re.search(r"(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})", xlsx.name)
    return xlsx, {"zip_name": name_hint, "zip_bytes": len(raw), "zip_sha256": hashlib.sha256(raw).hexdigest(),
                  "xlsx": str(xlsx), "xlsx_sha256": hashlib.sha256(xbytes).hexdigest(),
                  "range": [m.group(1), m.group(2)] if m else None}


def find_new_export(explicit_id: str | None) -> dict | None:
    """가장 최근의 미처리 export 메일 1통. 같은 폴링 안에 2통이 와도 최신 1건만
    (export 는 누적 1년 범위라 최신이 이전을 포함한다). 이전 건은 seen 에 함께 넣는다."""
    import gmail_client as g
    g.use("export")
    if explicit_id:
        return {"id": explicit_id, "also_seen": []}
    seen = load_json(SEEN_FILE)
    ids = g.search(f"from:{EXPORT_SENDER} has:attachment newer_than:14d", limit=10)  # 최신 우선
    fresh = [i for i in ids if i not in seen]
    return {"id": fresh[0], "also_seen": fresh[1:]} if fresh else None


def download_export(message_id: str) -> tuple[Path, dict]:
    import gmail_client as g
    g.use("export")
    headers, atts = g.attachments_of(message_id)
    if EXPORT_SENDER not in headers.get("From", ""):
        raise RuntimeError(f"발신자가 뱅샐 export 가 아니다: {headers.get('From')}")
    zips = [a for a in atts if a["filename"].lower().endswith(".zip")]
    if len(zips) != 1:
        raise RuntimeError(f"zip 첨부가 1개가 아니다: {[a['filename'] for a in atts]}")
    raw = g.attachment_bytes(message_id, zips[0]["attachmentId"])
    xlsx, meta = unzip_export(raw, zips[0]["filename"])
    return xlsx, {"message_id": message_id, "subject": headers.get("Subject", ""),
                  "sent_at": headers.get("Date", ""), **meta}


def cmd_fetch_export(args) -> int:
    run_id = uuid.uuid4().hex[:8]
    ok, why = lock_acquire(run_id)
    if not ok:
        return emit({"ok": True, "found": False, "locked": True, "reason": why})
    run = {"run_id": run_id, "started": now_iso(), "found": None, "failed": None,
           "retag": bool(args.retag), "db": str(DB_PATH), "stages": {}}
    save_run(run)
    if args.retag:
        run["found"] = False
        save_run(run)
        return stage_ok({"found": False, "retag": True, "run_id": run_id})

    if args.file:                                   # ── 로컬 파일 드롭
        src = Path(args.file).expanduser()
        if not src.is_file():
            return stage_fail(f"파일 없음: {src}")
        try:
            if src.suffix.lower() == ".zip":
                xlsx, meta = unzip_export(src.read_bytes(), src.name)
            else:
                EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
                xlsx = EXPORTS_DIR / src.name
                if src.resolve() != xlsx.resolve():
                    shutil.copy2(src, xlsx)
                m = re.search(r"(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})", xlsx.name)
                meta = {"xlsx": str(xlsx), "xlsx_sha256": hashlib.sha256(xlsx.read_bytes()).hexdigest(),
                        "range": [m.group(1), m.group(2)] if m else None}
        except Exception as e:
            return stage_fail(f"파일 준비 실패: {e}")
        run.update({"found": True, "xlsx": str(xlsx), "source": "file"})
        save_run(run)
        return stage_ok({"found": True, "run_id": run_id, "source": "file", **meta})

    if not (args.from_gmail or args.message_id):
        return stage_fail("--file <xlsx|zip> 또는 --from-gmail 또는 --retag 중 하나가 필요하다")
    if not gmail_available("export"):               # ── Gmail 폴링
        return stage_fail("Gmail 토큰 없음 — `python3 scripts/gmail_client.py --auth` 먼저")
    try:
        hit = find_new_export(args.message_id)
    except Exception as e:
        return stage_fail(f"메일 조회 실패: {e}")
    if hit is None:
        lock_release()
        RUN_FILE.unlink(missing_ok=True)
        return emit({"ok": True, "found": False, "run_id": run_id})
    seen = load_json(SEEN_FILE)
    try:
        xlsx, meta = download_export(hit["id"])
    except Exception as e:
        # 실패한 메시지도 seen 에 넣는다 — 폴링마다 같은 실패가 반복되는 것을 막는다. 재시도는 --message-id 로.
        seen[hit["id"]] = {"at": now_iso(), "error": str(e)[:200]}
        save_json(SEEN_FILE, seen)
        return stage_fail(f"export 다운로드/해제 실패: {e}", message_id=hit["id"])
    seen[hit["id"]] = {"at": now_iso(), "xlsx": xlsx.name, "run_id": run_id}
    for other in hit["also_seen"]:
        seen[other] = {"at": now_iso(), "superseded_by": hit["id"]}
    for k in sorted(seen, key=lambda k: seen[k].get("at", ""))[:-200]:   # 최근 200건만
        seen.pop(k, None)
    save_json(SEEN_FILE, seen)
    run.update({"found": True, "xlsx": str(xlsx), "message_id": hit["id"], "source": "gmail"})
    save_run(run)
    return stage_ok({"found": True, "run_id": run_id, "source": "gmail", "superseded": hit["also_seen"], **meta})


# ── ingest ──────────────────────────────────────────────────────────────
def backup_db() -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"budget-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(DB_PATH)
    try:
        with sqlite3.connect(dst) as d:
            src.backup(d)
    finally:
        src.close()
    for p in sorted(BACKUP_DIR.glob("budget-2*.db"))[:-BACKUP_KEEP]:
        p.unlink()
    return str(dst)


def cmd_ingest(args) -> int:
    sc = short_circuit(explicit_input=bool(args.xlsx))
    if sc is not None:
        return sc
    run = load_run()
    xlsx = Path(args.xlsx or run.get("xlsx") or "")
    if not xlsx.is_file():
        return stage_fail(f"xlsx 없음: {xlsx}")
    try:
        with quiet():
            import banksalad_ingest as bi
            backup = backup_db() if DB_PATH.exists() else None
            conn = connect()
            conn.executescript(bi.SCHEMA)
            total, new, upd = bi.ingest(xlsx, conn)
        cnt = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
        conn.close()
    except SystemExit as e:   # bi.ingest 는 시트 없음 등을 sys.exit 로 낸다
        return stage_fail(f"적재 중단: {e}")
    except Exception as e:
        return stage_fail(f"적재 실패: {type(e).__name__}: {e}")
    if total < MIN_ROWS:
        return stage_fail(f"xlsx 행수가 비정상적으로 적다 ({total}행 < BUDGET_MIN_ROWS={MIN_ROWS}) — 빈 export 의심",
                          rows_total=total)
    return stage_ok({"rows_total": total, "rows_new": new, "rows_updated": upd,
                     "db_count": cnt, "db_range": [lo, hi], "backup": backup})


# ── tag / match / apply ─────────────────────────────────────────────────
def _with_tagger(fn):
    sc = short_circuit()
    if sc is not None:
        return sc
    try:
        with quiet():
            import expense_tagger as et
            conn = connect()
            et.ensure_schema(conn)
            out = fn(et, conn)
        conn.commit()
        conn.close()
    except Exception as e:
        return stage_fail(f"{type(e).__name__}: {e}")
    return stage_ok(out)


def _enrich_enabled() -> str | None:
    """주문메일 보강을 건너뛸 사유. None 이면 진행."""
    if os.environ.get("BUDGET_ENRICH", "1") in ("0", "false", "no"):
        return "BUDGET_ENRICH=0"
    if not gmail_available("orders"):
        return "no-gmail-token"
    return None


def cmd_tag(args) -> int:
    return _with_tagger(lambda et, conn: {
        "rules": et.apply_merchant_rules(conn),
        "method": et.apply_method_defaults(conn),
        "overrides": et.apply_overrides(conn),
    })


def cmd_match_coupang(args) -> int:
    why = _enrich_enabled()
    if why:
        return short_circuit() or stage_skip(why)
    return _with_tagger(lambda et, conn: {
        "coupang": et.match_coupang(conn), "eats": et.match_coupang_eats(conn),
        "membership": et.match_coupang_membership(conn), "purpose_applied": et.apply_coupang_purpose(conn)})


def cmd_match_kurly(args) -> int:
    why = _enrich_enabled()
    if why:
        return short_circuit() or stage_skip(why)
    return _with_tagger(lambda et, conn: {"kurly": et.match_kurly(conn), "membership": et.match_kurly_membership(conn)})


def cmd_match_naver(args) -> int:
    """네이버페이는 메일 1통마다 본문을 받아 파싱하므로 전 기간을 매번 돌면 오래 걸린다(실측 200통/6분).
    기존 매칭은 DB 컬럼에 남아 있으므로 DB 최신일 기준 최근 창(--days)만 다시 본다."""
    why = _enrich_enabled()
    if why:
        return short_circuit() or stage_skip(why)
    sc = short_circuit()
    if sc is not None:
        return sc
    try:
        with quiet():
            import naver_pay_mail as npm
        conn = connect()
        hi = conn.execute("SELECT MAX(date) FROM transactions").fetchone()[0]
        conn.close()
        hi_d = datetime.strptime(hi, "%Y-%m-%d") if hi else datetime.now()
        after = (hi_d - timedelta(days=args.days)).strftime("%Y/%m/%d")
        before = (hi_d + timedelta(days=1)).strftime("%Y/%m/%d")
        with quiet():
            r = npm.match_to_db(DB_PATH, after=after, before=before)
    except Exception as e:
        return stage_fail(f"{type(e).__name__}: {e}")
    return stage_ok({**r, "window": [after, before]})


def cmd_apply(args) -> int:
    return _with_tagger(lambda et, conn: {
        "coupang_purpose": et.apply_coupang_purpose(conn),
        "rules": et.apply_merchant_rules(conn),
        "banksalad_fallback": et.apply_banksalad_fallback(conn),
        "method": et.apply_method_defaults(conn),
        "overrides": et.apply_overrides(conn),
        "purpose_source": purpose_source_dist(conn),
    })


def purpose_source_dist(conn: sqlite3.Connection) -> dict:
    return {r[0] or "(null)": r[1] for r in conn.execute(
        "SELECT purpose_source, COUNT(*) FROM transactions WHERE type='지출' "
        "GROUP BY purpose_source ORDER BY COUNT(*) DESC")}


# ── llm (소프트 실패) ────────────────────────────────────────────────────
def cmd_llm(args) -> int:
    sc = short_circuit()
    if sc is not None:
        return sc
    backend = args.backend or os.environ.get("BUDGET_LLM_BACKEND", "openrouter")
    if backend == "none":
        return stage_skip("BUDGET_LLM_BACKEND=none")
    started = time.time()
    try:
        with quiet():
            import expense_tagger as et
            conn = connect()
            et.ensure_schema(conn)
            before = purpose_source_dist(conn)
            summary = et.apply_llm_purpose(conn, limit=args.limit, backend=backend,
                                           model=args.model or os.environ.get("BUDGET_LLM_MODEL", ""))
            after = purpose_source_dist(conn)
            conn.close()
    except Exception as e:
        summary, before, after = {"error": f"{type(e).__name__}: {e}"[:300], "batches": 0, "batches_failed": 0}, {}, {}
    dur = round(time.time() - started, 1)
    ok = summary.get("error") is None and summary.get("aborted") is None and (
        summary.get("batches", 0) == 0 or summary.get("batches_failed", 0) < summary.get("batches", 0))
    payload = {**summary, "llm_rows_before": before.get("llm", 0), "llm_rows_after": after.get("llm", 0), "duration_s": dur}
    run = load_run()
    run.setdefault("stages", {})["llm"] = {"ok": ok, "at": now_iso(), **payload}   # run.failed 는 세우지 않는다
    save_run(run)
    return emit({"ok": ok, **payload})


# ── export (산출물 + 도착지) ──────────────────────────────────────────────
def monthly_agg(conn: sqlite3.Connection) -> dict:
    return {r[0]: {"n": r[1], "spend": r[2]} for r in conn.execute(
        "SELECT substr(date,1,7) m, COUNT(*), -SUM(amount) FROM transactions "
        "WHERE type='지출' AND amount<0 GROUP BY m ORDER BY m")}


def cmd_export(args) -> int:
    sc = short_circuit()
    if sc is not None:
        return sc
    conn = connect()
    agg = monthly_agg(conn)
    conn.close()
    months = sorted(agg)
    gaps = []   # 불변식: 월 연속성 (첫 달~마지막 달 사이 빈 달이 없다)
    if months:
        y, m = int(months[0][:4]), int(months[0][5:7])
        cur = months[0]
        while cur < months[-1]:
            m += 1
            if m > 12:
                y, m = y + 1, 1
            cur = f"{y:04d}-{m:02d}"
            if cur not in agg and cur < months[-1]:
                gaps.append(cur)
    if gaps:
        return stage_fail(f"월 연속성 위반 — 빈 달 {gaps}", months=months)
    out = Path(args.out or OUT_DIR)
    targets = [t.strip() for t in (args.target or os.environ.get("BUDGET_TARGETS", "html")).split(",") if t.strip()]
    try:
        with quiet():
            import export_budget as eb
            r = eb.generate(DB_PATH, out)
            delivered = eb.deliver(targets, out, r["summary"])
    except Exception as e:
        return stage_fail(f"산출물 생성 실패: {type(e).__name__}: {e}")
    prev = load_json(PREV_AGG_FILE)   # 드리프트: 직전 실행의 월별 집계와 비교
    drift = []
    for mth in months[-13:]:
        a, b = prev.get(mth), agg[mth]
        if a is None:
            drift.append({"month": mth, "new_month": True, "spend": b["spend"], "n": b["n"]})
        elif a != b:
            drift.append({"month": mth, "spend_delta": b["spend"] - a["spend"], "n_delta": b["n"] - a["n"]})
    save_json(PREV_AGG_FILE, agg)
    failed_targets = [k for k, v in delivered.items() if not v.get("ok")]
    return stage_ok({"out": str(out), "csv_rows": r["csv_rows"], "html_kb": r["html_kb"], "range": r["range"],
                     "last_month": r["last_month"], "last_month_spend": r["last_month_spend"],
                     "targets": delivered, "targets_failed": failed_targets, "drift": drift,
                     "evidence": r["summary"]["evidence"]})


# ── finish (알림 + 락 해제) ───────────────────────────────────────────────
def build_report(run: dict) -> tuple[str, str]:
    st, failed = run.get("stages", {}), run.get("failed")
    lines = []
    for key in STAGES[:-1]:
        s, label = st.get(key), STAGE_LABELS[key]
        if s is None:
            lines.append(f"⬜ {label} — 미실행")
            continue
        if s.get("skipped"):
            lines.append(f"⏭️ {label} — 건너뜀 ({s.get('because')})")
            continue
        icon = "✅" if s.get("ok") else ("⚠️" if key == "llm" else "❌")
        d = ""
        if key == "fetch-export":
            rng = s.get("range") or ["?", "?"]
            d = (f"{rng[0]}~{rng[1]} · {s.get('source')}" if s.get("found")
                 else ("재태깅 (export 없음)" if s.get("retag") else "새 export 없음"))
        elif key == "ingest":
            d = f"신규 {s.get('rows_new')} · 갱신 {s.get('rows_updated')} / 총 {s.get('rows_total')}행 → DB {s.get('db_count')}건"
        elif key == "tag":
            d = f"룰 {s.get('rules')} · 수단 {s.get('method')} · 오버라이드 {s.get('overrides')}"
        elif key == "match-coupang":
            c = s.get("coupang") or {}
            d = f"주문 {c.get('matched')}/{c.get('emails_total')} · 이츠 {(s.get('eats') or {}).get('matched')} · 용도 {s.get('purpose_applied')}건"
        elif key == "match-kurly":
            c = s.get("kurly") or {}
            d = f"주문 {c.get('matched')}/{c.get('emails_total')} · 멤버스 {(s.get('membership') or {}).get('matched')}"
        elif key == "match-naver":
            d = f"주문 {s.get('orders')} → 매칭 {s.get('matched')} · 모호 {s.get('ambiguous')}"
        elif key == "apply":
            ps = s.get("purpose_source") or {}
            d = "용도출처 " + " · ".join(f"{k} {v}" for k, v in sorted(ps.items(), key=lambda x: -x[1]))
        elif key == "llm":
            d = (f"{s.get('backend')} · 대상 {s.get('targets')}조합 · 반영 {s.get('written')}행 · "
                 f"배치 실패 {s.get('batches_failed')}/{s.get('batches')} · {s.get('duration_s')}s")
        elif key == "export":
            moved = [x for x in (s.get("drift") or []) if not x.get("new_month")]
            d = f"{s.get('last_month')} 지출 {int(s.get('last_month_spend') or 0):,}원 · csv {s.get('csv_rows')}행 · 월 변동 {len(moved)}개"
            if s.get("targets_failed"):
                d += f" · 전달 실패 {s['targets_failed']}"
        if not s.get("ok"):
            d = (s.get("error") or d)[:160]
        lines.append(f"{icon} {label} — {d}")
    try:
        dur = int((datetime.now() - datetime.strptime(run.get("started", ""), "%Y-%m-%dT%H:%M:%S")).total_seconds())
    except ValueError:
        dur = 0
    title = "가계부 파이프라인 — " + ("❌ 실패" if failed else ("🔁 재태깅 완료" if run.get("retag") else "✅ 완료"))
    return title, "\n".join(lines) + f"\n\nrun {run.get('run_id')} · {dur // 60}분 {dur % 60}초"


def notify(title: str, text: str) -> dict:
    """알림 채널 — BUDGET_DISCORD_WEBHOOK_URL 이 있으면 Discord. 없으면 stderr 에만 남긴다."""
    sys.stderr.write(f"\n{title}\n{text}\n")
    if not os.environ.get("BUDGET_DISCORD_WEBHOOK_URL"):
        return {"sent": False, "reason": "BUDGET_DISCORD_WEBHOOK_URL 미설정"}
    try:
        import export_budget as eb
        r = eb.deliver_discord({}, text=text, title=title)
        return {"sent": bool(r.get("ok")), **r}
    except Exception as e:
        return {"sent": False, "error": f"{type(e).__name__}: {e}"[:200]}


def cmd_finish(args) -> int:
    run = load_run()
    if not run:
        lock_release()
        return emit({"ok": True, "sent": False, "reason": "run.json 없음 (found=false 경로)"})
    title, text = build_report(run)
    sent = {"sent": False, "reason": "dry-run"} if args.dry_run else notify(title, text)
    hist = STATE_DIR / "runs"    # 실행 기록은 보존(진단용), 락은 해제
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{run.get('started', 'x').replace(':', '')}-{run.get('run_id')}.json").write_text(
        json.dumps({**run, "notify": sent}, ensure_ascii=False, indent=1), encoding="utf-8")
    for p in sorted(hist.glob("*.json"))[:-60]:
        p.unlink()
    RUN_FILE.unlink(missing_ok=True)
    lock_release()
    return emit({"ok": not run.get("failed"), "failed_stage": run.get("failed"), "run_id": run.get("run_id"),
                 "notify": sent, "report": text})


def cmd_notify(args) -> int:
    return emit({"ok": True, **notify(args.title, args.text)})


def cmd_status(args) -> int:
    lock = LOCK_FILE.read_text().strip() if LOCK_FILE.exists() else None
    return emit({"ok": True, "db": str(DB_PATH), "state_dir": str(STATE_DIR), "lock": lock,
                 "run": load_run(), "seen_count": len(load_json(SEEN_FILE)),
                 "gmail_export": gmail_available("export"), "gmail_orders": gmail_available("orders"),
                 "llm_backend": os.environ.get("BUDGET_LLM_BACKEND", "openrouter"),
                 "targets": os.environ.get("BUDGET_TARGETS", "html")})


def cmd_unlock(args) -> int:
    lock_release()
    RUN_FILE.unlink(missing_ok=True)
    return emit({"ok": True, "unlocked": True})


# ── run (n8n 없는 오케스트레이터) ─────────────────────────────────────────
def cmd_run(args) -> int:
    """STAGES 를 한 프로세스에서 순서대로. 단락·락·소프트 실패 규약은 단계 함수가 이미 갖고 있으므로
    여기서는 순서·슬라이싱·조기 종료(처리할 것 없음)만 맡는다."""
    global CURRENT_CMD
    seq = STAGES[:]
    if args.only:
        seq = [args.only]
    if getattr(args, "from_stage", None):
        if args.from_stage not in STAGES:
            return emit({"ok": False, "error": f"--from 은 {STAGES} 중 하나"})
        seq = STAGES[STAGES.index(args.from_stage):]
        if "fetch-export" not in seq and not load_run():   # 중간부터 시작 — run.json 이 없으면 재태깅 컨텍스트를 만든다
            args.retag = True
            seq = ["fetch-export"] + seq
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    if not args.file and not args.from_gmail and not args.retag and not args.message_id and "fetch-export" in seq:
        if gmail_available("export"):
            args.from_gmail = True
        else:
            return emit({"ok": False, "error": "--file <xlsx|zip> 또는 --from-gmail 또는 --retag 가 필요하다"})
    exit_code = 0
    for stage in seq:
        CURRENT_CMD = stage
        if stage in skip and stage not in ("fetch-export", "finish"):
            stage_skip("--skip")
            continue
        rc = FN[stage](args)
        if stage == "fetch-export":
            run = load_run()
            if not run or (run.get("found") is False and not run.get("retag")):
                log("처리할 export 없음 — 종료")
                return 0
        if rc != 0 and stage not in ("llm",):
            exit_code = 1
    return exit_code


FN = {
    "fetch-export": cmd_fetch_export, "ingest": cmd_ingest, "tag": cmd_tag,
    "match-coupang": cmd_match_coupang, "match-kurly": cmd_match_kurly, "match-naver": cmd_match_naver,
    "apply": cmd_apply, "llm": cmd_llm, "export": cmd_export, "finish": cmd_finish,
    "status": cmd_status, "unlock": cmd_unlock, "notify": cmd_notify, "run": cmd_run,
}


def _add_fetch_opts(p):
    p.add_argument("--file", help="뱅샐 export xlsx 또는 zip (로컬 파일 드롭)")
    p.add_argument("--from-gmail", action="store_true", help="Gmail 에서 새 export 메일을 찾아 받는다")
    p.add_argument("--message-id", help="특정 Gmail 메시지 id 재처리")
    p.add_argument("--retag", action="store_true", help="export 없이 태깅부터 (룰 yaml 변경 재적용)")


def _add_llm_opts(p):
    p.add_argument("--backend", choices=["openrouter", "claude", "codex", "gemini", "none"])
    p.add_argument("--model")
    p.add_argument("--limit", type=int, help="가맹점 조합 상한 (테스트용)")


def _add_export_opts(p):
    p.add_argument("--target", help="html,email,discord,hook,artifact (콤마 구분, 기본 BUDGET_TARGETS 또는 html)")
    p.add_argument("--out", help="산출물 디렉토리 (기본 dist/)")


def main() -> int:
    global CURRENT_CMD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="전 단계를 한 번에")
    _add_fetch_opts(p)
    _add_llm_opts(p)
    _add_export_opts(p)
    p.add_argument("--from", dest="from_stage", help="이 단계부터")
    p.add_argument("--only", choices=STAGES, help="이 단계만")
    p.add_argument("--skip", help="건너뛸 단계 (콤마 구분)")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--xlsx")
    p.add_argument("--dry-run", action="store_true", help="finish 알림을 보내지 않는다")
    p = sub.add_parser("fetch-export")
    _add_fetch_opts(p)
    p = sub.add_parser("ingest")
    p.add_argument("--xlsx")
    for name in ("tag", "match-coupang", "match-kurly", "apply", "status", "unlock"):
        sub.add_parser(name)
    p = sub.add_parser("match-naver")
    p.add_argument("--days", type=int, default=45, help="DB 최신일 기준 이 일수 전부터의 메일만 (기본 45)")
    p = sub.add_parser("llm")
    _add_llm_opts(p)
    p = sub.add_parser("export")
    _add_export_opts(p)
    p = sub.add_parser("finish")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("notify")
    p.add_argument("--text", required=True)
    p.add_argument("--title", default="가계부 파이프라인")
    args = ap.parse_args()
    CURRENT_CMD = args.cmd
    return FN[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
