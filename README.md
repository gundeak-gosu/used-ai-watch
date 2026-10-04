# 🛒 중고 AI 알리미 (웹 서비스판)

웹사이트에서 로그인 → **프로젝트(찾는 물건)** 를 만들면, 30분마다 당근(설정한 동네)·번개장터·중고나라·N플리마켓 매물을 모아
AI가 본문까지 읽고 "내 조건에 맞는 정도 + 가격 가치"로 점수를 매겨 비교하고, 좋은 매물이 TOP3에 들면 메일로 알려줍니다.
샀으면 **마감** → 프로젝트와 기록이 삭제되고 알림이 멈춥니다.

- 웹사이트: `docs/index.html` (GitHub Pages)
- 로그인·데이터: Supabase (`projects`, `project_state` 테이블, RLS로 본인 것만 보기/만들기/마감)
- 봇: `bot.py` — GitHub Actions가 30분마다 실행 (`.github/workflows/watch.yml`)
- 공통 설정: `settings.yaml` (AI 모델, 점수 가중치, 요약 메일 시각 등)

## 구조
```
[웹사이트] --로그인/프로젝트 생성·마감--> [Supabase]
                                            ^  |
                       bot_save_project()   |  | bot_list_projects()  (봇 전용 토큰으로만 호출 가능)
                                            |  v
                                  [GitHub Actions: bot.py] --메일--> 프로젝트 주인
```

## GitHub Secrets (관리자)
| 이름 | 값 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase publishable(공개) 키 |
| `BOT_TOKEN` | 봇 전용 토큰 (DB에는 해시만 저장) |
| `SMTP_USER` / `SMTP_PASS` | 알림 보낼 Gmail 주소 / 앱 비밀번호 |
| `ANTHROPIC_API_KEY` | Claude API 키 |
| `ANTHROPIC_WORKSPACE_ID` | (조직 단위 키일 때만) 워크스페이스 ID |

## 제한 (비용 보호)
- 1인당 프로젝트 5개, 전체 50개 (DB 트리거)
- 프로젝트당 키워드 5개, 당근 동네 3개
- 새 프로젝트 첫 회차 AI 평가 최대 120개, 이후 회차당 30개
- 알림 메일은 가입한 계정 메일로만 발송(남의 주소로 스팸 방지)

## 알아두기
- AI 판단은 판매글 정보와 AI의 시세 지식 기준이라 틀릴 수 있어요. 최종 확인은 직접!
- 당근이 해외(GitHub 서버) 접속을 막으면 웹의 상태 줄에 ⚠️가 표시됩니다.
- 개인·소규모 용도로, 지금 간격 정도로 가볍게 쓰세요. 사이트 구조가 바뀌면 코드 수정이 필요할 수 있습니다.
