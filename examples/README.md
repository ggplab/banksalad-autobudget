# 정기 실행 예시 (n8n 없이)

`pipeline.py run --from-gmail` 은 새 export 메일이 없으면 `found:false` 로 즉시 끝난다(락도 안 잡는다).
그래서 15분마다 돌려도 부담이 없다.

## cron (Linux · macOS)

```cron
# 15분마다 Gmail 폴링 → 새 export 있으면 전 단계 실행
*/15 * * * * cd /home/you/banksalad-autobudget && .venv/bin/python pipeline.py run --from-gmail >> ~/budget-pipeline.log 2>&1
```

`.env` 는 pipeline.py 가 스스로 읽으므로 cron 환경변수를 따로 손댈 필요 없다.
LLM 백엔드가 `claude`/`codex`/`gemini` CLI 면 cron 에서는 PATH 에 그 바이너리가 없을 수 있다 —
`PATH=/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin` 을 crontab 첫 줄에 둔다.

## launchd (macOS)

`examples/com.example.autobudget.plist` 를 `~/Library/LaunchAgents/` 에 복사하고 경로 두 곳을 고친 뒤:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.autobudget.plist
launchctl kickstart -k gui/$(id -u)/com.example.autobudget     # 즉시 1회
```

- 로그는 `~/Library/Logs/` 에 둔다. `/tmp` 는 macOS 가 주기적으로 비운다.
- launchd 는 `~/.zshenv` 를 읽지 않는다. 필요한 PATH 는 plist `EnvironmentVariables` 에 명시했다.
- 은퇴시킬 때는 `launchctl bootout` 만으로 끝나지 않는다 — plist 파일을 지우거나 이름을 바꿔야 다음 로그인에 되살아나지 않는다.

## 재태깅 (룰 yaml 을 고친 뒤)

```bash
.venv/bin/python pipeline.py run --retag
```

export 없이 태깅부터 다시 돈다. n8n 이면 `Manual 재태깅` 트리거 또는 `POST /webhook/budget-retag`.
