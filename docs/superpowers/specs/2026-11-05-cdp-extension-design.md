# CDP 브라우저 제어 확장

사용자가 승인한 개발 범위: Aside의 브라우저 접근 방식을 참고해 `IDly-report`의 확장을 발전시킨다. 기존 `feat/browser-extension` 브랜치(d85ceb3)에서 작업한다. 별도 브라우저·임의 JavaScript REPL·배포는 포함하지 않는다.

## 구조

- `extension/browser-session.js`: `chrome.debugger`로 작업 탭에 연결. `snapshot`, `click`, `fill`, `scroll`, `goto`, `close`를 제공하는 고정된 브라우저 인터페이스. CDP 명령은 모델에 직접 노출하지 않는다.
- `extension/background.js`: 작업 실행·사용자 로그인 대기·최종 확인·취소를 담당. DOM 번호 대신 최신 스냅샷 요소 참조로 브라우저 모듈을 호출한다.
- 서버의 판단 계약: 기존 번호 선택을 내부 모델 응답으로 유지하되, 검증 후 해당 스냅샷의 요소 참조와 snapshotId로 변환해 돌려준다. 클릭·일반 입력·위/아래 스크롤을 지원한다.
- MV3 classic worker에서 importScripts로 모듈 로드. Chrome 125 이상, debugger 권한을 명시한다. 세션·토큰·서버 상태의 영속화는 이번 작업 범위 밖이다.

## 화면과 동작

- AX(접근성) 트리에서 역할·이름·상태를 읽고 불필요한 중간 노드를 제외한다. 기존 서버 한도인 요소 150개·본문 2000자를 유지하며 잘렸다는 정보를 명시한다.
- backendNodeId·프레임·CDP 세션 정보는 확장에만 둔다. 서버에는 짧은 요소 참조·라벨·역할·비활성/편집 가능 상태를 보낸다.
- 주 프레임과 허용한 서비스 도메인의 iframe을 읽는다. OOP iframe은 CDP child session으로 처리한다. 외부 로그인 도메인·팝업에서는 사용자에게 직접 조작을 맡긴다.
- 이동·탭 전환·프레임 교체·debugger 해제 때 이전 참조를 무효화한다. 클릭·입력 전에 노드의 역할·이름·주소·상태를 다시 확인한다. 달라졌으면 실행하지 않고 다시 읽는다.
- 클릭·입력은 Playwright CRX 0.15.0 어댑터가 담당한다. 고정 ElementHandle의 backend ID를 승인한 노드와 대조하고, 가림막·안정성 검사 및 대기/재시도 후 물리 입력 직전 참조를 다시 확인한다. 기존 CDP 엔진은 비교·복귀용 명시 옵션으로 남긴다. 입력은 일반 텍스트 필드만 대상으로 한다. 입력값을 스냅샷에 포함하지 않는다. 비밀번호·OTP·결제 정보·로그인/인증 필드는 자동 입력하지 않는다.
- 비밀번호/인증 입력 화면은 판단 API로 보내지 않고 기존 사용자 로그인 대기로 전환한다. URL의 query/fragment와 input value는 모델 전송에서 제외한다.
- 페이지 이동·팝업·탭 종료·프레임 이벤트를 세션 상태에 반영한다. 클릭 후 실제 화면을 다시 읽어 다음 판단을 요청한다.
- 기존 도메인 제한·결제/소유권/동의·최종 탈퇴/해지 확인은 유지한다. debugger 연결 실패 시 불확실한 DOM fallback 실행 대신 실패 사유와 직접 처리 방법을 알려준다.

## 검증

- Node 테스트: 실제 모듈 + CDP transport만 fake. 역할/참조 매핑, 프레임 구분, 새 문서·교체된 요소·외부 주소·debugger 해제 시 거부, 일반 입력·비밀 입력 차단, 스크롤, 연결 해제 검증.
- 백그라운드 통합 테스트: 로그인 → 확인 → 실행 → 완료. 모듈 테스트에서 화면·문서 변경 시 이전 참조 실행 거부를 검증하며, 서버 테스트에서 확정 명령을 현재 스냅샷에 결합한다. 팝업 전환은 구현되어 있으나 실제 서비스 팝업 로그인은 검증하지 않는다.
- Python 테스트: 판단 계약, 범위 밖 이동/요소·입력 거부, 기존 소유권·동의·결제 검사 회귀. OpenAI 요청은 차단/모킹한다.
- 가능하면 격리된 로컬 HTTPS fixture와 Chromium에서 실제 CDP 스냅샷·클릭·입력·iframe 흐름을 검증한다. 사용자의 실제 브라우저 프로필·계정·메일·외부 사이트는 사용하지 않는다.
- 코드 독립 리뷰 뒤 전체 Node/Python 회귀 테스트. 실제 API 호출·커밋·푸시·배포는 하지 않는다.

## 참고

- https://aside.com/blog/how-we-built-the-sota-browser-agent-that-outperforms-fable : 공개 구조 설명만 참고. 소스 복제나 설치 없음.
- https://developer.chrome.com/docs/extensions/reference/api/debugger : CDP transport·flat iframe session.
- https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/ : AX 트리.

## Playwright CRX 적용 (2026-11-05)

사용자 승인으로 브라우저 제어층에 playwright-crx 0.15.0을 적용했다. npm lockfile, 어댑터, 재현 가능한 로컬 빌드와 라이선스/NOTICE를 관리한다. Chrome 153의 preloaded OOP iframe 초기화 호환과 물리 입력 직전 검사, 팝업 연결 소유권은 버전/원본 문자열 검사로 제한한 빌드 패치다. `_toImpl` 내부 연결부로 선택한 핸들의 실제 backend ID를 확인하므로 업그레이드 때 통합 검증이 필요하다. 승인/동의/인증 정보 제외 계약은 유지한다. 기존 서버의 AI 완료 판정·재시작 복구는 이번 도입으로 해결되지 않는다.
