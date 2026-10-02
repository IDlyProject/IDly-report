# IDly 리포트

## 흐름

카카오·Apple 로그인 → 약관 동의 → 홈(내 리포트·구독 금액) → 메일 연결 → 분석 → 결제 전 요약(숫자는 ???) → Lemon Squeezy 결제 → 웹훅으로 열림 → 전체 리포트·PDF

- 가격: 계정마다 첫 메일함 1개 무료, 추가 메일함은 메일함당 REPORT_PRICE. 한 번 열린 메일함은 다시 분석해도 열려 있다
- 결제: 결제창에 custom data(report_id·user_id)를 실어 보내고, 웹훅(서명 확인)으로 그 사용자의 그 메일함만 연다. 환불 웹훅이 오면 다시 잠근다
- 저장: SQLite(db.py) — 사용자·세션·리포트(분석 결과만)·결제·열린 메일함. 메일 비밀번호·원문은 저장하지 않는다
- 약관·정책: /legal/terms, /legal/privacy, /legal/refund (사업자 정보 없이 '운영자' 기준, 판매자는 Lemon Squeezy)

## 출시 전에 채울 것

- 카카오 REST API 키, Apple Services ID (.env.example 참고). 둘 다 없으면 IDLY_DEBUG=1일 때 개발용 로그인만 보인다
- Lemon Squeezy 웹훅 등록과 서명 비밀값. 로컬에서는 웹훅이 들어올 수 없어 ngrok 같은 터널이 필요하다 (IDLY_DEBUG=1이면 결제 화면의 '테스트: 결제 완료 처리'로 흉내 낼 수 있다)
- 개인정보 보호책임자(PRIVACY_OFFICER), 문의 메일(CONTACT_EMAIL)
- 약관·방침 문구 법률 검토

메일 연동 → 무료 요약 → 결제 → 전체 리포트·PDF. IMAP 수집과 계정 탐색(`imap_sync.py`, `analysis.py`, `providers.py`)은 `IDly_PoC/backend`에서 가져왔고, 에이전트 관련 코드는 뺐다.

- 가입 없음. 리포트 id(추측 불가 토큰)가 곧 열람 권한이다. 주소 `/#r=<id>`
- 메일 비밀번호·원문은 저장하지 않는다. 리포트는 메모리에만 두고 `REPORT_TTL_HOURS`(기본 24시간) 뒤 지운다
- PDF는 리포트 HTML을 크로미움(Playwright)으로 A4 인쇄한다. 웹 화면과 같은 HTML

## 분석이 하는 일 (analysis.py)

- 헤더 규칙으로 가입·인증·결제·구독·보안·휴면·회원 고지 신호를 고르고 서비스(도메인)별로 묶는다
- 신호는 없지만 서비스 주소에서 메일이 여러 통 온 곳은 '약한 후보'로, 최근 메일 본문 앞·끝까지 보고 AI가 계정인지 정한다
- 구독: 결제 실패·해지·만료(만료일이 지났는지까지) 안내를 반영한다. AI 판단만으로는 구독으로 보지 않는다
- 보안: 인증 메일 본문을 읽어 실제 '의심 로그인' 안내만 센다. Google '보안 알림'은 본문을 읽어 앱 비밀번호·새 기기 로그인·접근 허용으로 나눈다
- Google 계정 공유 메일로 'Google로 로그인한 서비스' 목록을 만든다
- OpenAI 한도 초과(429)·일시 오류는 기다렸다 다시 보낸다. 실패한 묶음만 규칙 결과로 대신한다
- 실제 Gmail(11,246통) 기준 전체 흐름 약 6분

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 47830
```

http://127.0.0.1:47830 · 메일 없이 보려면 "샘플 리포트 먼저 보기"

## API

| | |
|---|---|
| `POST /api/reports` | 메일 연결 확인 후 탐색 시작 → `{id}` |
| `POST /api/reports/sample` | 샘플 리포트 (`IDLY_SAMPLE=1`) |
| `GET /api/reports/{id}` | 진행 상태 + 무료 요약(숫자만) |
| `POST /api/reports/{id}/checkout` | 결제. `PAYMENT_MODE=mock`이면 바로 결제됨 |
| `GET /api/reports/{id}/view` | 리포트 HTML 조각. 결제 전에는 요약만, 결제 후 전체 |
| `GET /api/reports/{id}/pdf` | 결제 후 PDF (전에는 402) |

## 남은 일

- MoR 결제: checkout에서 결제창 URL 생성, 결제 완료 웹훅에서 `paid=True`
- 리포트가 서버 메모리에만 있다. 재시작하면 사라지고 서버를 여러 대 띄울 수 없다
- 배포(Linux)에는 크로미움과 함께 한글 폰트가 필요하다. 지금은 Google Fonts를 받아 쓰므로 네트워크가 막히면 글자가 깨진다
- 결제 후 리포트를 메일로 보내 주기 (하루 지나면 못 보므로)
