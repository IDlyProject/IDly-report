"""IMAP으로 붙일 수 있는 메일 서비스 목록.

auth
- password: IMAP 비밀번호 로그인. 대부분 2단계 인증 + 앱 비밀번호가 필요하다.
- oauth_microsoft: Microsoft가 비밀번호 IMAP을 막아 OAuth로만 된다. IDly는 OAuth를 쓰지 않으므로 연동 불가로 표시된다.

steps: 사용자가 연동 전에 해야 하는 일. url이 있으면 그 설정 페이지로 바로 보낸다.
password_label: 비밀번호 입력칸 이름 (서비스마다 앱 비밀번호 필요 여부가 다르다).
login_hint: 로그인 실패 시 보여줄 안내.
"""

from typing import Dict, List, Optional

PROVIDERS: List[dict] = [
    {
        "id": "gmail",
        "name": "Gmail",
        "domains": ["gmail.com", "googlemail.com"],
        "host": "imap.gmail.com",
        "auth": ["password"],
        "steps": [
            {"text": "Google 계정에서 2단계 인증 켜기", "url": "https://myaccount.google.com/security"},
            {"text": "앱 비밀번호 16자리 만들기", "url": "https://myaccount.google.com/apppasswords"},
        ],
        "password_label": "앱 비밀번호 (16자리)",
        "login_hint": "Google 계정 비밀번호가 아니라 앱 비밀번호를 입력했는지 확인해주세요.",
    },
    {
        "id": "naver",
        "name": "네이버 메일",
        "domains": ["naver.com"],
        "host": "imap.naver.com",
        "auth": ["password"],
        "steps": [
            {"text": "네이버 메일 왼쪽 아래 환경설정 → POP3/IMAP 설정 → IMAP/SMTP 설정 탭에서 '사용함' 선택 후 저장", "url": "https://mail.naver.com"},
            {"text": "내정보 → 보안설정에서 2단계 인증 켜기 (로그인 비밀번호로는 연결이 거절되는 경우가 많아요)", "url": "https://nid.naver.com/user2/help/myInfoV2?m=viewSecurity"},
            {"text": "2단계 인증 관리 → 애플리케이션 비밀번호 만들기 (종류: 기타) → 나온 비밀번호를 아래에 넣기", "url": "https://nid.naver.com/user2/help/myInfoV2?m=viewSecurity"},
        ],
        "password_label": "네이버 애플리케이션 비밀번호",
        "login_hint": "IMAP 사용 설정이 켜져 있는지, 2단계 인증을 쓴다면 애플리케이션 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "daum",
        "name": "다음 메일",
        "domains": ["daum.net", "hanmail.net"],
        "host": "imap.daum.net",
        "auth": ["password"],
        "steps": [
            {"text": "다음 메일 환경설정 > IMAP/POP3에서 IMAP 사용하기", "url": "https://mail.daum.net"},
            {"text": "카카오 계정 2단계 인증을 쓰고 있다면 앱 비밀번호 만들기", "url": "https://accounts.kakao.com"},
        ],
        "password_label": "비밀번호 (2단계 인증을 쓰면 앱 비밀번호)",
        "login_hint": "IMAP 사용 설정이 켜져 있는지, 2단계 인증을 쓴다면 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "kakao",
        "name": "카카오 메일",
        "domains": ["kakao.com"],
        "host": "imap.kakao.com",
        "auth": ["password"],
        "steps": [
            {"text": "카카오 메일 환경설정 > IMAP/POP3에서 IMAP 사용하기", "url": "https://mail.kakao.com"},
            {"text": "카카오 계정 보안 설정에서 앱 비밀번호 만들기", "url": "https://accounts.kakao.com"},
        ],
        "password_label": "앱 비밀번호",
        "login_hint": "IMAP 사용 설정이 켜져 있는지, 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "nate",
        "name": "네이트 메일",
        "domains": ["nate.com"],
        "host": "imap.nate.com",
        "auth": ["password"],
        "steps": [{"text": "네이트 메일 환경설정 > POP3/IMAP 설정에서 IMAP 사용하기", "url": "https://www.nate.com"}],
        "password_label": "네이트 비밀번호",
        "login_hint": "IMAP 사용 설정이 켜져 있는지 확인해주세요.",
    },
    {
        "id": "outlook",
        "name": "Outlook / Hotmail",
        "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.kr", "hotmail.co.kr"],
        "host": "outlook.office365.com",
        "auth": ["oauth_microsoft"],
        "steps": [],
        "password_label": None,
        "login_hint": "Microsoft 로그인을 다시 시도해주세요.",
    },
    {
        "id": "icloud",
        "name": "iCloud 메일",
        "domains": ["icloud.com", "me.com", "mac.com"],
        "host": "imap.mail.me.com",
        "auth": ["password"],
        "steps": [{"text": "Apple 계정 > 로그인 및 보안 > 앱 암호 만들기", "url": "https://account.apple.com"}],
        "password_label": "앱 암호",
        "login_hint": "Apple 계정 암호가 아니라 앱 암호를 넣었는지 확인해주세요.",
    },
    {
        "id": "yahoo",
        "name": "Yahoo 메일",
        "domains": ["yahoo.com", "ymail.com", "rocketmail.com"],
        "host": "imap.mail.yahoo.com",
        "auth": ["password"],
        "steps": [{"text": "계정 보안에서 앱 비밀번호 만들기", "url": "https://login.yahoo.com/account/security"}],
        "password_label": "앱 비밀번호",
        "login_hint": "계정 비밀번호가 아니라 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "aol",
        "name": "AOL 메일",
        "domains": ["aol.com"],
        "host": "imap.aol.com",
        "auth": ["password"],
        "steps": [{"text": "계정 보안에서 앱 비밀번호 만들기", "url": "https://login.aol.com/account/security"}],
        "password_label": "앱 비밀번호",
        "login_hint": "계정 비밀번호가 아니라 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "zoho",
        "name": "Zoho 메일",
        "domains": ["zoho.com", "zohomail.com"],
        "host": "imap.zoho.com",
        "auth": ["password"],
        "steps": [
            {"text": "Zoho 메일 설정 > 메일 계정에서 IMAP 접근 켜기", "url": "https://mail.zoho.com"},
            {"text": "2단계 인증을 쓰고 있다면 앱 비밀번호 만들기", "url": "https://accounts.zoho.com/home#security/app_password"},
        ],
        "password_label": "비밀번호 (2단계 인증을 쓰면 앱 비밀번호)",
        "login_hint": "IMAP 접근이 켜져 있는지, 2단계 인증을 쓴다면 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "gmx",
        "name": "GMX",
        "domains": ["gmx.com"],
        "host": "imap.gmx.com",
        "auth": ["password"],
        "steps": [{"text": "GMX 설정 > POP3 & IMAP에서 외부 접근 켜기", "url": "https://www.gmx.com"}],
        "password_label": "GMX 비밀번호",
        "login_hint": "POP3 & IMAP 외부 접근이 켜져 있는지 확인해주세요.",
    },
    {
        "id": "yandex",
        "name": "Yandex 메일",
        "domains": ["yandex.com", "yandex.ru"],
        "host": "imap.yandex.com",
        "auth": ["password"],
        "steps": [
            {"text": "메일 설정 > 메일 프로그램에서 IMAP 켜기", "url": "https://mail.yandex.com"},
            {"text": "앱 비밀번호 만들기", "url": "https://id.yandex.com/security/app-passwords"},
        ],
        "password_label": "앱 비밀번호",
        "login_hint": "IMAP이 켜져 있는지, 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "fastmail",
        "name": "Fastmail",
        "domains": ["fastmail.com", "fastmail.fm"],
        "host": "imap.fastmail.com",
        "auth": ["password"],
        "steps": [{"text": "IMAP 권한으로 앱 비밀번호 만들기", "url": "https://app.fastmail.com/settings/security/apps"}],
        "password_label": "앱 비밀번호",
        "login_hint": "IMAP 권한이 있는 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "google_workspace",
        "name": "Google Workspace (학교·회사 Gmail)",
        "domains": [],
        "host": "imap.gmail.com",
        "auth": ["password"],
        "steps": [
            {"text": "이 메일 주소로 Google 계정 보안 페이지에 들어가 2단계 인증 켜기", "url": "https://myaccount.google.com/security"},
            {"text": "앱 비밀번호 16자리 만들기 (메뉴가 없으면 학교·회사 관리자가 막아 둔 거예요)", "url": "https://myaccount.google.com/apppasswords"},
        ],
        "password_label": "앱 비밀번호 (16자리)",
        "login_hint": "학교·회사 계정 비밀번호가 아니라 앱 비밀번호를 넣었는지 확인해주세요.",
    },
    {
        "id": "microsoft_365",
        "name": "Microsoft 365 (학교·회사 Outlook)",
        "domains": [],
        "host": "outlook.office365.com",
        "auth": ["oauth_microsoft"],
        "steps": [],
        "password_label": "",
        "login_hint": "",
    },
    {
        "id": "naver_works",
        "name": "네이버웍스",
        "domains": [],
        "host": "imap.worksmobile.com",
        "auth": ["password"],
        "steps": [{"text": "관리자가 IMAP을 허용했는지 확인하고, 2단계 인증을 쓰면 앱 비밀번호 만들기"}],
        "password_label": "네이버웍스 비밀번호 (2단계 인증을 쓰면 앱 비밀번호)",
        "login_hint": "관리자가 IMAP을 허용했는지, 비밀번호가 맞는지 확인해주세요.",
    },
    {
        "id": "hiworks",
        "name": "하이웍스 (가비아)",
        "domains": [],
        "host": "imap.hiworks.com",
        "auth": ["password"],
        "steps": [{"text": "하이웍스 메일 환경설정에서 IMAP 사용을 켜기 (관리자가 막아 두었을 수 있어요)"}],
        "password_label": "하이웍스 비밀번호",
        "login_hint": "IMAP 사용이 켜져 있는지, 비밀번호가 맞는지 확인해주세요.",
    },
    {
        "id": "custom",
        "name": "이 메일",
        "domains": [],
        "host": None,
        "auth": ["password"],
        "steps": [
            {"text": "학교·회사 메일 도움말에서 'IMAP 서버' 주소를 찾아 아래에 넣기 (보통 imap.도메인 또는 mail.도메인, 포트 993)"},
            {"text": "메일 설정에서 IMAP(외부 메일 프로그램) 사용이 켜져 있는지 확인"},
        ],
        "password_label": "메일 비밀번호",
        "login_hint": "IMAP 서버 주소·포트와 비밀번호를 확인해주세요. 회사·학교 메일은 관리자가 IMAP을 막아 두었을 수 있어요.",
    },
]

_BY_ID: Dict[str, dict] = {p["id"]: p for p in PROVIDERS}


def get_provider(provider_id: str) -> Optional[dict]:
    return _BY_ID.get(provider_id)


def detect_provider(email: str) -> dict:
    domain = email.rsplit("@", 1)[-1].lower()
    for p in PROVIDERS:
        if domain in p["domains"]:
            return p
    return _BY_ID["custom"]


# --- 학교·회사 메일: 메일 서버(MX)로 어떤 서비스인지 알아낸다 -----------------------------

_MX_RULES = [
    ("google_workspace", ("google.com", "googlemail.com")),
    ("microsoft_365", ("outlook.com", "office365.com")),
    ("naver_works", ("worksmobile.com",)),
    ("naver", ("naver.com",)),
    ("daum", ("daum.net", "hanmail.net", "kakao.com")),
    ("hiworks", ("hiworks.com", "hiworks.co.kr")),
]


def detect_by_mx(domain: str) -> Optional[dict]:
    """도메인의 메일 서버(MX)를 보고 알려진 서비스를 고른다. 모르면 None."""
    try:
        import dns.resolver

        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        hosts = [str(r.exchange).rstrip(".").lower() for r in answers]
    except Exception:
        return None
    for provider_id, suffixes in _MX_RULES:
        if any(h == s or h.endswith("." + s) for h in hosts for s in suffixes):
            return _BY_ID[provider_id]
    # 스팸 필터 중계 서버(가비아 등)가 앞에 있으면 MX로는 모른다. 발신 허용 목록(SPF)에 Google·Microsoft가 있으면 그쪽으로 추정한다
    try:
        import dns.resolver

        spf = " ".join(b"".join(r.strings).decode(errors="ignore") for r in dns.resolver.resolve(domain, "TXT", lifetime=4))
    except Exception:
        return None
    if "_spf.google.com" in spf:
        return {**_BY_ID["google_workspace"], "guessed": True}
    if "spf.protection.outlook.com" in spf:
        return {**_BY_ID["microsoft_365"], "guessed": True}
    return None


def resolve_provider(email: str) -> dict:
    """주소 도메인 → 알려진 메일 서비스, 아니면 MX로, 그래도 모르면 직접 입력."""
    provider = detect_provider(email)
    if provider["id"] != "custom":
        return provider
    return detect_by_mx(email.rsplit("@", 1)[-1].lower()) or provider


# --- 로그인 실패 사유: 메일 서버가 돌려준 원문을 사용자가 할 일로 바꾼다 --------------------

_LOGIN_ERRORS = [
    # (원문에 있는 말, 안내)
    (("imap/smtp settings", "imap access is disabled", "imap is disabled", "imap 사용"),
     "메일 설정에서 IMAP 사용이 꺼져 있어요. 위 안내의 첫 단계대로 IMAP을 켠 뒤 다시 시도해주세요."),
    (("application-specific password required", "app password"),
     "이 계정은 앱 비밀번호가 필요해요. 로그인 비밀번호 대신 앱 비밀번호를 만들어 넣어주세요."),
    (("disabled for your domain", "administrator", "admin has", "not allowed for your organization"),
     "학교·회사 관리자가 외부 메일 연결(IMAP)을 막아 두었어요. 관리자에게 IMAP 허용을 요청하거나 개인 메일로 해주세요."),
    (("web login required", "log in via your web browser", "please log in via"),
     "메일 서비스가 보안 확인을 요구해요. 브라우저에서 한 번 로그인한 뒤 다시 시도해주세요."),
    (("too many", "rate limit", "try again later"),
     "로그인 시도가 너무 많았어요. 몇 분 뒤 다시 시도해주세요."),
]


def explain_login_error(raw: str, provider: dict) -> str:
    text = raw.lower()
    for needles, message in _LOGIN_ERRORS:
        if any(n in text for n in needles):
            return message
    if provider["id"] == "naver":
        return ("네이버가 로그인을 거절했어요. 네이버 로그인 비밀번호로는 거절되는 경우가 많아요. "
                "2단계 인증을 켜고 애플리케이션 비밀번호를 만들어 넣어 주세요. "
                "이미 그렇게 했다면 네이버 앱·메일에 '로그인 차단' 알림이 왔는지 확인해 주세요.")
    return f"로그인하지 못했어요. {provider['login_hint']}"
