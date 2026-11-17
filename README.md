# IDly 리포트

## 흐름

카카오·Apple 로그인 → 약관 동의 → 홈(내 리포트·구독 금액) → 메일 연결 → 분석 → 결제 전 요약(숫자는 ???) → Paddle 결제 → 웹훅으로 열림 → 전체 리포트·PDF

- 가격: 메일함마다 REPORT_PRICE (무료 메일함 없음). 한 번 결제한 메일함은 다시 분석해도 열려 있다
- 결제: 결제창에 custom data(report_id·user_id)를 실어 보내고, 웹훅(서명 확인)으로 그 사용자의 그 메일함만 연다. 환불 웹훅이 오면 다시 잠근다
- 저장: SQLite(db.py) — 사용자·세션·리포트(분석 결과만)·결제·열린 메일함. 메일 비밀번호·원문은 저장하지 않는다
- 약관·정책: /legal/terms, /legal/privacy, /legal/refund (사업자 정보 없이 '운영자' 기준, 판매자는 Paddle)

## 출시 전에 채울 것

- 카카오 REST API 키, Apple Services ID (.env.example 참고). 둘 다 없으면 IDLY_DEBUG=1일 때 개발용 로그인만 보인다
- Paddle 웹훅(Notification destination) 등록과 서명 비밀값, 결제 도메인 승인. 로컬에서는 웹훅이 들어올 수 없어 ngrok 같은 터널이 필요하다 (IDLY_DEBUG=1이면 결제 화면의 '테스트: 결제 완료 처리'로 흉내 낼 수 있다)
- 개인정보 보호책임자(PRIVACY_OFFICER), 문의 메일(CONTACT_EMAIL)
- 에러 제보용 `DISCORD_WEBHOOK_URL`: IDly-Back과 같은 웹훅 URL을 배포 환경변수에 설정 (`DISCORD_WEBHOOK_AVATAR_URL`은 선택)
- 통계용 `GA_MEASUREMENT_ID`(GA4 측정 ID): 넣으면 화면 전환·퍼널 이벤트를 보낸다 (`static/analytics.js`). GA4 관리 > 데이터 스트림 > 향상된 측정에서 '브라우저 방문 기록 이벤트에 따른 페이지 변경'은 꺼 둔다 (주소의 리포트 id가 넘어가지 않게). GA4 데이터 보관 기간은 개인정보처리방침에 적은 14개월과 맞춘다
- 약관·방침 문구 법률 검토

메일 연동 → 무료 요약 → 결제 → 전체 리포트·PDF. IMAP 수집과 계정 탐색(`imap_sync.py`, `analysis.py`, `providers.py`)은 `IDly-PoC/backend`에서 가져왔다. 사용자 크롬 확장 에이전트도 별도 모듈로 이관했다.

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

## 조치 링크 (action_links.py)

결제 후 리포트의 구독·안 쓰는 계정·보안 알림 행마다 해지·탈퇴·보안 설정 페이지로 가는 버튼을 단다.
링크는 큐레이션(공식 도움말에서 확인한 것, 칸마다 출처) → 검색 캐시(DB `action_links`, 90일) →
OpenAI 웹 검색 → 구글 검색 링크 순서로 고른다. 웹 검색은 **결제한 리포트에서만** 결제 확인 직후
백그라운드로 돌고(리포트당 최대 20건·20초), 찾은 링크는 서비스 자기 도메인일 때만 쓴다.
설계는 `docs/superpowers/specs/2026-10-19-action-links-design.md`.

## 브라우저 확장

PoC(`IDly-PoC@cac1fd1`)의 Manifest V3 확장과 서버 판단·작업 모듈을 이관했다.

1. 서버에서 `IDLY_EXTENSION=1`, `OPENAI_API_KEY`를 설정한다. 기본값은 비활성이다.
   모델은 `OPENAI_AGENT_MODEL` → `OPENAI_MODEL` → `gpt-4o-mini` 순서로 정한다.
2. 처음 설치할 때 `npm ci --ignore-scripts`, `npm run build:extension`을 실행한다.
   **Chrome 125 이상**의 `chrome://extensions`에서 개발자 모드를 켜고 **압축해제된 확장 프로그램 로드**로
   이 저장소의 `extension/` 폴더를 선택한다. 설치 후 리포트 페이지를 새로고침한다.
3. 결제된 본인 리포트의 해지·탈퇴 링크 옆 **내 크롬에서 진행**을 누른다.
   화면 일부와 버튼·링크 목록의 IDly·OpenAI 전송에 동의하면 새 탭에서 작업한다.
   로그인·본인인증은 직접 하고 최종 해지·탈퇴는 사용자 확정 후 진행한다.
4. 리포트의 작업 패널이나 작업 탭에서 이어서 진행·확정·취소할 수 있다.
   샘플·미결제 리포트와 PDF에는 확장 실행 버튼이 붙지 않는다. 보안 설정은 기존 링크를 이용한다.

웹 연결 주소는 `extension/manifest.json`의 `content_scripts.matches`에 명시한다.
현재 로컬 47830 포트와 `idly.kr`, `www.idly.kr`, `idly.up.railway.app`을 지원한다.
주소를 추가하면 확장도 다시 로드해야 한다. 다른 사이트를 조작하기 위한 `<all_urls>` 권한이 필요하다.
0.3.0은 Playwright CRX를 사용한다. 빌드 후 기존 설치도 확장을 다시 로드하고 리포트 페이지를 새로고침한다.
작업 탭을 CDP에 연결할 때 Chrome의 디버깅 안내가 표시된다. 연결을 해제하면 작업이 멈추며,
같은 탭에 DevTools나 다른 디버거가 연결되어 있으면 연결에 실패할 수 있다.

브라우저 접근은 `extension/browser-session.js`가 담당한다. 접근성 트리(AX)에서 버튼의 역할·이름과
스냅샷별 참조를 만들고, 클릭 직전 같은 문서·프레임·요소인지 다시 확인한다. 확인 대기 중 화면이 바뀌면
이전 승인을 재사용하지 않는다. 같은 서비스의 iframe과 별도 프로세스 iframe을 지원하고, 다른 도메인의
내용과 링크는 제외한다. 클릭·입력은 CRX의 고정 ElementHandle로 수행하며, 클릭 전 요소가 보이고 안정되고
가려지지 않았는지 확인한다. 실제 노드의 backend ID와 승인한 노드를 대조하고, 물리 입력 직전마다 화면 참조와
취소 명령을 다시 검사한다. CSS transform 자체를 이유로 일괄 차단하지 않는다.
일반 텍스트 입력(최대 200자)과 상하 스크롤을 지원한다. 인증·결제 입력 화면은 사용자에게 맡기며,
입력값과 편집 영역의 자식 텍스트, 자체 작업 패널은 판단용 스냅샷에서 제외한다.
모델은 제한된 동작만 요청할 수 있으며 임의 JavaScript나 CDP 명령을 보내지 못한다.

- `extension/`: 페이지 연결(`bridge.js`), 탭 실행·로그인 대기·확정 패널(`background.js`).
- `actions/`: AI 판단, 도메인·최종 확인 검사, 작업별 토큰과 명령 큐. 클라우드 브라우저 실행기는 포함하지 않는다.
- `static/extension.js`: 리포트의 확장 감지, 동의, 시작, 진행 상태와 명령 UI.
- `POST/GET /api/reports/{report_id}/extension-runs`: 소유권·결제 확인 후 작업 생성/목록.
- `POST /api/reports/{report_id}/extension-runs/{run_id}/commands`: 웹의 이어서 진행·확정·취소.
- `/api/agent/{run_id}/decide`, `/status`, `/commands`: 작업별 Bearer 토큰을 사용하는 확장 전용 API.

작업 상태·토큰은 PoC처럼 서버 메모리에만 있다. 단일 서버 프로세스를 전제로 하며 서버나 확장 서비스 워커가
재시작되면 자동 복구하지 않는다. 작업 토큰은 1시간 뒤 만료된다. 장기 저장·재시작 복구·배포 인스턴스 간 공유는 후속 확장 범위다.
페이지 내용 일부는 서버·OpenAI로 전송하지만 쿠키·비밀번호 입력값은 수집하지 않고, 비밀번호 입력 화면은 판단 API로 보내지 않는다.

로컬 검증(실제 OpenAI·계정 사이트 호출 없음):

```sh
DATABASE_URL='' IDLY_DB=:memory: OPENAI_API_KEY='' DISCORD_WEBHOOK_URL='' .venv/bin/python -m unittest discover -s tests -v
npm ci --ignore-scripts
npm run build:extension
npm test
.venv/bin/python scripts/check_extension_browser.py
```

마지막 검증은 Playwright Chromium·cryptography가 설치된 개발 환경에서 실행한다. 임시 프로필과 로컬 HTTPS
fixture만 사용하며 외부 페이지 요청은 차단한다. 실제 서비스의 로그인·해지 완료 여부는 이 테스트의 범위에 포함하지 않는다.

오픈소스 적용: `playwright-crx@0.15.0`(Apache-2.0)과 빌드 도구 버전을 `package-lock.json`으로 고정한다.
원본 패키지를 직접 편집하지 않고 `extension/crx-driver.mjs` 어댑터를 사용한다. Chrome 153에서 재현된
이미 열린 OOP iframe의 초기 상태 처리, 입력 직전 검사, 팝업 연결 소유권은 `scripts/crx-compat.mjs`의
버전 제한 빌드 패치로 관리한다. 실제 backend ID 확인에는 이 버전에 포함된 내부 `_toImpl` 연결부를 사용한다.
버전이나 패치 대상 코드가 달라지면 빌드가 실패하므로, 업그레이드 때 어댑터와 패치를 함께 검토한다.
빌드가 생성한 `extension/vendor/`에는 라이브러리와 라이선스·NOTICE·번들 저작권 안내가 포함된다.
이 디렉터리는 Git에서 제외하므로 확장을 배포·전달할 때 반드시 빌드 결과 전체를 포함한다.
기존 CDP 엔진은 명시적인 `{ engine: 'cdp' }` 옵션으로 비교·복귀할 수 있으나 자동 fallback은 하지 않는다.
이 도입은 브라우저 제어층의 개선이며 서비스별 완료 판정과 서버 재시작 복구는 별도로 구현해야 한다.

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 47830
```

http://127.0.0.1:47830 · 메일 없이 보려면 "샘플 리포트 먼저 보기"

## 배포 DB

- Vercel에는 Supabase PostgreSQL 연결 문자열을 `DATABASE_URL` 환경변수로 등록한다. IDly_PoC와 같은 Supabase 프로젝트를 쓸 수 있으며, 이 앱은 기존 `idly` 스키마와 분리된 `idly_report` 스키마를 사용한다.
- 로컬은 `DATABASE_URL`이 없으면 SQLite를 사용한다. 연결 문자열은 `.env`에만 두고 커밋하지 않는다.

## API

|                                   |                                                    |
| --------------------------------- | -------------------------------------------------- |
| `POST /api/reports`               | 메일 연결 확인 후 탐색 시작 → `{id}`               |
| `POST /api/reports/sample`        | 샘플 리포트 (`IDLY_SAMPLE=1`)                      |
| `GET /api/reports/{id}`           | 진행 상태 + 무료 요약(숫자만)                      |
| `POST /api/reports/{id}/checkout` | 결제. `PAYMENT_MODE=mock`이면 바로 결제됨          |
| `GET /api/reports/{id}/view`      | 리포트 HTML 조각. 결제 전에는 요약만, 결제 후 전체 |
| `GET /api/reports/{id}/pdf`       | 결제 후 PDF (전에는 402)                           |
| `POST /api/feedback`             | 화면 내 제보 버튼 → Discord (이미지 최대 5장, IP당 시간당 3회) |

## 남은 일

- MoR 결제: checkout에서 결제창 URL 생성, 결제 완료 웹훅에서 `paid=True`
- 리포트가 서버 메모리에만 있다. 재시작하면 사라지고 서버를 여러 대 띄울 수 없다
- 배포(Linux)에는 크로미움과 함께 한글 폰트가 필요하다. 지금은 Google Fonts를 받아 쓰므로 네트워크가 막히면 글자가 깨진다
- 결제 후 리포트를 메일로 보내 주기 (하루 지나면 못 보므로)
