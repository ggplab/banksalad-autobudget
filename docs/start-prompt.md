# 시작 프롬프트 — AI 코딩 에이전트에게 세팅을 맡길 때

Claude Code · Codex CLI · Gemini CLI 중 어느 것이든 repo 루트에서 열고 아래를 그대로 붙여 넣는다.
에이전트가 README 의 flowchart 를 따라 질문하고, `.env`·룰 파일을 채우고, 샘플 → 실데이터 순으로 돌린다.

---

```
이 repo(banksalad-budget-organizer)를 내 환경에 맞게 세팅하고 첫 실행까지 해줘. README.md 를 먼저 읽어.

순서:
1. Python 3.10+ 확인 후 .venv 만들고 requirements.txt 설치.
2. 샘플로 파이프라인이 도는지 먼저 확인: `pipeline.py run --file samples/sample-export.xlsx --dry-run`
   → dist/budget.html 이 생기면 통과.
3. 나에게 아래를 한 번에 물어보고 답을 .env 에 반영해 (cp .env.example .env 부터):
   a. Trigger — export 파일을 매번 직접 넘길지(파일 드롭), Gmail 에서 자동 수신할지.
      Gmail 이면 scripts/gmail_client.py docstring 의 GCP OAuth 절차를 안내하고 `--auth` 를 같이 돌려.
   b. 보강 — 쿠팡·컬리·네이버페이 주문메일이 같은 Gmail 계정에 있는지, 다른 계정인지, 안 쓸지.
   c. LLM — 지금 네가 돌고 있는 CLI 를 백엔드로 쓸지(claude/codex/gemini), OpenRouter 키를 쓸지, 끌지.
   d. Target — html / email / discord / hook / artifact 중 무엇. artifact 면 실행 후 dist/budget.html 을
      이 세션의 아티팩트로 올려서 보여줘.
   e. 귀속 — 개인만인지, 사업자가 있어서 결제수단별로 개인/사업을 가를지. 있으면 rules/attribution.yaml 을 만들어.
4. rules/merchant_rules.example.yaml 을 rules/merchant_rules.yaml 로 복사하고, 내 뱅샐 export 의 가맹점 상위 30개를
   보여주면서 룰을 같이 채워 (내가 답하는 대로).
5. 실데이터로 `pipeline.py run --file <내 export>` 실행. 결과 카드(stderr 마지막 블록)와 dist/budget.html 요약을 보여줘.
6. 정기 실행을 원하면 examples/ 의 cron 또는 launchd 를 내 경로로 맞춰 설치해줘. n8n 을 쓰면 n8n/budget-pipeline.json
   임포트 절차를 안내해.

규칙: 비밀값(zip 비번·API 키·앱 비밀번호)은 네가 대신 입력하지 말고 내가 .env 에 직접 넣게 해. data/ 와 dist/ 는
git 에 올리지 마. 모르는 건 추측 말고 물어봐.
```

---

## 에이전트별 메모

| 에이전트 | LLM 백엔드 | 비고 |
|---|---|---|
| Claude Code | `BUDGET_LLM_BACKEND=claude` | `claude -p --tools ""` 로 순수 판정기 호출. 구독이면 추가 비용 없음 |
| Codex CLI | `BUDGET_LLM_BACKEND=codex` | `codex exec -o <file>` 로 마지막 메시지를 받는다 |
| Gemini CLI | `BUDGET_LLM_BACKEND=gemini` | `gemini -p` stdout 에서 JSON 을 뽑는다 |

세 백엔드 모두 응답에서 `{"results":[...]}` JSON 만 추려 쓰고, 허용 라벨 밖·확신도 범위 밖·모르는 id 는 버린다.
헤드리스(ssh·cron·launchd)에서 CLI 가 로그인 정보를 못 읽으면 `openrouter` 로 두는 게 빠르다.
