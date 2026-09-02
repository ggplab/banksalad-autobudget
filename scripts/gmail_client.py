#!/usr/bin/env python3
"""Gmail 읽기 전용 클라이언트 — 표준 라이브러리만 사용 (google-api-python-client 불필요).

두 가지 일에 쓴다:
  1. 뱅크샐러드 export 메일의 zip 첨부 받기 (`pipeline.py fetch-export --from-gmail`)
  2. 쿠팡·컬리·네이버페이 주문메일 본문 읽기 (품목 보강)

둘이 **다른 Gmail 계정**이어도 된다 — 프로필(`export` / `orders`)마다 토큰 파일을 따로 둔다.
같은 계정이면 토큰 하나를 공유한다(기본값).

  최초 1회 (브라우저 필요):
      python3 scripts/gmail_client.py --auth                 # export·orders 공용 토큰
      python3 scripts/gmail_client.py --auth --profile orders  # 주문메일 계정이 다를 때
  동작 확인:
      python3 scripts/gmail_client.py --probe

## 준비물 — 자기 GCP OAuth 클라이언트 (약 10분)
  1. https://console.cloud.google.com → 프로젝트 생성 → "API 및 서비스" → Gmail API 사용 설정
  2. "사용자 인증 정보" → OAuth 클라이언트 ID → 유형 **데스크톱 앱** → JSON 다운로드
  3. OAuth 동의 화면 → 테스트 사용자에 자기 Gmail 주소 추가 (게시 안 해도 된다)
  4. 다운로드한 JSON을 `~/.config/banksalad-budget/client_secret.json` 에 둔다
     (경로 변경: 환경변수 BUDGET_GMAIL_CLIENT_SECRET)

스코프는 gmail.readonly 고정 — 이 스크립트는 읽기만 한다.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("BUDGET_CONFIG_DIR") or Path.home() / ".config/banksalad-budget")
CLIENT_SECRET = Path(os.environ.get("BUDGET_GMAIL_CLIENT_SECRET") or CONFIG_DIR / "client_secret.json")
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1"

# 프로필별 토큰 파일. orders 가 비어 있으면 export 토큰을 같이 쓴다.
_TOKEN_FILES = {
    "export": Path(os.environ.get("BUDGET_GMAIL_TOKEN") or CONFIG_DIR / "gmail-token.json"),
    "orders": Path(os.environ["BUDGET_ORDERS_GMAIL_TOKEN"]) if os.environ.get("BUDGET_ORDERS_GMAIL_TOKEN")
    else Path(os.environ.get("BUDGET_GMAIL_TOKEN") or CONFIG_DIR / "gmail-token.json"),
}
_PROFILE = "orders"


def use(profile: str) -> None:
    """이후 호출이 어느 토큰을 쓸지 정한다 (`export` | `orders`)."""
    global _PROFILE
    if profile not in _TOKEN_FILES:
        raise ValueError(f"알 수 없는 프로필: {profile}")
    _PROFILE = profile


def token_file(profile: str | None = None) -> Path:
    return _TOKEN_FILES[profile or _PROFILE]


def available(profile: str | None = None) -> bool:
    """토큰이 있어 이 프로필로 Gmail 을 읽을 수 있는가 — 파이프라인이 보강 단계를 건너뛸지 정할 때 쓴다."""
    return token_file(profile).exists() and CLIENT_SECRET.exists()


# --------------------------------------------------------------------------- auth

def client_config() -> tuple[str, str]:
    if not CLIENT_SECRET.exists():
        sys.exit(f"client_secret 없음: {CLIENT_SECRET}\n(모듈 docstring 의 'GCP OAuth 클라이언트' 절 참조)")
    d = json.loads(CLIENT_SECRET.read_text())
    c = d[next(iter(d))]
    return c["client_id"], c["client_secret"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _post(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(TOKEN_URI, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"토큰 요청 실패 {e.code}: {e.read().decode()[:400]}")


def save_token(tok: dict, profile: str | None = None) -> None:
    p = token_file(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tok, indent=2))
    p.chmod(0o600)


def auth(profile: str = "export", login_hint: str | None = None) -> None:
    """PKCE loopback 플로우. 브라우저가 열리므로 데스크톱에서 1회만 실행한다."""
    client_id, client_secret = client_config()
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    caught: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            caught.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in caught and caught.get("state") == state
            msg = "인증 완료. 터미널로 돌아가세요." if ok else "인증 실패. 터미널을 확인하세요."
            self.wfile.write(f"<html><body style='font:16px sans-serif;padding:40px'>{msg}</body></html>".encode())

        def log_message(self, *a):  # 서버 로그 억제
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    redirect_uri = f"http://localhost:{port}"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",          # refresh_token을 반드시 받기 위해
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"
    print(f"브라우저에서 Gmail 계정으로 로그인하세요 (프로필 {profile}).\n{url}\n")
    webbrowser.open(url)

    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    t.join(timeout=300)
    srv.server_close()

    if "code" not in caught:
        sys.exit(f"인증 코드 미수신 (timeout 또는 거부). 응답: {caught}")
    if caught.get("state") != state:
        sys.exit("state 불일치 — 중단")

    tok = _post({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": caught["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    if "refresh_token" not in tok:
        sys.exit("refresh_token 미발급 — prompt=consent 재시도 필요")
    tok["expiry"] = time.time() + tok.get("expires_in", 3600)
    save_token(tok, profile)
    use(profile)
    email = api_get("/users/me/profile").get("emailAddress")
    print(f"✅ 토큰 저장: {token_file(profile)}\n   계정: {email}")


def access_token() -> str:
    p = token_file()
    if not p.exists():
        sys.exit(f"토큰 없음: {p}\n먼저 `python3 scripts/gmail_client.py --auth` 를 실행하세요.")
    tok = json.loads(p.read_text())
    if tok.get("expiry", 0) > time.time() + 60:
        return tok["access_token"]

    client_id, client_secret = client_config()
    new = _post({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    })
    tok["access_token"] = new["access_token"]
    tok["expiry"] = time.time() + new.get("expires_in", 3600)
    save_token(tok)
    return tok["access_token"]


# --------------------------------------------------------------------------- api

def api_get(path: str, params: dict | None = None) -> dict:
    url = f"{API}{path}"
    if params:
        # doseq=True — metadataHeaders 처럼 리스트 값을 반복 파라미터로 펴야 한다
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Gmail API {e.code} on {path}: {e.read().decode()[:400]}")


def search(query: str, limit: int = 500) -> list[str]:
    """쿼리에 매칭되는 메시지 id 전량 (페이지네이션). Gmail API 는 최신 우선으로 돌려준다."""
    ids: list[str] = []
    token = None
    while len(ids) < limit:
        p = {"q": query, "maxResults": min(500, limit - len(ids))}
        if token:
            p["pageToken"] = token
        d = api_get("/users/me/messages", p)
        ids += [m["id"] for m in d.get("messages", [])]
        token = d.get("nextPageToken")
        if not token:
            break
    return ids


def headers_of(msg_id: str, names: tuple[str, ...] = ("From", "Subject", "Date")) -> dict[str, str]:
    d = api_get(f"/users/me/messages/{msg_id}",
                {"format": "metadata", "metadataHeaders": list(names)})
    return {h["name"]: h["value"] for h in d.get("payload", {}).get("headers", [])}


def _walk_parts(part: dict):
    yield part
    for p in part.get("parts", []):
        yield from _walk_parts(p)


def body_of(msg_id: str) -> tuple[dict[str, str], str]:
    """(헤더, 본문 텍스트). text/plain 우선, 없으면 text/html 태그 제거."""
    d = api_get(f"/users/me/messages/{msg_id}", {"format": "full"})
    payload = d.get("payload", {})
    hdr = {h["name"]: h["value"] for h in payload.get("headers", [])}
    plain, html = "", ""
    for p in _walk_parts(payload):
        data = p.get("body", {}).get("data")
        if not data:
            continue
        raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        if p.get("mimeType") == "text/plain" and not plain:
            plain = raw
        elif p.get("mimeType") == "text/html" and not html:
            html = raw
    if plain.strip():
        return hdr, plain
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = urllib.parse.unquote(text) if "%3C" in text else text
    import html as _html
    text = _html.unescape(text)
    return hdr, re.sub(r"\n{2,}", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def attachments_of(msg_id: str) -> tuple[dict[str, str], list[dict]]:
    """(헤더, [{filename, attachmentId, size}]) — export zip 을 받을 때 쓴다."""
    d = api_get(f"/users/me/messages/{msg_id}", {"format": "full"})
    payload = d.get("payload", {})
    hdr = {h["name"]: h["value"] for h in payload.get("headers", [])}
    atts = [{"filename": p["filename"], "attachmentId": p["body"].get("attachmentId"),
             "size": p["body"].get("size")}
            for p in _walk_parts(payload) if p.get("filename") and p.get("body", {}).get("attachmentId")]
    return hdr, atts


def attachment_bytes(msg_id: str, attachment_id: str) -> bytes:
    d = api_get(f"/users/me/messages/{msg_id}/attachments/{attachment_id}")
    return base64.urlsafe_b64decode(d["data"] + "==")


# --------------------------------------------------------------------------- probe

def probe() -> None:
    """토큰이 살아 있는지, 주문메일이 실제로 몇 건 있는지 실측."""
    for profile in ("export", "orders"):
        use(profile)
        print(f"[{profile}] 토큰 {token_file()} — {'있음' if available() else '없음'}")
        if not available():
            continue
        print(f"  계정: {api_get('/users/me/profile').get('emailAddress')}")
    use("export")
    if available():
        n = len(search("from:export-noreply@banksalad.com has:attachment newer_than:1y", limit=50))
        print(f"  뱅샐 export 메일(1년): {n}건")
    use("orders")
    if available():
        for label, q in (("쿠팡 주문", 'from:coupang.com subject:"주문하신 내역" newer_than:1y'),
                         ("컬리 주문", 'from:kurly.com subject:"주문이 정상적으로 접수" newer_than:1y'),
                         ("네이버페이", "from:navercorp.com newer_than:1y")):
            print(f"  {label}: {len(search(q, limit=1000))}건")


def main() -> None:
    argv = sys.argv[1:]
    profile = argv[argv.index("--profile") + 1] if "--profile" in argv else "export"
    hint = argv[argv.index("--account") + 1] if "--account" in argv else None
    if "--auth" in argv:
        auth(profile, hint)
    elif "--probe" in argv:
        probe()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
