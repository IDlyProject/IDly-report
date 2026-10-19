# 리포트 조치 링크 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 결제 후 리포트의 구독·안 쓰는 계정·보안 알림 행마다 그 조치(해지·탈퇴·보안 설정) 페이지로 가는 버튼을 단다.

**Architecture:** 새 모듈 `action_links.py`가 링크를 고른다(큐레이션 → DB 캐시 → OpenAI 웹 검색 → 구글 검색 대체). 분석 완료 때 네트워크 없이 정해 리포트 데이터에 저장하고, 결제가 확인되면 백그라운드 스레드가 `fallback` 칸만 웹 검색으로 다시 찾은 뒤 저장하고 PDF를 다시 만든다. `report_view.py`는 저장된 값만 그린다.

**Tech Stack:** Python 3 · FastAPI · httpx · SQLite/Postgres(db.py) · unittest (pytest 없음)

**Spec:** `docs/superpowers/specs/2026-10-19-action-links-design.md`

## Global Constraints

- 브랜치 `feat/action-links` (main 기준). 새 환경변수 없음 — `OPENAI_API_KEY`, `OPENAI_MODEL`(analysis.py) 재사용.
- 테스트 실행: `.venv/bin/python -m unittest tests/<file>.py` (레포 루트에서). `unittest discover`는 안 된다.
- 조치 이름은 정확히 `"해지"`, `"탈퇴"`, `"보안"`. `source`는 정확히 `"curated" | "cache" | "search" | "fallback"`.
- 대체 링크: `https://www.google.com/search?q=` + urlencode(`"<서비스명> <조치> 방법"`).
- 검색 상한: 리포트당 최대 20건, 동시 5건, 건당 10초, 전체 20초. 모듈 상수로 둔다.
- 캐시 유효기간 90일. 검증 통과한 `search` 결과만 캐시에 쓴다(`fallback`은 절대 안 씀).
- 검색 질의에는 서비스 이름·도메인·조치만. 사용자 메일 주소·메일 내용 금지.
- 서버가 찾은 URL을 직접 가져오지(fetch) 않는다.
- 링크 단계의 어떤 예외도 리포트 완성·저장·결제 처리를 깨지 않는다.
- 결제 전(잠긴) 미리보기에는 버튼이 없다.
- 커밋 메시지에 Co-Authored-By·"Generated with Claude Code" 넣지 않는다.

## Review Focus

1. **OpenAI가 이상한 응답을 줄 때** (URL 없음, 여러 URL, 마크다운 `[x](url)`, 끝에 `.`/`)` 붙음, 4xx/5xx) → 첫 유효 URL만 꺼내 검증하고, 안 되면 조용히 fallback. → Task 2 `test_search_parses_markdown_and_trailing_punctuation`, `test_search_http_error_falls_back`.
2. **같은 메일함의 여러 리포트가 한 번에 결제됨으로 바뀜** → 바뀐 리포트마다 업그레이드가 한 번씩만 돈다. → Task 3 `test_payment_upgrades_every_newly_paid_report_once`.
3. **업그레이드 중 PDF가 이미 만들어지는 중** → 옛 PDF가 남지 않는다(락 안에서 비우고 다시 예약). → Task 3 `test_invalidate_pdf_waits_for_running_render`.
4. **도메인이 비슷한 피싱 호스트** (`netflix.com.evil.io`, `evilnetflix.com`, 대문자·끝 점 `NETFLIX.COM.`) → 앞 둘은 거절, 마지막은 정규화 후 통과. → Task 1 `test_validate_rejects_lookalikes`.
5. **알려지지 않은 도메인·이름이 HTML 특수문자인 서비스** (`<b>&"`) → 대체 링크 쿼리는 URL 인코딩되고 렌더링에서 이스케이프된다. → Task 4 `test_button_escapes_url_and_label`.

---

## File Structure

- Create `action_links.py` — 큐레이션 표, 허용 도메인, 검증, 고르기, 웹 검색, 리포트 데이터에 붙이기/업그레이드. 리포트 렌더링·app 의존 없음.
- Modify `db.py` — `action_links` 표와 `get_action_link` / `put_action_link`.
- Modify `app.py` — `on_done`, 결제 확인 세 곳, `Report.invalidate_pdf`, 업그레이드 스레드.
- Modify `report_view.py` + `static/report.css` — 버튼.
- Modify `sample_report.json` — `actions`.
- Tests: `tests/test_action_links.py`, `tests/test_action_links_app.py`, `tests/test_report_view_actions.py`.

---

### Task 1: 링크 검증·고르기 (네트워크 없음) + DB 캐시

**Files:**
- Create: `action_links.py`
- Modify: `db.py` (SCHEMA 끝에 표 추가, 함수 2개)
- Test: `tests/test_action_links.py`

**Interfaces:**
- Produces:
  - `ACTIONS = ("해지", "탈퇴", "보안")`
  - `CURATED: Dict[str, Dict[str, Tuple[str, str]]]` — `domain → {action: (url, source_url)}`. 이 태스크에선 테스트용 몇 칸(넷플릭스 해지, Google 보안, Apple)만; 나머지는 Task 5.
  - `ALLOWED_DOMAINS: Dict[str, List[str]]` — `domain → 추가 허용 도메인`. 예: `"youtube.com": ["google.com"]`, `"google.com": ["youtube.com"]`.
  - `base_domain(account_id: str) -> str` — `"apps.apple.com/…"` → `"apple.com"`, 그 밖엔 id 소문자.
  - `is_valid(url: str, domain: str) -> bool`
  - `fallback_link(service: str, action: str) -> Dict[str, str]` → `{"url": ..., "source": "fallback"}`
  - `resolve_offline(account: Dict, action: str, use_cache: bool = True) -> Dict[str, str]` — 큐레이션 → (use_cache면) 캐시 → 대체.
  - `label(action: str, source: str) -> str` — `해지`: `"해지하러 가기"` / fallback `"해지 방법 찾아보기"`; `탈퇴`: `"탈퇴하러 가기"` / `"탈퇴 방법 찾아보기"`; `보안`: `"보안 설정 열기"` / `"보안 설정 방법 찾아보기"`.
  - db.py: `get_action_link(domain: str, action: str) -> Optional[Dict[str, Any]]` (`url, source, found_at`), `put_action_link(domain: str, action: str, url: str, source: str) -> None` (upsert).

- [ ] **Step 1: Write the failing tests**

```python
class ValidateTests(unittest.TestCase):
    def test_validate_accepts_domain_subdomain_and_allowed(self):
        self.assertTrue(al.is_valid("https://netflix.com/cancelplan", "netflix.com"))
        self.assertTrue(al.is_valid("https://www.netflix.com/cancelplan", "netflix.com"))
        self.assertTrue(al.is_valid("https://www.google.com/youtube/x", "youtube.com"))  # ALLOWED_DOMAINS
    def test_validate_rejects_unsafe(self):
        for url in ["http://www.netflix.com/x", "https://user:pw@netflix.com/x", "https://netflix.com:8443/x",
                    "https://1.2.3.4/x", "https://[::1]/x", "https://other.com/x", "javascript:alert(1)", ""]:
            self.assertFalse(al.is_valid(url, "netflix.com"), url)
    def test_validate_rejects_lookalikes(self):
        self.assertFalse(al.is_valid("https://netflix.com.evil.io/x", "netflix.com"))
        self.assertFalse(al.is_valid("https://evilnetflix.com/x", "netflix.com"))
        self.assertTrue(al.is_valid("https://NETFLIX.COM./x", "netflix.com"))

class ResolveOfflineTests(unittest.TestCase):
    # setUp: db 메모리 DB 그대로 사용, 테스트마다 action_links 표 비우기
    def test_curated_wins_over_cache(self): ...  # put_action_link로 다른 URL 넣어도 source == "curated"
    def test_cache_used_when_not_curated(self): ...  # 미등록 도메인 + 캐시 → source "cache"
    def test_cache_expired_after_90_days(self): ...  # found_at을 91일 전으로 직접 UPDATE → source "fallback"
    def test_cache_skipped_when_use_cache_false(self): ...
    def test_fallback_url_is_google_search_with_encoded_query(self):
        link = al.fallback_link("넷플릭스", "해지")
        self.assertEqual(link, {"url": "https://www.google.com/search?q=" + quote_plus("넷플릭스 해지 방법"), "source": "fallback"})
    def test_apple_app_store_subscription(self):
        link = al.resolve_offline({"id": "apps.apple.com/claude", "service": "Claude"}, "해지")
        self.assertEqual(link["url"], "https://apps.apple.com/account/subscriptions")
    def test_labels(self):
        self.assertEqual(al.label("해지", "curated"), "해지하러 가기")
        self.assertEqual(al.label("해지", "fallback"), "해지 방법 찾아보기")
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m unittest tests/test_action_links.py` → `ModuleNotFoundError: action_links`.

- [ ] **Step 3: Implement**
  - db.py SCHEMA에 `CREATE TABLE IF NOT EXISTS action_links (domain TEXT NOT NULL, action TEXT NOT NULL, url TEXT NOT NULL, source TEXT NOT NULL, found_at TEXT NOT NULL, PRIMARY KEY (domain, action));` + 주석("사용자 데이터 아님, 탈퇴 시 지우지 않음"). upsert는 `save_profile`과 같은 `ON CONFLICT … DO UPDATE` 방식, `found_at = _now()`.
  - `is_valid`: `urllib.parse.urlsplit`; scheme `https`; `username`/`password` 없음; `port` 없음(포트 파싱 ValueError도 거절); host 소문자·끝 `.` 제거; `ipaddress.ip_address(host)` 성공하면 거절; host가 허용 집합(`base_domain` + `ALLOWED_DOMAINS`)의 어느 d와 같거나 `"." + d`로 끝나야 통과.
  - 캐시 조회 실패(DB 예외)는 잡아서 캐시 없음으로 본다. 캐시 값도 `is_valid`로 다시 확인.
  - CURATED 값의 출처는 튜플 두 번째 칸(검수용 데이터; 스펙의 "주석"을 테스트 가능한 데이터로 바꾼 것).

- [ ] **Step 4: Run, expect PASS** — 같은 명령, 새 테스트 전부 OK. 기존 `tests/test_feedback.py`도 OK.

- [ ] **Step 5: Commit** — `git add action_links.py db.py tests/test_action_links.py && git commit -m "feat: 조치 링크 검증·고르기와 검색 캐시"`

---

### Task 2: 리포트 데이터에 붙이기 + 웹 검색 업그레이드

**Files:**
- Modify: `action_links.py`
- Test: `tests/test_action_links.py` (클래스 추가)

**Interfaces:**
- Consumes: Task 1 전부.
- Produces:
  - `needed_actions(account: Dict) -> List[str]` — 구독 상태가 `("활성", "만료 예정")`이면 `"해지"`; `unused`면 `recommended == "구독 해지"`일 때 `"해지"` 아니면 `"탈퇴"`; 중복 제거.
  - `attach_offline(report: Dict) -> None` — 각 계정에 `account["actions"] = {action: resolve_offline(...)}` (needed_actions만), 각 `kind == "보안"` insight에 `insight["action"] = security_link(account, insight)`. 이미 있는 값은 덮지 않는다. 예외는 계정 단위로 잡고 그 칸은 fallback.
  - `security_link(account, insight, use_cache=True) -> Dict` — insight `advice`에 `myaccount.google.com/<path>`가 있으면 `{"url": "https://myaccount.google.com/<path>", "source": "curated"}`, 아니면 `resolve_offline(account, "보안")`.
  - `upgrade_report(report: Dict, api_key: Optional[str] = None) -> int` — `source == "fallback"`인 칸(계정 actions + 보안 insight action)을 모아 `(domain, action)` 기준 중복 제거 후 최대 `MAX_SEARCHES = 20`건 검색. 찾아서 검증 통과하면 그 칸들을 `{"url", "source": "search"}`로 바꾸고 `db.put_action_link`. 바꾼 칸 수 반환. `api_key` 기본값은 `os.getenv("OPENAI_API_KEY", "").strip()`; 비면 0 반환, 네트워크 없음.
  - `search_link(service: str, domain: str, action: str, api_key: str) -> Optional[str]` — 검증 통과한 URL 또는 None. 예외 안 던짐.
  - 상수 `MAX_SEARCHES = 20`, `SEARCH_WORKERS = 5`, `SEARCH_TIMEOUT = 10`, `SEARCH_DEADLINE = 20`, `CACHE_DAYS = 90`.

- [ ] **Step 1: Write the failing tests** (httpx.post를 `patch.object(al.httpx, "post", fake)`로 대체; fake는 호출 인자를 기록하고 `httpx.Response(200, json=..., request=httpx.Request("POST", url))` 반환)

```python
def test_needed_actions(self): ...  # 활성 구독 → ["해지"]; unused+recommended "구독 해지" → ["해지"]; unused 기타 → ["탈퇴"]; 둘 다 아님 → []
def test_attach_offline_sets_every_needed_slot_and_security_insights(self): ...  # 모든 슬롯 존재, 네트워크 호출 0
def test_google_insight_uses_specific_page(self): ...  # advice "...myaccount.google.com/apppasswords에서..." → https://myaccount.google.com/apppasswords
def test_upgrade_only_searches_fallback_slots(self): ...  # curated 칸은 질의 안 함
def test_upgrade_replaces_and_caches_valid_result(self): ...  # source "search", get_action_link 결과 있음
def test_upgrade_rejects_offdomain_result_and_does_not_cache(self): ...  # 그대로 fallback, 캐시 없음
def test_search_parses_markdown_and_trailing_punctuation(self): ...  # 응답 텍스트 "[해지](https://www.netflix.com/cancelplan)." → "https://www.netflix.com/cancelplan"
def test_search_http_error_falls_back(self): ...  # 500 응답과 httpx.TimeoutException 둘 다 → 칸 유지, 예외 없음
def test_no_api_key_means_no_network(self): ...
def test_cap_twenty_searches(self): ...  # fallback 칸 25개(서로 다른 도메인) → post 호출 20번, 나머지 5개 fallback 유지
def test_query_never_contains_user_email(self): ...  # account["email"] = "me@example.com" → 모든 요청 body에 "me@example.com" 없음
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: needed_actions`).

- [ ] **Step 3: Implement**
  - 요청: `POST https://api.openai.com/v1/responses`, `{"model": OPENAI_MODEL(analysis에서 import 말고 os.getenv("OPENAI_MODEL","gpt-4.1")), "tools": [{"type": "web_search"}], "input": <질의>}`, 헤더 `Authorization: Bearer`, `timeout=SEARCH_TIMEOUT`. 질의 문구: `f"{service}({domain}) 계정의 {action} 페이지 공식 URL 하나만 답해. 공식 도움말이나 {domain} 페이지만. 모르면 NONE."`(보안은 "비밀번호·로그인 보안 설정 페이지").
  - 응답 파싱: `output[]` 중 `type == "message"`의 `content[]`에서 `type == "output_text"`의 `text`와 `annotations[].url`(url_citation)을 후보로, 텍스트 안 URL은 정규식 `https://[^\s)\]>"'<]+` 뒤 `.,;:!?)` 제거. 순서대로 `is_valid` 첫 통과값.
  - 동시성: `ThreadPoolExecutor(SEARCH_WORKERS)` + `concurrent.futures.wait(futures, timeout=SEARCH_DEADLINE)`; 안 끝난 건 버리고 `shutdown(wait=False, cancel_futures=True)`.

- [ ] **Step 4: Run, expect PASS** (+ test_feedback).

- [ ] **Step 5: Commit** — `git commit -m "feat: 리포트 데이터에 조치 링크 붙이기와 결제 후 웹 검색"`

---

### Task 3: app.py 연결 (분석 완료·결제 확인·PDF 재생성)

**Files:**
- Modify: `app.py` (`Report` 클래스, `start_report.on_done`, `checkout` mock, `paddle_webhook`, `dev_confirm`)
- Test: `tests/test_action_links_app.py`

**Interfaces:**
- Consumes: `action_links.attach_offline`, `action_links.upgrade_report`.
- Produces (app.py):
  - `Report.invalidate_pdf(self) -> None` — `_pdf_lock` 안에서 `_pdf = None`, 그다음 `prepare_pdf()`.
  - `Report._links_upgraded: bool` (초기 False), 모듈 락 `_upgrade_lock`.
  - `_upgrade_links(report: Report) -> None` — 락 안에서 플래그 확인·세팅(이미 True면 return), 데이터 없거나 샘플이면 return, 백그라운드 `threading.Thread(daemon=True)`에서 `upgrade_report(report.data)` → 바뀐 게 있으면 `report.save()` + `report.invalidate_pdf()`. 스레드 안 예외는 `print`로만.
  - `_mark_paid(report: Report) -> None` — 기존 세 곳의 "같은 메일함 리포트들 paid=True" 루프를 이 함수로 모으고, `paid`가 False→True로 바뀐 리포트(그리고 결제한 리포트 자신)마다 `_upgrade_links`. 환불은 기존 루프 그대로(링크 안 건드림).
  - `on_done`: `try: attach_offline(_data) except: print` → `report.save()` → `if report.paid: _upgrade_links(report)` → `report.prepare_pdf()`.

- [ ] **Step 1: Write the failing tests** (`patch.object(app, "upgrade_report")`로 검색 대체 — app.py는 `from action_links import attach_offline, upgrade_report`로 가져온다. 스레드는 테스트에서 `patch.object(app.threading, "Thread")`로 즉시 실행하는 가짜를 쓴다)

```python
def test_mock_checkout_triggers_upgrade(self): ...  # PAYMENT_MODE="mock", 게스트 헤더로 리포트 생성 후 POST /checkout → upgrade_report 1회
def test_dev_confirm_triggers_upgrade(self): ...   # DEBUG=True
def test_paddle_paid_webhook_triggers_upgrade(self): ...  # billing.verify_signature/parse_webhook patch
def test_refund_does_not_upgrade_or_revert(self): ...
def test_payment_upgrades_every_newly_paid_report_once(self): ...  # 같은 user·메일함 리포트 2개 + dev-confirm 두 번 → 리포트마다 1회
def test_unpaid_report_never_searches(self): ...  # on_done 경로(on_done을 직접 부를 수 있게 start_report의 on_done 로직을 `_finish_report(report, data)`로 뺀다) → upgrade 0회, actions 존재
def test_paid_at_start_searches_on_done(self): ...
def test_upgrade_failure_keeps_report(self): ...  # upgrade_report가 raise → 예외 전파 없음, data 유지
def test_invalidate_pdf_waits_for_running_render(self): ...  # _pdf_lock을 잡은 채 invalidate_pdf를 다른 스레드에서 호출 → 락 놓은 뒤 _pdf None
```

  → `on_done` 로직은 `_finish_report(report: Report, data: Dict[str, Any]) -> None`으로 빼고 `on_done = lambda d: _finish_report(report, d)`.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** (위 Interfaces 그대로).
- [ ] **Step 4: Run all three test files + test_feedback, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: 분석 완료·결제 확인 때 조치 링크 정하기"`

---

### Task 4: 렌더링 + 샘플

**Files:**
- Modify: `report_view.py` (`_subscriptions`, `_unused`, `_todo`), `static/report.css`, `sample_report.json`
- Test: `tests/test_report_view_actions.py`

**Interfaces:**
- Consumes: `action_links.resolve_offline(..., use_cache=False)`, `security_link(..., use_cache=False)`, `label`.
- Produces: `_action_button(link: Dict[str, str], action: str) -> str` →
  `<a class="rp-action" href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(label)}</a>` (fallback이면 클래스 `rp-action is-search` 추가).
  - 링크 값이 저장돼 있지 않으면(옛 리포트) 그릴 때 `use_cache=False`로 정한다 — DB·네트워크 안 씀.
  - `_subscriptions`: 각 행 `rp-row-body` 안 끝에 `해지`. `_unused`: 행마다 needed 조치(`해지`/`탈퇴`). `_todo`: `kind == "보안"` 행에만 `보안`.
  - CSS: 작은 알약 버튼, `var(--main)` 계열, `rp-advice` 밑에 붙는 형태(빌더 재량). `is-search`는 테두리만.

- [ ] **Step 1: Write the failing tests**

```python
def test_full_report_has_buttons_in_three_sections(self): ...  # 구독 해지·안 쓰는 계정 탈퇴·보안 행 각각 class="rp-action"
def test_locked_preview_has_no_buttons(self): ...  # render_fragment(full=False)에 "rp-action" 없음
def test_fallback_label_differs(self): ...  # source fallback → "해지 방법 찾아보기", class에 is-search
def test_button_escapes_url_and_label(self): ...  # 서비스명 '<b>&"' → href에 원문 '<b>' 없음, &amp; 등 이스케이프
def test_old_report_without_actions_still_has_buttons(self): ...  # actions 키 없는 데이터, db 함수 patch해 호출 0 확인
def test_links_have_noopener(self): ...
def test_sample_report_renders_curated_buttons(self): ...  # sample_report.json 전체 렌더에 rp-action ≥ 1, fallback 없음(샘플 계정 id가 이름이라 actions를 json에 직접 넣는다)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** + sample_report.json 각 해당 계정에 `actions`(그리고 보안 insight에 `action`)를 Task 5의 큐레이션과 같은 URL, `source: "curated"`로 넣는다.
- [ ] **Step 4: Run, expect PASS** (+ 전체).
- [ ] **Step 5: 눈으로 확인** — `IDLY_SAMPLE=1 .venv/bin/python -m uvicorn app:app --port 8010` 실행 후 `POST /api/reports/sample` → `GET /api/reports/<id>/view` HTML에 버튼이 보이는지, `/pdf`가 열리고 링크가 눌리는지.
- [ ] **Step 6: Commit** — `git commit -m "feat: 리포트에 해지·탈퇴·보안 버튼"`

---

### Task 5: 큐레이션 목록 채우기

**Files:**
- Modify: `action_links.py` (`CURATED`, `ALLOWED_DOMAINS`)
- Test: `tests/test_action_links.py` (클래스 추가)

**Interfaces:** Task 1의 `CURATED` 형태 그대로.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_curated_url_passes_validation(self):
    for domain, actions in al.CURATED.items():
        for action, (url, source) in actions.items():
            self.assertIn(action, al.ACTIONS)
            self.assertTrue(al.is_valid(url, domain), (domain, action, url))
            self.assertTrue(source.startswith("https://"), (domain, action))
def test_curated_keys_are_known_services(self):
    self.assertLessEqual(set(al.CURATED), set(analysis.KNOWN_SERVICES))
def test_curated_covers_major_subscriptions(self):
    for d in ["netflix.com", "youtube.com", "spotify.com", "coupang.com", "tving.com", "watcha.com", "melon.com", "notion.so", "openai.com", "adobe.com"]:
        self.assertIn("해지", al.CURATED.get(d, {}), d)
```

  (마지막 목록은 조사 결과 공식 근거를 못 찾은 서비스가 있으면 거기서 빼고, PR에 이유를 적는다 — 추측으로 채우지 않는다.)

- [ ] **Step 2: Run, expect FAIL** (major subscriptions 미충족).
- [ ] **Step 3: 조사·채우기** — `KNOWN_SERVICES` 130개를 몇 묶음으로 나눠 병렬 조사(WebSearch/WebFetch). 기준: 서비스 공식 도움말·공식 페이지에서 그 URL(또는 그 URL로 가는 메뉴 경로)을 직접 본 경우만. 칸마다 `(url, 출처 URL)`. 확인 못 한 칸은 빈다. 같은 서비스의 별칭 도메인(`kakaocorp.com`, `navercorp.com`, `behance.com`, `onstove.com`, `pokemon.com`, `nianticlabs.com`)은 대표 도메인 값을 그대로 참조. 기준 도메인 밖 링크가 필요하면 `ALLOWED_DOMAINS`에 추가.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: 서비스별 해지·탈퇴·보안 링크 큐레이션"`

---

### Task 6: 문서·PR

- [ ] README에 조치 링크 동작 한 단락(순서, 결제 후 검색, 상한, 캐시 표).
- [ ] 전체 테스트: `.venv/bin/python -m unittest tests/test_feedback.py tests/test_action_links.py tests/test_action_links_app.py tests/test_report_view_actions.py` → OK.
- [ ] 커밋, `git push -u origin feat/action-links`, `gh pr create --base main` — 템플릿 `## 📌 작업 개요 / ## ✅ 작업 상세 내용 / ## 🔍 테스트 및 확인 사항 / ## 📂 관련 이슈 / ## 🙋 기타 참고 사항`. 기타에: 큐레이션 출처 검수 요청, 근거 못 찾아 비운 칸 수, 결제 직후 몇 초 fallback 보임, PR #4(보안)와 독립.
