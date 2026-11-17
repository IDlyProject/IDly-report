# CDP Extension Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. 사용자가 채팅에서 제안한 브라우저 제어층 개발을 승인했으며 이 세션에서 직접 구현한다.

**Goal:** AX 스냅샷과 검증된 요소 참조로 기존 리포트 확장 브라우저 제어를 강화한다.
**Architecture:** Chrome debugger/CDP adapter → 기존 작업 워커 → 서버의 제한된 판단 계약. 사용자의 계정·로그인은 기존 Chrome 안에 유지한다.
**Tech Stack:** Manifest V3, JavaScript, Chrome CDP, FastAPI/Pydantic, Node test, unittest.
**Spec:** `docs/superpowers/specs/2026-11-05-cdp-extension-design.md`.

## Global Constraints

실제 API·사용자 프로필·외부 계정 조작 금지. 테스트 DB는 메모리. 최종 해지·탈퇴 확인과 기존 소유권·동의 검사를 유지한다. 사용자 작성 .omc와 환경변수 파일은 변경하지 않는다.

## Review Focus

확정 대기 중 화면 변경, 프레임 재사용/다른 도메인 이동, input value 유출, debugger 해제 뒤 실행, 서버가 허용하지 않은 입력/클릭 모두 각각 실패 테스트로 고정한다.

### Task 1 — 브라우저 인터페이스

Files: `extension/browser-session.js`, `tests/browser-session.test.cjs`.
- [x] transport fake 테스트를 먼저 작성하고 기능 미구현 실패 확인.
- [x] snapshot/click/fill/scroll/goto/close, AX ref 매핑·프레임·이벤트·검증 구현.
- [x] 단위 테스트 통과 확인.

### Task 2 — 실행 워커와 판단 계약

Files: `extension/background.js`, `extension/manifest.json`, `actions/extension_runs.py`, `actions/ai_agent.py`, `app.py`, 기존 확장 테스트.
- [x] 로그인·확정·새 참조 동작 및 입력/스크롤 판단 실패 테스트 작성.
- [x] 워커를 BrowserSession에 연결하고 안전한 서버 응답·계약 구현.
- [x] 전체 Node/Python 회귀 테스트 통과 확인.

### Task 3 — 실물 검증·문서·리뷰

Files: `README.md`, `scripts/check_extension_browser.py` (로컬 fixture 포함).
- [x] 격리된 테스트 브라우저에서 실제 CDP 클릭·입력·프레임·화면 변경 검증.
- [x] 권한·지원 범위·확장 다시 로드 방법과 검증 한계 문서화.
- [x] 독립 리뷰, 의미 있는 지적 수정, 전체 테스트 재검증.

검증 결과: Python unittest 73개, Node 13개 통과. 임시 프로필의 실제 Chromium에서 CDP 스냅샷·입력·클릭, 같은 프로세스/OOP iframe, 상위 가림막 차단, stale 요소 거부, Shadow DOM 클릭·입력, 인증 화면/section 접두 OTP 제외를 확인했다. 리뷰에서 발견한 프레임 가림막, autocomplete 토큰, Shadow DOM, 클릭 좌표 불일치를 수정했다. 실제 서비스·OpenAI 호출과 배포는 실행하지 않았다.

### Task 4 — Playwright CRX 도입

- [x] playwright-crx 0.15.0, esbuild 0.28.2 정확 버전과 lockfile, 설치 스크립트 비활성화.
- [x] 기존 BrowserSession 인터페이스와 승인/프라이버시 검사를 유지한 어댑터 연결.
- [x] CRX 라이선스와 NOTICE, 원본 무수정 빌드 패치·업그레이드 실패 검사.
- [x] 실제 Chrome에서 변환 요소, OOP iframe, 잘못된 노드 선택, 대기 중 의미 변경, 팝업 연결 검사.
- [x] 입력 포커스 변경 방어와 최종 회귀/독립 리뷰 마무리.

회귀 테스트 유효성: 입력 직전 검사만 제거한 생성 번들에서는 대기 중 버튼 의미 변경 테스트가 실패함을 확인했고 정상 빌드로 복원했다.

최종 CRX 검증: npm ci --ignore-scripts 후 재빌드 성공, npm 감사 알려진 취약점 0건. Python 73개·Node 13개 통과. 격리 Chrome 153의 로컬 HTTPS fixture 17개 검증 통과(일반/OOP iframe 클릭·입력, 가림막, 변환 좌표, 고정 노드 identity, 대기 중 의미 변경, 포커스 탈취, 미채택 팝업, 인증·입력값 제외 포함). 독립 리뷰 지적 4건 수정 후 추가 중요 지적 없음. 실제 계정·OpenAI API·배포·푸시 없음.
