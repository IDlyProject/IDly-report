"""IDly 리포트 백엔드: 로그인 → 메일 연동 → 분석·리포트·PDF까지 미리 만들어 둠 → 요약 → 결제(Lemon Squeezy) → 전체 리포트·PDF.

- 카카오·Apple로 로그인한 사용자만 리포트를 만들고 본다. 리포트는 만든 사람만 볼 수 있다 (샘플 제외).
- 메일 비밀번호와 메일 원문은 저장하지 않는다. 분석 결과만 DB(db.py)에 둔다.
- 결제는 PAYMENT_MODE=lemonsqueezy면 결제창 → 웹훅으로 연다. mock이면 바로 결제된 것으로 본다 (개발용).
"""

import imaplib
import json
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import traceback
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import auth  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
from imap_sync import MAX_PER_FOLDER, Credential, SyncJob, connect, start_sync  # noqa: E402
from providers import PROVIDERS, explain_login_error, get_provider, resolve_provider  # noqa: E402
from report_view import render_fragment, render_html, render_pdf, summarize, summarize_many, _money_line  # noqa: E402

PRICE = int(os.getenv("REPORT_PRICE", "4900"))
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "lemonsqueezy")
# 보관 기간: 결제 안 한 리포트는 짧게, 결제한 리포트는 사용자가 지우거나 탈퇴할 때까지 (최대 1년)
UNPAID_DAYS = int(os.getenv("UNPAID_REPORT_DAYS", "7"))
PAID_DAYS = int(os.getenv("PAID_REPORT_DAYS", "365"))
SAMPLE_ENABLED = os.getenv("IDLY_SAMPLE", "1") == "1"
ID_LOGIN_PROVIDERS = {"naver", "nate"}
# 1이면 실패 응답에 debug(메일 서버 원문 오류·시도한 사용자 이름·스택)를 붙이고 개발용 로그인을 연다. 배포 때는 0
DEBUG = os.getenv("IDLY_DEBUG", "1") == "1"
SESSION_COOKIE = "idly_session"
SECURE_COOKIE = auth.APP_URL.startswith("https://")
HERE = Path(__file__).parent

# 운영·문의 정보 (약관·푸터에 들어간다). 사업자 없이 운영하고, 판매·결제는 Lemon Squeezy가 판매자(MoR)로 처리한다
BUSINESS = {
    "name": os.getenv("OPERATOR_NAME", "IDly 팀"),
    "email": os.getenv("CONTACT_EMAIL", "idly1apt@gmail.com"),
    "privacy_officer": os.getenv("PRIVACY_OFFICER", ""),
    "seller": "Lemon Squeezy, LLC (Merchant of Record)",
    "effective_date": os.getenv("TERMS_EFFECTIVE_DATE", "2026년 10월 2일"),
}

app = FastAPI(title="IDly report")


class ApiError(Exception):
    """사용자에게 보여줄 메시지 + 디버그 정보(DEBUG일 때만 응답에 붙는다). 비밀번호는 절대 넣지 않는다."""

    def __init__(self, status: int, message: str, debug: Optional[Dict[str, Any]] = None):
        self.status, self.message, self.debug = status, message, debug or {}


@app.exception_handler(ApiError)
def _api_error(request: Request, exc: ApiError):
    print(f"[api] {request.method} {request.url.path} -> {exc.status} {exc.message} {json.dumps(exc.debug, ensure_ascii=False, default=str)}")
    body: Dict[str, Any] = {"detail": exc.message}
    if DEBUG:
        body["debug"] = exc.debug
    return JSONResponse(body, status_code=exc.status)


@app.exception_handler(Exception)
def _unexpected(request: Request, exc: Exception):
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"[api] {request.method} {request.url.path} -> 500\n{trace}")
    body: Dict[str, Any] = {"detail": "서버에서 문제가 생겼어요. 잠시 후 다시 시도해주세요."}
    if DEBUG:
        body["debug"] = {"error": repr(exc), "traceback": trace}
    return JSONResponse(body, status_code=500)


# --- 리포트 (메모리 + DB) ---------------------------------------------------------

class Report:
    def __init__(self, email: str, user_id: Optional[str], job: Optional[SyncJob] = None,
                 data: Optional[Dict[str, Any]] = None, report_id: Optional[str] = None):
        self.id = report_id or secrets.token_urlsafe(16)
        self.user_id = user_id
        # 서버가 다시 켜져서 진행 중이던 분석이 끊겼다
        self.interrupted = False
        self.email = email
        self.job = job
        self._data = data
        self.paid = False
        self.created = datetime.now()
        self._pdf: Optional[bytes] = None
        self._pdf_lock = threading.Lock()

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        return self._data if self._data is not None else (self.job.report if self.job else None)

    @property
    def sample(self) -> bool:
        return bool(self.data and self.data.get("sample"))

    @property
    def status(self) -> str:
        if self.data is not None:
            return "완료"
        if self.interrupted:
            return "중단됨"
        return self.job.status if self.job else "실패"

    def save(self) -> None:
        if not self.sample:
            db.save_report(self.id, self.user_id, self.email, self.created, self.data, self.paid)

    def pdf(self) -> bytes:
        with self._pdf_lock:
            if self._pdf is None:
                self._pdf = render_pdf(render_html(self.data, self.email, self.created))
            return self._pdf

    def prepare_pdf(self) -> None:
        """결제 전에 PDF를 미리 만들어 둔다. 결제는 다 만든 리포트를 받는 값이라, 결제하면 바로 내려받게 한다.
        찾은 계정이 없으면 팔 것이 없으니 만들지 않는다."""
        if self.data is None or self._pdf is not None or not summarize(self.data)["accounts"]:
            return

        def run() -> None:
            try:
                self.pdf()
            except Exception as e:  # PDF는 내려받을 때 다시 만들어 볼 수 있다
                print(f"[pdf] {self.id} 미리 만들기 실패: {e!r}")

        _pdf_pool.submit(run)


_reports: Dict[str, Report] = {}
# Playwright(크로뮴)는 무거워서 PDF는 한 번에 하나씩 만든다
_pdf_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf")


def _load_saved() -> None:
    """서버를 켤 때 DB의 리포트를 불러온다. 결과가 없는(분석 중에 끊긴) 것은 '중단됨'으로."""
    for row in db.load_reports():
        report = Report(row["mailbox"], row["user_id"], data=row["data"], report_id=row["id"])
        report.created = datetime.fromisoformat(row["created"])
        report.paid = bool(row["paid"])
        report.interrupted = row["data"] is None
        _reports[report.id] = report
        report.prepare_pdf()


_load_saved()


def _purge() -> None:
    now = datetime.now()
    for rid in db.delete_reports_before(now - timedelta(days=UNPAID_DAYS), paid=False) + \
            db.delete_reports_before(now - timedelta(days=PAID_DAYS), paid=True):
        _reports.pop(rid, None)
    for rid in [rid for rid, r in _reports.items() if r.sample and r.created < now - timedelta(days=1)]:
        _reports.pop(rid, None)


# --- 로그인 ------------------------------------------------------------------------

def _user(request: Request) -> Optional[Dict[str, Any]]:
    return db.session_user(request.cookies.get(SESSION_COOKIE, ""))


def _require_user(request: Request, terms: bool = True) -> Dict[str, Any]:
    user = _user(request)
    if user is None:
        raise ApiError(401, "로그인이 필요해요.")
    if terms and not user["terms_at"]:
        raise ApiError(403, "약관 동의가 필요해요.")
    return user


@app.get("/auth/kakao")
def kakao_start():
    if not auth.KAKAO_KEY:
        raise ApiError(404, "카카오 로그인이 설정되지 않았어요.")
    state = auth.new_state()
    response = RedirectResponse(auth.kakao_authorize_url(state))
    response.set_cookie("idly_oauth_state", state, max_age=600, httponly=True, samesite="lax", secure=SECURE_COOKIE)
    return response


def _popup_result(user: Optional[Dict[str, Any]], message: str = "") -> HTMLResponse:
    """카카오 로그인 팝업 창의 마지막 화면: 원래 창에 결과를 알리고 닫힌다. 팝업이 아니면 홈으로 간다."""
    payload = json.dumps({"type": "idly-login", "ok": user is not None, "message": message}, ensure_ascii=False)
    fallback = "/#login-error" if user is None else ("/#terms" if not user["terms_at"] else "/#home")
    response = HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>IDly 로그인</title>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh">로그인하는 중이에요…</p>
<script>
  if (window.opener) {{ window.opener.postMessage({payload}, location.origin); window.close(); }}
  else {{ location.replace({json.dumps(fallback)}); }}
</script>""")
    if user is not None:
        response.set_cookie(SESSION_COOKIE, db.create_session(user["id"]), max_age=db.SESSION_DAYS * 86400,
                            httponly=True, samesite="lax", secure=SECURE_COOKIE)
    return response


@app.get("/auth/kakao/callback")
def kakao_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        # 사용자가 카카오 동의 화면에서 취소한 경우도 여기로 온다
        return _popup_result(None, "카카오 로그인을 취소했어요." if error == "access_denied" else "카카오 로그인에 실패했어요.")
    if not state or state != request.cookies.get("idly_oauth_state"):
        print("[auth] kakao state mismatch")
        return _popup_result(None, "로그인 요청이 만료됐어요. 다시 시도해 주세요.")
    try:
        profile = auth.kakao_profile(code)
    except auth.AuthError as e:
        print(f"[auth] {e}")
        return _popup_result(None, "카카오 로그인에 실패했어요.")
    return _popup_result(db.upsert_user("kakao", profile["uid"], profile["email"], profile["name"]))


@app.get("/auth/apple/config")
def apple_config():
    """Apple JS 팝업에 넣을 공개 설정과 nonce. nonce는 쿠키에도 두고 토큰 검증 때 맞춰 본다."""
    if not auth.APPLE_CLIENT_ID:
        raise ApiError(404, "Apple 로그인이 아직 설정되지 않았어요.")
    # Apple은 Services ID에 등록한 https 주소에서만 로그인 팝업을 연다
    if not auth.APPLE_WEB_REDIRECT_URI.startswith("https://"):
        raise ApiError(400, "Apple 로그인은 https 주소에서만 쓸 수 있어요. 지금은 카카오로 로그인해 주세요.",
                       {"redirect_uri": auth.APPLE_WEB_REDIRECT_URI})
    nonce, state = auth.new_state(), auth.new_state()
    response = JSONResponse({"client_id": auth.APPLE_CLIENT_ID, "redirect_uri": auth.APPLE_WEB_REDIRECT_URI,
                             "nonce": nonce, "state": state})
    response.set_cookie("idly_oauth_nonce", nonce, max_age=600, httponly=True, samesite="lax", secure=SECURE_COOKIE)
    return response


class AppleToken(BaseModel):
    id_token: str
    user: Optional[Dict[str, Any]] = None  # 첫 로그인 때만 이름·이메일이 온다


@app.post("/auth/apple/token")
def apple_token(body: AppleToken, request: Request):
    try:
        profile = auth.apple_profile(body.id_token, request.cookies.get("idly_oauth_nonce", ""),
                                     json.dumps(body.user) if body.user else None)
    except auth.AuthError as e:
        raise ApiError(401, "Apple 로그인에 실패했어요. 다시 시도해 주세요.", {"error": str(e)})
    user = db.upsert_user("apple", profile["uid"], profile["email"], profile["name"])
    response = JSONResponse({"next": "terms" if not user["terms_at"] else "home"})
    response.set_cookie(SESSION_COOKIE, db.create_session(user["id"]), max_age=db.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=SECURE_COOKIE)
    response.delete_cookie("idly_oauth_nonce")
    return response


class MockLogin(BaseModel):
    provider: str


@app.post("/auth/mock")
def mock_login(body: MockLogin, request: Request):
    """목업 로그인: 버튼을 누른 서비스 이름의 테스트 계정으로 로그인한다 (AUTH_MODE=mock일 때만).
    계정은 브라우저마다 따로 만든다 (기기 쿠키). 배포해도 다른 사람의 리포트가 보이지 않는다."""
    if auth.AUTH_MODE != "mock" or body.provider not in auth.MOCK_PROVIDERS:
        raise ApiError(404, "없는 기능이에요.")
    device = request.cookies.get("idly_device") or secrets.token_urlsafe(16)
    profile = auth.mock_profile(body.provider, device)
    user = db.upsert_user(f"mock-{body.provider}", profile["uid"], profile["email"], profile["name"])
    response = JSONResponse({"next": "terms" if not user["terms_at"] else "home"})
    response.set_cookie(SESSION_COOKIE, db.create_session(user["id"]), max_age=db.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=SECURE_COOKIE)
    response.set_cookie("idly_device", device, max_age=365 * 86400, httponly=True, samesite="lax", secure=SECURE_COOKIE)
    return response


@app.get("/api/me")
def me(request: Request):
    user = _user(request)
    profile = user and db.get_profile(user["id"])
    return {
        "user": user and {"id": user["id"], "provider": user["provider"], "email": user["email"],
                          "name": user["name"], "terms": bool(user["terms_at"]), "marketing": bool(user["marketing"]),
                          "profile_asked": profile is not None, "profile": profile},
        "login": auth.providers(DEBUG),
    }


# 내부 통계용 선택 정보: 고를 수 있는 값 (화면도 이 목록으로 그린다)
PROFILE_OPTIONS = {
    "age": ["10대", "20대", "30대", "40대", "50대", "60대 이상"],
    "gender": ["여성", "남성", "기타"],
    "job": ["학생", "직장인", "자영업", "프리랜서", "주부", "구직 중", "기타"],
}


class ProfileBody(BaseModel):
    age: Optional[str] = None
    gender: Optional[str] = None
    job: Optional[str] = None


@app.post("/api/profile")
def save_profile(body: ProfileBody, request: Request):
    """모두 비워서 보내면 건너뛰기(또는 지우기)."""
    user = _require_user(request)
    values = {}
    for key, options in PROFILE_OPTIONS.items():
        value = getattr(body, key)
        if value is not None and value not in options:
            raise ApiError(400, "고를 수 없는 값이에요.")
        values[key] = value
    db.save_profile(user["id"], **values)
    return {"ok": True}


class TermsBody(BaseModel):
    terms: bool = False
    privacy: bool = False
    age14: bool = False
    marketing: bool = False


@app.post("/api/terms")
def agree(body: TermsBody, request: Request):
    user = _require_user(request, terms=False)
    if not (body.terms and body.privacy and body.age14):
        raise ApiError(400, "필수 항목에 모두 동의해 주세요.")
    db.agree_terms(user["id"], body.marketing)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    db.delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.delete("/api/me")
def withdraw(request: Request):
    """회원 탈퇴: 사용자·리포트·결제 기록·세션을 지운다."""
    user = _require_user(request, terms=False)
    for rid in [rid for rid, r in _reports.items() if r.user_id == user["id"]]:
        _reports.pop(rid, None)
    db.delete_user(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- 설정·메일 서비스 -------------------------------------------------------------

@app.get("/api/config")
def config():
    return {"price": PRICE, "payment_mode": PAYMENT_MODE, "sample": SAMPLE_ENABLED, "debug": DEBUG,
            "contact_email": BUSINESS["email"], "business": BUSINESS, "profile_options": PROFILE_OPTIONS}


@app.get("/api/providers/detect")
def detect(email: str):
    """주소 도메인으로 모르는 메일(학교·회사)은 메일 서버(MX)로 서비스를 알아낸다."""
    provider = resolve_provider(email)
    keys = ("id", "name", "domains", "host", "auth", "steps", "password_label")
    # guessed: 메일 서버로 확실히 안 게 아니라 SPF로 추정했다 (화면에서 사용자가 바꿀 수 있게 알린다)
    return {**{k: provider[k] for k in keys}, "guessed": bool(provider.get("guessed"))}


@app.get("/api/providers")
def providers():
    keys = ("id", "name", "domains", "host", "auth", "steps", "password_label")
    return [{k: p[k] for k in keys} for p in PROVIDERS]


# --- 리포트 -----------------------------------------------------------------------

def _get(report_id: str, request: Request) -> Report:
    report = _reports.get(report_id)
    # 샘플은 누구나, 나머지는 만든 사람만
    if report is None or (not report.sample and (_user(request) or {}).get("id") != report.user_id):
        raise ApiError(404, "리포트를 찾지 못했어요. 지웠거나 보관 기간이 지났을 수 있어요.")
    return report


def _paid(report_id: str, request: Request) -> Report:
    report = _get(report_id, request)
    if report.data is None:
        raise ApiError(409, "아직 분석이 끝나지 않았어요.")
    if not report.paid:
        raise ApiError(402, "결제 후 볼 수 있어요.")
    return report


class StartBody(BaseModel):
    email: str
    password: str
    provider: Optional[str] = None
    host: Optional[str] = None
    port: int = 993
    # 마스킹한 메일 요약을 OpenAI(국외)로 보내 분석하는 데 동의
    consented: bool = False


@app.get("/api/my/reports")
def my_reports(request: Request):
    user = _require_user(request)
    items = []
    for row in db.user_reports(user["id"]):
        report = _reports.get(row["id"])
        if report is None:
            continue
        s = summarize(report.data) if report.data is not None else None
        items.append({
            "id": report.id, "mailbox": report.email, "created": report.created.isoformat(timespec="minutes"),
            "status": report.status, "paid": report.paid,
            "summary": s and {"accounts": s["accounts"], "subscriptions": s["subscriptions"], "security": s["security"],
                              "money": _money_line(s)},
        })
    # 홈 합계: 메일함마다 가장 최근에 끝난 리포트 하나씩 (다시 분석한 메일함을 두 번 세지 않는다)
    latest: Dict[str, Report] = {}
    for item in items:
        report = _reports[item["id"]]
        key = report.email.lower()
        if report.data is not None and (key not in latest or report.created > latest[key].created):
            latest[key] = report
    total = summarize_many([r.data for r in latest.values()]) if latest else None
    if total:
        total["money"] = _money_line(total)
    return {"reports": items, "total": total}


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: str, request: Request):
    report = _get(report_id, request)
    if report.sample:
        raise ApiError(400, "샘플은 지울 수 없어요.")
    _reports.pop(report.id, None)
    db.delete_report(report.id)
    return {"ok": True}


@app.post("/api/reports")
def start_report(body: StartBody, request: Request):
    user = _require_user(request)
    _purge()
    provider = get_provider(body.provider) if body.provider else resolve_provider(body.email)
    email = body.email.strip()
    # 붙여 넣을 때 앞뒤에 딸려 온 공백은 뺀다
    password = body.password.strip()
    # Google 앱 비밀번호는 "abcd efgh ijkl mnop"처럼 띄어 써서 보여 준다. 실제 값에는 공백이 없다
    if provider and provider["id"] in ("gmail", "google_workspace"):
        password = password.replace(" ", "")
    host = (body.host or (provider or {}).get("host") or "").strip()
    # 디버그 정보: 비밀번호 자체는 넣지 않고, 흔한 입력 실수(한글 자판, 공백)만 알려 준다
    debug: Dict[str, Any] = {
        "email": email,
        "provider": provider and {"id": provider["id"], "name": provider["name"]},
        "provider_from": "client" if body.provider else "server",
        "host": host, "port": body.port,
        "password": {
            "length": len(password),
            "trimmed_whitespace": password != body.password,
            "has_non_ascii": any(ord(c) > 127 for c in password),
            "has_inner_space": " " in password,
        },
        "attempts": [],
    }
    if not body.consented:
        raise ApiError(400, "메일 분석 동의가 필요해요.", debug)
    if provider is None:
        raise ApiError(400, "알 수 없는 메일 서비스예요.", debug)
    if "password" not in provider["auth"]:
        raise ApiError(400, f"{provider['name']}은 비밀번호로 메일을 읽는 방식을 막아 두어 연동할 수 없어요. 다른 메일 주소로 해주세요.", debug)
    if not host:
        raise ApiError(400, "IMAP 서버 주소를 입력해주세요.", debug)
    # 메일 서버 로그인은 영문·숫자·기호만 보낼 수 있다. 한/영 전환 없이 입력한 경우가 많다
    if debug["password"]["has_non_ascii"]:
        raise ApiError(400, "비밀번호에 한글 같은 문자가 들어 있어요. 한/영 키를 확인하고 다시 입력해주세요.", debug)
    # 네이버·네이트는 IMAP 사용자 이름이 '아이디'다. 아이디로 먼저, 안 되면 전체 주소로 한 번 더 시도한다
    usernames = [email.split("@")[0], email] if provider["id"] in ID_LOGIN_PROVIDERS else [email]
    # 연결이 되는지 먼저 확인하고 탐색은 백그라운드로 돈다. 서버 원문 오류는 로그에만 남긴다
    cred, error = None, None
    for username in usernames:
        attempt = Credential(email, host, body.port, username, password)
        started = time.time()
        try:
            connect(attempt).logout()
            cred = attempt
            debug["attempts"].append({"username": username, "result": "ok", "ms": int((time.time() - started) * 1000)})
            break
        except imaplib.IMAP4.error as e:
            raw = e.args[0].decode(errors="replace") if e.args and isinstance(e.args[0], bytes) else str(e)
            debug["attempts"].append({"username": username, "result": "auth_failed", "server_said": raw,
                                      "ms": int((time.time() - started) * 1000)})
            error = e
            # 설정이 꺼져 있는 등 사용자 이름과 무관한 실패면 다시 시도하지 않는다
            if "username" not in raw.lower() and "invalid credentials" not in raw.lower():
                break
        except OSError as e:
            debug["attempts"].append({"username": username, "result": "connect_failed", "error": repr(e),
                                      "ms": int((time.time() - started) * 1000)})
            raise ApiError(502, f"{host}:{body.port} 메일 서버에 연결하지 못했어요. 서버 주소와 포트를 확인해주세요.", debug)
    if cred is None:
        raise ApiError(401, explain_login_error(str(error), provider), debug)
    report = Report(cred.email, user["id"])
    # 메일함마다 한 번 결제한다. 결제한 메일함은 다시 분석해도 열려 있다
    report.paid = db.unlock_of(user["id"], cred.email) == "paid"
    # 분석이 끝나면 결과를 DB에 쓴다 (서버를 다시 켜도 남게)
    # 분석이 끝나면 결과를 DB에 쓰고, 결제 전에 PDF까지 만들어 둔다
    def on_done(_data: Dict[str, Any]) -> None:
        report.save()
        report.prepare_pdf()

    report.job = start_sync(cred, MAX_PER_FOLDER, owner_id=0, on_done=on_done)
    _reports[report.id] = report
    report.save()
    return {"id": report.id}


@app.post("/api/reports/sample")
def sample_report():
    if not SAMPLE_ENABLED:
        raise ApiError(404, "샘플이 꺼져 있어요.")
    _purge()
    data = json.loads((HERE / "sample_report.json").read_text(encoding="utf-8"))
    report = Report("sample@idly.kr", None, data=data)
    # 샘플은 결제 후 받게 될 전체 리포트를 그대로 보여준다
    report.paid = True
    _reports[report.id] = report
    return {"id": report.id}


@app.get("/api/reports/{report_id}")
def report_status(report_id: str, request: Request):
    report = _get(report_id, request)
    job = report.job.to_dict() if report.job else {}
    data = report.data
    return {
        "id": report.id,
        "email": report.email,
        "status": report.status,
        "step": job.get("step"),
        "step_done": job.get("step_done"),
        "step_total": job.get("step_total"),
        "total": job.get("total"),
        "fetched": job.get("fetched"),
        "error": job.get("error") or ("서버가 다시 시작되면서 진행 중이던 분석이 멈췄어요." if report.interrupted else None),
        "debug": job.get("debug") if DEBUG else None,
        "paid": report.paid,
        "sample": report.sample,
        "price": PRICE,
        "summary": summarize(data) if data is not None else None,
    }


@app.post("/api/reports/{report_id}/checkout")
def checkout(report_id: str, request: Request):
    user = _require_user(request)
    report = _get(report_id, request)
    if report.data is None:
        raise ApiError(409, "아직 분석이 끝나지 않았어요.")
    # 찾은 계정이 없으면 팔 것이 없다
    if not summarize(report.data)["accounts"]:
        raise ApiError(409, "찾은 계정이 없어서 결제할 내용이 없어요.")
    if report.paid:
        return {"paid": True}
    if PAYMENT_MODE == "mock":
        report.paid = True
        report.save()
        return {"paid": True}
    try:
        url = billing.create_checkout(report.id, user["id"], user.get("email"), f"{auth.APP_URL}/#r={report.id}")
    except billing.BillingError as e:
        raise ApiError(502, "결제창을 열지 못했어요. 잠시 후 다시 시도해주세요.", {"error": str(e)})
    return {"paid": False, "url": url}


@app.post("/api/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request):
    raw = await request.body()
    if not billing.verify_signature(raw, request.headers.get("X-Signature", "")):
        print("[billing] webhook signature mismatch")
        return JSONResponse({"detail": "invalid signature"}, status_code=401)
    event = billing.parse_webhook(raw)
    if event is None:
        return {"ok": True, "ignored": True}
    report = _reports.get(event["report_id"])
    if report is None or (event["user_id"] and event["user_id"] != report.user_id):
        print(f"[billing] webhook for unknown report {event}")
        return {"ok": True, "ignored": True}
    if event["status"] in ("paid", "refunded"):
        changed = db.record_payment(event["order_id"], report.id, report.user_id, event["status"],
                                    event["amount"], event["currency"], event["event"])
        # 같은 메일함의 다른 리포트도 같이 열거나 잠근다
        for other in _reports.values():
            if other.user_id == report.user_id and other.email.lower() == report.email.lower():
                other.paid = event["status"] == "paid"
        print(f"[billing] {event['event']} order={event['order_id']} report={report.id} paid={report.paid} "
              f"test={event['test_mode']} changed={changed}")
    return {"ok": True}


@app.post("/api/reports/{report_id}/dev-confirm")
def dev_confirm(report_id: str, request: Request):
    """개발용: 로컬에서는 결제사 웹훅이 들어올 수 없으니 결제 완료를 흉내 낸다 (IDLY_DEBUG=1일 때만)."""
    if not DEBUG:
        raise ApiError(404, "없는 기능이에요.")
    _require_user(request)
    report = _get(report_id, request)
    db.record_payment(f"dev-{report.id}", report.id, report.user_id, "paid", PRICE, "KRW", "dev_confirm")
    for other in _reports.values():
        if other.user_id == report.user_id and other.email.lower() == report.email.lower():
            other.paid = True
    return {"paid": True}


@app.get("/api/reports/{report_id}/view")
def report_view(report_id: str, request: Request):
    """화면에 끼워 넣을 리포트 HTML 조각. 결제 전에는 요약 카드와 숫자만 준다."""
    report = _get(report_id, request)
    if report.data is None:
        raise ApiError(409, "아직 분석이 끝나지 않았어요.")
    return {"paid": report.paid, "html": render_fragment(report.data, report.created, full=report.paid)}


@app.get("/api/reports/{report_id}/pdf")
def report_pdf(report_id: str, request: Request):
    report = _paid(report_id, request)
    name = f"IDly-report-{report.created:%Y%m%d}.pdf"
    return Response(report.pdf(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --- 약관·정책 페이지 --------------------------------------------------------------

LEGAL_PAGES = {"terms": "이용약관", "privacy": "개인정보처리방침", "refund": "환불 정책"}


@app.get("/legal/{page}", response_class=HTMLResponse)
def legal(page: str):
    if page not in LEGAL_PAGES:
        raise ApiError(404, "없는 페이지예요.")
    body = (HERE / "static" / "legal" / f"{page}.html").read_text(encoding="utf-8")
    # {{name}} 같은 자리에 사업자 정보를 넣는다 (비어 있으면 '준비 중')
    body = re.sub(r"\{\{(\w+)\}\}", lambda m: escape(BUSINESS.get(m.group(1)) or "준비 중"), body)
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{LEGAL_PAGES[page]} · IDly</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="stylesheet" href="/static/style.css?v={ASSET_VERSION}"></head>
<body><div class="app"><main class="screen legal">
<header class="bar-head"><a class="back" href="/" aria-label="처음으로"><img src="/static/assets/ic-back.svg" alt="" width="13.3333" height="13.3095"></a>
<h2 class="head-title">{LEGAL_PAGES[page]}</h2></header>
<article class="content legal-body">{body}</article></main></div></body></html>""")


# --- 웹 화면 -----------------------------------------------------------------------

# 화면 파일이 바뀌면 브라우저가 바로 새 파일을 받게 한다 (예전 화면이 캐시에 남아 없는 주소를 부르지 않게)
ASSET_VERSION = str(int(max(f.stat().st_mtime for f in (HERE / "static").glob("*.*"))))


@app.get("/")
def index():
    html = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "report.css", "app.js"):
        html = html.replace(f"/static/{name}", f"/static/{name}?v={ASSET_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
