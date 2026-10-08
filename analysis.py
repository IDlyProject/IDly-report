"""메일 헤더에서 온라인 계정을 찾는다.

1. 메일 헤더(보낸 곳·제목·날짜)만 규칙으로 훑어 계정 신호를 고른다. (AI 없음, 비용 없음)
   광고·답장·결제대행사 메일은 뺀다.
2. 보낸 곳을 서비스 도메인 단위로 묶고 가입일·최근 활동·구독 상태·휴면/삭제 안내·보안 알림을 정리한다.
3. 결제·구독 메일은 서비스마다 최근 몇 통의 본문을 이 서버 안에서만 읽는다. (외부 전송 없음)
   금액·다음 결제일을 뽑고, 1회성 결제(주문·예약)와 구독을 나누고, 결제 간격으로 주기(월·연·주)를 추정한다.
   구독인데 다음 결제일이 한참 지나도 결제 메일이 없으면 해지한 것으로 본다.
4. OPENAI_API_KEY가 있으면 후보 서비스만 마스킹한 요약을 보내 서비스 이름·분류·계정 여부를 다듬는다.
5. 프론트엔드 Account 구조와 "메일로 확인한 상태 / IDly가 제안할 행동"(insights)으로 돌려준다.
"""

import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import httpx


OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
AI_CHUNK = 30  # 한 번에 OpenAI로 보내는 서비스 수
AI_RETRIES = 6  # 한도 초과·일시 오류 때 다시 보내는 횟수
AI_PARALLEL = 4  # 동시에 보내는 AI 묶음 수 (한도에 걸리면 위 재시도가 기다린다)
BODY_PARALLEL = 4  # 동시에 읽는 메일 본문 수 (imap_sync.BODY_WORKERS와 맞춘다)
UNUSED_DAYS = 365
RECENT_DAYS = 180  # 인증 메일 본문에서 의심 로그인 안내를 찾아볼 기간
MAX_AUTH_BODIES = 10  # 서비스마다 본문을 읽어 볼 최근 인증·로그인 메일 수
USD_KRW = 1400  # 달러 결제를 원화로 대략 환산할 때

# --- 마스킹 -------------------------------------------------------------------

_EMAIL_LOCAL_RE = re.compile(r"\b[A-Z0-9._%+-]+@(?=[A-Z0-9.-]+\.[A-Z]{2,}\b)", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:01[016789][- ]?\d{3,4}[- ]?\d{4}|\+82[- ]?10[- ]?\d{3,4}[- ]?\d{4})(?!\d)")
_RESIDENT_RE = re.compile(r"(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{1,7}(?!\d)")
# 인증번호는 '인증·코드·code' 근처 숫자만 가린다 (날짜·주문번호는 남긴다)
_OTP_RE = re.compile(r"((?:인증|코드|code|otp|verification|pin)[^\d\n]{0,20})\d{4,8}(?!\d)", re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.I | re.S)
_SPACE_RE = re.compile(r"\s+")


def redact(value: str) -> str:
    """메일이 서버 밖(OpenAI)으로 나가기 전에 직접 식별자를 가린다."""
    value = _RESIDENT_RE.sub("[주민번호]", value)
    value = _CARD_RE.sub("[카드번호]", value)
    value = _PHONE_RE.sub("[전화번호]", value)
    value = _OTP_RE.sub(r"\1[인증번호]", value)
    # 주소의 아이디만 가리고 도메인은 남긴다 (서비스를 알아보는 단서)
    return _EMAIL_LOCAL_RE.sub("[이메일]@", value)


# --- 1단계: 헤더 규칙 ---------------------------------------------------------

def _re(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.I)


# 계정 신호로 보지 않는 제목: 답장·전달, 광고, 캘린더 초대, 비회원 주문
_SKIP_SUBJECT_RE = _re(
    r"^\s*(re|fw|fwd|답장|전달)\s*:|\(광고\)|\[광고\]|\d+\s?% off|할인 쿠폰|출시"
    r"|^\s*(초대|업데이트된 초대|invitation|updated invitation|accepted|수락됨|거절됨|취소된 일정|변경된 일정|canceled event|cancelled event|updated event)\s*:|약속 예약됨|비회원"
    # 캘린더 일정 알림 ("알림: 일정 이름 - 2025년 3월 11일 (화) 오전 11:30 (GMT+9)")
    r"|^\s*(알림|reminder|notification)\s*:.*(\(GMT[+-]\d|\d{4}년 \d{1,2}월 \d{1,2}일)"
)
# 결제를 권하는 광고 ("~결제해 보세요", "지금 구매하세요"). 결제 신호로 보지 않는다
_PROMO_RE = _re(
    r"할인|특가|쿠폰|혜택을 받|세일|행사|추천|구매하세요|결제해\s?보세요|해\s?보세요|만나보세요|\bsale\b|\bdeals?\b|% off|"
    r"\boffer\b|discount|promo|\bshop now|don'?t miss|last chance|free shipping"
)
# '휴면 해제'는 휴면이 풀렸다는 뜻 (다시 쓰기 시작한 계정)
_REACTIVATED_RE = _re(r"휴면\s?(해제|복구)|reactivat")

# 휴면·삭제·백업 안내 (계정이 곧 사라지거나 이미 방치된 상태)
# 백업은 계정·삭제 맥락이 있을 때만 (백업 방법을 알려주는 광고 제외)
_BACKUP_RE = _re(r"(back ?up|백업).{0,40}(account|계정|delet|삭제|days? left|기한)|(account|계정).{0,40}(back ?up|백업)|account data (attached|export)|days? left to back ?up")
_DORMANCY_RE = _re(r"미\s?로그인|휴면|장기\s?미(이용|사용|접속)|개인정보\s?파기|will be (permanently )?deleted|scheduled for deletion|"
                   r"(데이터|계정|정보|파일).{0,6}삭제\s?(예정|됩니다|될|안내)|inactive account|account.{0,30}(delet|clos)|inactive account|dormant|" + _BACKUP_RE.pattern)
# 인증·로그인 메일 본문에 이런 말이 있으면 본인이 아닌 로그인 시도 안내다
_SUSPICIOUS_BODY_RE = _re(r"의심스러운|suspicious|unusual (sign|activity|login)|누군가.{0,20}로그인|someone (tried|attempted)|본인이 로그인을 시도하셨나요")
# 회원에게만 오는 정기 고지 (계정이 있다는 신호일 뿐 보안 문제는 아님)
_MEMBER_NOTICE_RE = _re(r"개인정보\s?이용\s?내역|이용\s?내역\s?(통지|안내)|수신\s?동의.{0,10}(확인|안내)|약관.{0,6}(변경|개정)|개인정보\s?(처리)?\s?방침|"
                        r"(update[sd]?|changes?) to (our |the )?(terms|privacy policy|user agreement)|updated (our |the )?(terms|privacy policy)|"
                        r"terms of (service|use) (update|change)|privacy policy update")

# 구독 상태. 구독 맥락 단어가 있어야 만료·해지로 본다 (스탬프 만료, 스토리 만료 같은 오탐 방지)
_SUB_CONTEXT_RE = _re(r"구독|멤버십|membership|subscription|플랜|plan|요금제|이용권|pro\b|premium|프리미엄|정기\s?결제|자동\s?결제")
_SUB_STATES: List[Tuple[str, "re.Pattern[str]", bool]] = [
    # (상태, 패턴, 구독 맥락 단어 필요 여부)
    # 결제 실패: 결제가 된 메일이 아니다. 실패가 이어지면 구독이 끊긴다
    ("결제 실패", _re(r"(payment|charge|결제).{0,30}(unsuccessful|failed|declined|did not go through|couldn'?t be processed)|"
                   r"(unable|couldn'?t|could not) (to )?(process|charge|complete).{0,20}payment|결제.{0,8}(실패|거절|오류|되지 않|안 ?됐|불가)|"
                   r"update your payment (method|details|info)|결제\s?(방법|수단|정보).{0,8}(업데이트|변경|확인)하세요"), False),
    ("해지됨", _re(r"갱신되지\s?않|해지.{0,4}(완료|되었|신청|접수)|cancel+ation|cancel+ed|won'?t renew|구독이 취소|"
                r"(정기\s?결제|구독|자동\s?결제|멤버십|자동\s?갱신).{0,6}(취소|해지|중단)(되었|됐|됩|했|완료|처리)"), True),
    ("체험 종료", _re(r"trial.{0,10}(ended|has ended|expired|is over)|(무료\s?)?체험.{0,8}(종료|끝)|무료 플랜.{0,4}종료"), False),
    ("만료 예정", _re(r"만료(됩니다|예정|될|되기)|expir(es|ing)|ends on|곧 종료|갱신하세요|renew now"), True),
    ("활성", _re(r"정기\s?결제|자동\s?결제|구독.{0,10}(결제|갱신|완료|시작)|subscription.{0,25}(renew|confirm|receipt|payment|started|active)|renewal (receipt|confirmation)|멤버십.{0,10}(결제|갱신)|membership.{0,10}(renew|payment)|welcome to the .{0,20}plan|플랜.{0,6}(결제|시작)"), False),
]

_SECURITY_RE = _re(r"의심|suspicious|unusual|비정상|새로운\s?(기기|환경|위치)|new (device|sign-?in|login)|password (was )?(changed|reset)|비밀번호.{0,4}(변경|재설정)|유출|breach|security alert|보안\s?(경고|알림)")
# 보안 신호 중 사용자에게 '확인이 필요하다'고 알릴 것: 본인 확인을 묻거나 이상 징후를 알리는 안내만.
# "새 기기에서 로그인되었습니다" 같은 단순 통지는 신호로만 남기고 알리지 않는다
_SECURITY_ALERT_RE = _re(r"의심|suspicious|unusual|비정상|하셨나요|본인이 맞|was (this|it) you|password (was )?changed|비밀번호.{0,6}변경(되었|됐|됨)|유출|breach|(계정|account).{0,10}(정지|잠금|잠겼|suspend|lock)")

SIGNAL_PATTERNS = [
    ("가입", _re(r"가입|환영|welcome|sign[ -]?up|registration|계정이 (생성|만들어)|account (created|is ready)|getting started|"
                r"you'?re (on|in)\b|등록이? 완료|finish setting up|set ?up your (account|profile)|activate your account")),
    ("보안", _SECURITY_RE),
    ("결제", _re(r"결제|영수증|\breceipts?\b|\binvoices?\b|\bpayments?\b|청구|구매|주문|\borders?\b|예약|\bbookings?\b|\bbilling\b|\bcharged\b")),
    ("로그인", _re(r"로그인|log[ -]?in|sign(ed)?[ -]?in|접속")),
    ("인증", _re(r"인증\s?(코드|번호)|인증 안내|이메일\s?인증|verif|confirm your|verification code|security code|login code|activation (code|link)|일회용 코드|one-?time|\botp\b|코드는|\bcode is\b")),
]

# 결제대행사: 결제 알림은 오지만 그 자체가 사용자의 서비스 계정은 아니다
PAYMENT_GATEWAYS = {
    "inicis.com", "kcp.co.kr", "tosspayments.com", "nicepay.co.kr", "danal.co.kr", "kakaopay.com",
    "cafe24corp.com", "payco.com", "settlebank.co.kr", "kicc.co.kr", "ksnet.co.kr", "paypal.com", "stripe.com",
    "kakaopaycorp.net", "kakaopay.co.kr", "naverpay.com", "smilepay.co.kr", "ssgpay.com",
}

# 사람끼리 주고받는 메일 도메인. 여기서 온 메일은 서비스 발신(noreply 등)일 때만 본다
FREEMAIL = {
    "gmail.com", "googlemail.com", "naver.com", "daum.net", "hanmail.net", "nate.com",
    "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "me.com",
}
_SERVICE_SENDER_RE = _re(r"no-?reply|do-?not-?reply|notice|notification|account|security|info|help|support|mailer|webmaster|member|admin|service")

# 두 단계 최상위 도메인 (mail.coupang.co.kr → coupang.co.kr)
_MULTI_TLD = {
    "co.kr", "or.kr", "ne.kr", "go.kr", "ac.kr", "re.kr", "pe.kr",
    "co.jp", "ne.jp", "or.jp", "co.uk", "org.uk", "ac.uk",
    "com.au", "net.au", "com.cn", "com.tw", "com.sg", "com.br", "co.in",
}


# 한 서비스가 여러 도메인으로 메일을 보내는 경우
_DOMAIN_ALIASES = {
    "amazonaws.com": "aws.amazon.com", "aws.com": "aws.amazon.com", "twitter.com": "x.com",
    "claude.com": "anthropic.com", "clip-studio.com": "clipstudio.net", "smartthings.com": "samsung.com",
    "samsungcard.com": "samsungcard.com", "googlemail.com": "google.com", "accounts.google.com": "google.com",
    "adobesystems.com": "adobe.com", "artspark.co.jp": "clipstudio.net", "celsys.com": "clipstudio.net", "clip-studio.net": "clipstudio.net", "iloen.com": "melon.com",
    "nianticlabs.com": "pokemongolive.com", "zerolongevity.com": "zerofasting.com", "samsungcloud.com": "samsung.com",
    "neosapience.com": "typecast.ai", "pencil.dev": "pen.dev", "inv.tech": "meridial.ai", "linkedin-ei.com": "linkedin.com", "facebookmail.com": "facebook.com",
}

# 자주 나오는 서비스의 이름·분류. AI 키가 없어도 이름과 분류가 제대로 나오게 한다
KNOWN_SERVICES: Dict[str, Tuple[str, str]] = {
    "coupang.com": ("쿠팡", "쇼핑"), "netflix.com": ("넷플릭스", "OTT"), "youtube.com": ("YouTube", "OTT"),
    "laftel.net": ("라프텔", "OTT"), "watcha.com": ("왓챠", "OTT"), "tving.com": ("티빙", "OTT"),
    "melon.com": ("멜론", "음악"), "spotify.com": ("Spotify", "음악"), "distrokid.com": ("DistroKid", "창작"),
    "bandlab.com": ("BandLab", "창작"), "beatstars.com": ("BeatStars", "창작"),
    "x.com": ("X", "SNS"), "facebook.com": ("Facebook", "SNS"), "instagram.com": ("Instagram", "SNS"),
    "pinterest.com": ("Pinterest", "SNS"), "snapchat.com": ("Snapchat", "SNS"), "band.us": ("밴드", "SNS"),
    "kakao.com": ("카카오", "포털"), "kakaocorp.com": ("카카오", "포털"), "cyworld.com": ("싸이월드", "SNS"),
    "google.com": ("Google", "생산성"), "microsoft.com": ("Microsoft", "생산성"), "apple.com": ("Apple", "생산성"),
    "notion.so": ("Notion", "생산성"), "zoom.us": ("Zoom", "생산성"), "hancom.com": ("한컴", "생산성"),
    "simplenote.com": ("Simplenote", "생산성"), "readdle.com": ("Spark", "생산성"), "streak.com": ("Streak", "생산성"),
    "any.do": ("Any.do", "생산성"), "dropbox.com": ("Dropbox", "클라우드"), "mediafire.com": ("MediaFire", "클라우드"),
    "sendanywhere.com": ("Send Anywhere", "클라우드"), "lastpass.com": ("LastPass", "생산성"),
    "openai.com": ("OpenAI", "생산성"), "anthropic.com": ("Anthropic(Claude)", "생산성"), "manus.im": ("Manus", "생산성"),
    "aws.amazon.com": ("AWS", "개발"), "snyk.io": ("Snyk", "개발"), "n8n.io": ("n8n", "개발"),
    "huggingface.co": ("Hugging Face", "개발"), "expo.dev": ("Expo", "개발"), "upstage.ai": ("Upstage", "개발"),
    "portswigger.net": ("PortSwigger", "개발"), "hackthebox.com": ("Hack The Box", "개발"), "yorba.co": ("Yorba", "생산성"),
    "wishket.com": ("위시켓", "커리어"), "saramin.co.kr": ("사람인", "커리어"), "linkedin.com": ("LinkedIn", "커리어"),
    "data-bank.ai": ("TestGlider", "교육"), "ets.org": ("TOEFL(ETS)", "교육"), "ebs.co.kr": ("EBS", "교육"),
    "nurimedia.co.kr": ("DBpia", "교육"), "dacon.io": ("데이콘", "교육"), "coursera.org": ("Coursera", "교육"),
    "inflearn.com": ("인프런", "교육"), "etoos.com": ("이투스", "교육"), "megastudy.net": ("메가스터디", "교육"),
    "multicampus.co.kr": ("멀티캠퍼스", "교육"), "riss.kr": ("RISS", "교육"), "edunet.net": ("에듀넷", "교육"),
    "by-works.com": ("AI 허브", "개발"), "ybmnet.co.kr": ("YBM NET", "교육"), "move.is": ("오르비", "교육"),
    "adobe.com": ("Adobe", "디자인"), "behance.net": ("Behance", "디자인"), "behance.com": ("Behance", "디자인"),
    "canva.com": ("Canva", "디자인"), "miricanvas.com": ("미리캔버스", "디자인"), "sandoll.co.kr": ("산돌구름", "디자인"),
    "clipstudio.net": ("CLIP STUDIO", "창작"), "pixiv.net": ("pixiv", "창작"), "postype.com": ("포스타입", "창작"),
    "capcut.com": ("CapCut", "창작"), "rawpixel.com": ("rawpixel", "디자인"), "pngtree.com": ("Pngtree", "디자인"),
    "tinkercad.com": ("Tinkercad", "디자인"), "prezi.com": ("Prezi", "생산성"), "typecast.ai": ("타입캐스트", "창작"),
    "nate.com": ("네이트", "포털"), "grammarly.com": ("Grammarly", "생산성"), "evernote.com": ("Evernote", "생산성"),
    "soundcloud.com": ("SoundCloud", "음악"), "imgur.com": ("Imgur", "SNS"), "pokemongolive.com": ("Pokémon GO", "게임"),
    "zerofasting.com": ("Zero", "기타"), "plusdocs.com": ("Plus AI", "생산성"), "lalal.ai": ("LALAL.AI", "창작"),
    "nexon.com": ("넥슨", "게임"), "riotgames.com": ("라이엇게임즈", "게임"), "nintendo.com": ("Nintendo", "게임"),
    "twitch.tv": ("Twitch", "게임"), "ea.com": ("EA", "게임"), "netmarble.com": ("넷마블", "게임"),
    "smilegate.com": ("STOVE", "게임"), "onstove.com": ("STOVE", "게임"), "pokemon.com": ("Pokémon GO", "게임"),
    "nianticlabs.com": ("Pokémon GO", "게임"), "daum.net": ("다음", "SNS"),
    "interpark.com": ("인터파크", "여행"), "hotels.com": ("Hotels.com", "여행"), "marriott.com": ("Marriott", "여행"),
    "koreanair.com": ("대한항공", "여행"), "viarail.ca": ("VIA Rail", "여행"), "megabox.co.kr": ("메가박스", "기타"),
    "sejongpac.or.kr": ("세종문화회관", "기타"), "bucketplace.net": ("오늘의집", "쇼핑"), "aladin.co.kr": ("알라딘", "쇼핑"),
    "kaerumall.com": ("카에루몰", "쇼핑"), "ohprint.me": ("오프린트미", "쇼핑"), "redprinting.co.kr": ("레드프린팅", "쇼핑"),
    "snaps.com": ("스냅스", "쇼핑"), "bizhows.com": ("비즈하우스", "쇼핑"), "marpple.com": ("마플", "쇼핑"),
    "wish.com": ("Wish", "쇼핑"), "ebay.com": ("eBay", "쇼핑"), "nike.com": ("Nike", "쇼핑"), "mwave.co.kr": ("Mwave", "쇼핑"),
    "kakaostyle.com": ("지그재그", "쇼핑"), "hellomarket.com": ("헬로마켓", "쇼핑"), "musinsa.com": ("무신사", "쇼핑"),
    "tossbank.com": ("토스뱅크", "금융"), "toss.im": ("토스", "금융"), "samsung.com": ("삼성 계정", "기타"),
    "navercorp.com": ("네이버", "포털"), "naver.com": ("네이버", "포털"), "betterme.world": ("BetterMe", "기타"),
    "runtastic.com": ("adidas Runtastic", "기타"), "polar.com": ("Polar", "기타"), "opensurvey.io": ("오픈서베이", "기타"),
}


def base_domain(domain: str) -> str:
    parts = domain.lower().strip(".").split(".")
    if parts[-1] == "aws":  # signin.aws, signup.aws
        return "aws.amazon.com"
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        base = ".".join(parts[-3:])
    else:
        base = ".".join(parts[-2:])
    # 발송 전용 도메인을 본 도메인으로 (facebookmail.com, samsung-mail.com, email-marriott.com)
    name, _, suffix = base.partition(".")
    for pattern in (r"^(.+?)-?mail$", r"^e?mail-(.+)$"):
        m = re.match(pattern, name)
        if m and m.group(1) not in ("g", "hot", "e", ""):
            base = f"{m.group(1)}.{suffix}"
            break
    return _DOMAIN_ALIASES.get(base, base)


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _date(value: Optional[str]) -> Optional[date]:
    """메일 날짜를 서버 시간대 기준 날짜로 (UTC로 온 메일이 하루 앞당겨 보이지 않게)."""
    try:
        sent = parsedate_to_datetime(value or "")
        return (sent.astimezone() if sent.tzinfo else sent).date()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def subscription_state(subject: str) -> Optional[str]:
    for state, pattern, needs_context in _SUB_STATES:
        if pattern.search(subject) and (not needs_context or _SUB_CONTEXT_RE.search(subject)):
            return state
    return None


def classify(subject: str) -> Optional[str]:
    """제목 하나를 신호 종류로 분류한다. 순서가 중요하다 (예: '미로그인'은 로그인이 아니다)."""
    if _SKIP_SUBJECT_RE.search(subject):
        return None
    if _REACTIVATED_RE.search(subject):
        return "로그인"
    if _DORMANCY_RE.search(subject) or _MEMBER_NOTICE_RE.search(subject):
        return "안내"
    # "Plus 구독을 시작해 보세요" 같은 권유 광고는 구독 신호가 아니다
    if subscription_state(subject) and not _PROMO_RE.search(subject):
        return "구독"
    for kind, pattern in SIGNAL_PATTERNS:
        if pattern.search(subject):
            if kind == "결제" and _PROMO_RE.search(subject):
                return None
            return kind
    return None


def _text_body(message: Message) -> str:
    parts: List[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            content = _HTML_RE.sub(" ", _STYLE_RE.sub(" ", content))
        # &#8202; 같은 문자 코드를 풀어야 그 안의 숫자가 금액·날짜로 잘못 읽히지 않는다
        parts.append(html.unescape(content))
    return _SPACE_RE.sub(" ", "\n".join(parts)).strip()


# --- 3단계: 구독 메일 본문에서 금액·다음 결제일 (서버 안에서만) --------------------

_AMOUNT_NEAR_RE = _re(r"(결제\s?금액|결제액|청구\s?금액|금액|합계|총액|total|amount|charged)[^\d₩$]{0,20}(₩|\$|US\$|KRW|USD)?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?\s?(원)?")
_KRW_RE = _re(r"(?:₩|KRW)\s?(\d{1,3}(?:,\d{3})+|\d{3,})|(\d{1,3}(?:,\d{3})+)\s?원")
_USD_RE = _re(r"(?:US\$|\$|USD)\s?(\d+(?:\.\d{2})?)")
_NEXT_KO_RE = _re(r"(다음\s?(결제|청구|갱신)\s?(일|예정일|예정)?|결제\s?예정일)[^\d]{0,15}(?:(\d{4})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_NEXT_EN_RE = _re(r"(next (billing|payment|charge)( date)?|renews? on|will renew on)\W{0,5}([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})")


def parse_amount(text: str) -> Optional[int]:
    """본문에서 결제 금액(원)을 찾는다. 금액 근처 단어가 있는 숫자를 먼저 본다."""
    m = _AMOUNT_NEAR_RE.search(text)
    if m:
        value = float(m.group(3).replace(",", "") + (m.group(4) or ""))
        is_usd = (m.group(2) or "").upper() in {"$", "US$", "USD"}
        if value > 0:
            return int(round(value * USD_KRW)) if is_usd else int(value)
    m = _KRW_RE.search(text)
    if m:
        return int((m.group(1) or m.group(2)).replace(",", ""))
    m = _USD_RE.search(text)
    if m:
        return int(round(float(m.group(1)) * USD_KRW))
    return None


def parse_next_billing(text: str, sent: Optional[date]) -> Optional[str]:
    m = _NEXT_KO_RE.search(text)
    if m:
        year = int(m.group(4)) if m.group(4) else (sent.year if sent else datetime.now().year)
        try:
            found = date(year, int(m.group(5)), int(m.group(6)))
            if sent and not m.group(4) and found < sent:
                found = date(year + 1, found.month, found.day)
            return found.isoformat()
        except ValueError:
            return None
    m = _NEXT_EN_RE.search(text)
    if m and m.group(4).lower()[:3] in _MONTHS:
        try:
            return date(int(m.group(6)), _MONTHS[m.group(4).lower()[:3]], int(m.group(5))).isoformat()
        except ValueError:
            return None
    return None


# --- 결제 이력: 1회성 결제와 구독 구분, 결제 주기, 결제가 끊겼는지 ------------------

MAX_PAYMENT_BODIES = 4     # 서비스마다 본문을 읽을 최근 결제 메일 수
MAX_TOTAL_BODIES = 400     # 한 번 탐색에서 읽는 결제 메일 본문 상한 (IMAP 부담)

_RECURRING_RE = _re(
    r"정기\s?결제|자동\s?(결제|갱신|연장)|구독|멤버십|membership|subscription|매월|매달|월간|월\s?(이용료|요금|회비)|"
    r"다음\s?(결제|청구|갱신)|갱신\s?(예정|일)|renew|recurring|billing (period|cycle)|per month|/\s?mo(nth)?\b|monthly|"
    r"annual(ly)?|yearly|per year|연간|정기\s?(구독|배송)|이용권.{0,6}(자동|정기)"
)
_ONE_TIME_RE = _re(
    r"주문|배송|구매\s?(완료|확정|내역)|예약|티켓|order (confirm|number|#|receipt)|your order|shipped|shipping|"
    r"purchase (confirm|receipt)|1회|단건|일시불|booking|reservation|결제\s?취소|환불"
)
_YEARLY_RE = _re(r"연간|1년\s?(이용|구독|플랜)|annual|yearly|per year|/\s?y(ea)?r\b")
_WEEKLY_RE = _re(r"주간\s?(구독|결제|이용)|weekly|per week|/\s?w(ee)?k\b")
_MONTHLY_RE = _re(r"매월|매달|월간|월\s?(이용료|요금|회비|구독)|monthly|per month|/\s?mo(nth)?\b|1개월")

CYCLE_DAYS = {"주간": 7, "월간": 30, "연간": 365}
# 결제 예정일이 이만큼 지나도 결제 메일이 없으면 해지한 것으로 본다
CYCLE_GRACE = {"주간": 5, "월간": 12, "연간": 30}
# 결제 간격이 이 범위면 그 주기로 본다
_CYCLE_GAPS = (("주간", 6, 8), ("월간", 26, 35), ("연간", 350, 380))


class Payment(NamedTuple):
    date: date
    subject: str
    amount: Optional[int]
    recurring: bool  # 정기결제·구독·갱신 같은 말이 있다
    one_time: bool   # 주문·배송·예약 같은 1회성 결제이고 구독 말은 없다
    cycle_hint: Optional[str]
    next_billing: Optional[str]
    trial: bool = False               # 무료 체험·혜택 기간이라 지금은 0원 (본문 금액은 체험 뒤 가격)
    trial_end: Optional[str] = None   # 유료로 바뀌는 날


# "(monthly)", "monthly subscription", "월간 구독"처럼 내 결제 주기를 직접 말하는 표현
_CYCLE_EXPLICIT = [
    ("월간", _re(r"\((monthly|month|월간|매월|1개월)\)|\bmonthly (plan|subscription|membership|billing)|billed monthly|"
                 r"per month\b|/\s?mo(nth)?\b|월간\s?(구독|플랜|요금제|이용권|결제)|매월\s?(결제|자동)|1개월\s?(이용권|구독)")),
    ("연간", _re(r"\((annual|annually|yearly|연간|1년)\)|\b(annual|yearly) (plan|subscription|membership|billing)\b(?! is set to renew)|"
                 r"billed (annually|yearly)|per year\b|/\s?y(ea)?r\b|연간\s?(구독|플랜|요금제|이용권|결제)|1년\s?(이용권|구독|플랜)")),
    ("주간", _re(r"\((weekly|주간)\)|\bweekly (plan|subscription)|billed weekly|per week\b|주간\s?(구독|결제|이용권)")),
]
# 결제 주기와 무관한 문장: 조건("If you have an annual plan…"), 전환 권유("연간으로 바꾸면 20% 할인")
_CYCLE_NOISE_RE = _re(
    r"[^.!?\n]*\bif you (have|are on|choose|switch to|upgrade to)\b[^.!?\n]*[.!?]?|"
    r"[^.!?\n]*\b(switch|upgrade|change) to (an? )?(annual|yearly|monthly)\b[^.!?\n]*[.!?]?|"
    r"[^.!?\n]*\bsave\b[^.!?\n]{0,40}\b(annual|yearly)\b[^.!?\n]*[.!?]?|"
    r"[^.!?\n]*(연간|월간)[^.!?\n]{0,10}(으로|로)\s?(바꾸|변경|전환)[^.!?\n]*[.!?]?"
)


def cycle_hint(subject: str, body: str) -> Optional[str]:
    """결제 메일에서 결제 주기를 읽는다. 제목 → 본문의 명시적 표현 → 본문 단어 수 순서로 본다."""
    for text in (subject, _CYCLE_NOISE_RE.sub(" ", body)):
        found = {name for name, pattern in _CYCLE_EXPLICIT if pattern.search(text)}
        if len(found) == 1:
            return found.pop()
    clean = f"{subject} {_CYCLE_NOISE_RE.sub(' ', body)}"
    counts = {"연간": len(_YEARLY_RE.findall(clean)), "주간": len(_WEEKLY_RE.findall(clean)), "월간": len(_MONTHLY_RE.findall(clean))}
    best = max(counts, key=counts.get)
    # 두 주기가 똑같이 나오면 정하지 않는다 (cycleKnown=False → 결제 간격으로 판단)
    if counts[best] == 0 or sorted(counts.values())[-2] == counts[best]:
        return None
    return best


# 무료 체험·학생 혜택처럼 지금은 0원이고 나중에 유료로 바뀌는 결제 ("12개월 무료", "2027년 10월 3일까지 무료")
_TRIAL_RE = _re(r"무료\s?체험|체험\s?(기간|판)|free trial|trial period|your trial|프로모션\s?(기간|혜택|가격|적용)|promotional (period|offer|price)|promo (period|price|offer)|"
                r"무료\s?(이용\s?)?기간|혜택\s?기간|학생\s?(혜택|할인|인증|요금|플랜)|student (offer|discount|plan|pricing)|intro(ductory)? (offer|price)|"
                r"\d+\s?(개월|months?|년|years?)\s?(간\s?|동안\s?)?(무료|free)|free for \d+|까지\s?무료|free until")
# "12개월 무료", "1년간 무료", "free for 12 months" → 체험 기간 (전환일이 메일에 없을 때 보낸 날에 더한다)
_TRIAL_SPAN_RE = _re(r"(\d+)\s?(개월|months?|년|years?)\s?(간\s?|동안\s?)?(무료|free)|free for (\d+)\s?(months?|years?)")
_TRIAL_OVER_RE = _re(r"체험.{0,8}(종료|끝났)|trial.{0,10}(ended|has ended|expired|is over)")
# 이번에 낸 돈이 0원 ("결제 금액 ₩0", "Total: $0.00", "₩0/월")
_ZERO_PAID_RE = _re(r"(결제\s?금액|결제액|청구\s?금액|합계|총액|오늘\s?(결제|청구)|total|amount|charged|due today)[^\d₩$]{0,20}(₩|\$|US\$|KRW|USD)?\s?0(\.00)?(?![\d,]|\.\d)|"
                    r"(₩|KRW|US\$|\$)\s?0(\.00)?(?![\d,]|\.\d)")
_DATE_KO_RE = _re(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_DATE_EN_RE = _re(r"\b([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})")


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return d


def parse_trial_end(text: str, sent: Optional[date]) -> Optional[str]:
    """체험·혜택 말 가까이에 있는, 보낸 날 이후의 첫 날짜를 유료 전환일로 본다.
    날짜가 없고 "12개월 무료"처럼 기간만 있으면 보낸 날 + 기간."""
    found = []
    for pattern, ymd in ((_DATE_KO_RE, lambda m: (m.group(1), m.group(2), m.group(3))),
                         (_DATE_EN_RE, lambda m: (m.group(3), _MONTHS.get(m.group(1).lower()[:3]), m.group(2)))):
        for m in pattern.finditer(text):
            y, mo, d = ymd(m)
            if not mo or not _TRIAL_RE.search(text[max(0, m.start() - 80):m.end() + 30]):
                continue
            try:
                when = date(int(y), int(mo), int(d))
            except ValueError:
                continue
            if sent is None or when > sent:
                found.append(when)
    if found:
        return min(found).isoformat()
    m = _TRIAL_SPAN_RE.search(text)
    if m and sent:
        n = int(m.group(1) or m.group(5))
        unit = (m.group(2) or m.group(6)).lower()
        return _add_months(sent, n * 12 if unit.startswith(("년", "y")) else n).isoformat()
    return None


def read_payment(event: "Event", body: str) -> Payment:
    """결제 메일 하나(제목 + 서버 안에서 읽은 본문)를 해석한다. 본문을 못 읽었으면 제목만 본다."""
    text = f"{event.subject} {body[:4000]}"
    recurring = bool(_RECURRING_RE.search(text))
    one_time = not recurring and bool(_ONE_TIME_RE.search(text))
    hint = cycle_hint(event.subject, body[:4000])
    # 체험이 끝났다는 안내는 체험 중이 아니다. 체험 말만 있고 0원도 전환일도 없으면 (체험 뒤 첫 결제 등) 유료로 본다
    trial_end = parse_trial_end(text, event.date) if _TRIAL_RE.search(text) and not _TRIAL_OVER_RE.search(text) else None
    trial = bool(trial_end or (_ZERO_PAID_RE.search(text) and _TRIAL_RE.search(text)))
    return Payment(event.date, event.subject, parse_amount(body) if body else None, recurring, one_time, hint,
                   parse_next_billing(body, event.date) if body else None, trial, trial_end)


def cycle_from_dates(dates: List[date]) -> Optional[str]:
    """결제 날짜 간격으로 주기를 추정한다. 간격의 절반 이상이 한 주기 범위에 들어야 한다."""
    days = sorted(set(dates))
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    if not gaps:
        return None
    for name, low, high in _CYCLE_GAPS:
        if sum(low <= g <= high for g in gaps) * 2 >= len(gaps):
            return name
    return None


def _similar(a: Optional[int], b: Optional[int]) -> bool:
    return a is None or b is None or abs(a - b) <= max(a, b) * 0.15


def _monthly(amount: Optional[int], cycle: str) -> Optional[int]:
    if amount is None:
        return None
    return {"월간": amount, "연간": round(amount / 12), "주간": round(amount * 52 / 12)}[cycle]


# 메일 위치 (폴더, UID). 필요한 메일 본문을 나중에 받을 때 쓴다
MailRef = Tuple[str, str]


class HeaderRecord(NamedTuple):
    ref: MailRef
    sender_name: str
    sender_address: str
    subject: str
    date: Optional[date]


def header_record(ref: MailRef, headers: Message) -> HeaderRecord:
    name, address = parseaddr(_decode(headers.get("From")))
    return HeaderRecord(ref, name.strip(), address.lower(), _decode(headers.get("Subject")), _date(headers.get("Date")))


# --- 2단계: 서비스 단위로 묶기 --------------------------------------------------

class Event(NamedTuple):
    date: Optional[date]
    subject: str
    ref: MailRef


class ServiceGroup:
    def __init__(self, domain: str):
        self.domain = domain
        self.sender_names: Counter = Counter()
        self.first_seen: Optional[date] = None
        self.last_seen: Optional[date] = None
        self.signals: List[Dict[str, str]] = []
        self.subscription: List[Tuple[str, Event]] = []  # (상태, 메일)
        self.dormancy: List[Event] = []
        self.security: List[Event] = []
        self.auth: List[Event] = []  # 인증·로그인 메일 (본문에 의심 로그인 안내가 있는지 본다)
        self.suspicious_login = False
        self.last_payment: Optional[Event] = None
        self.payments: List[Event] = []  # 결제·구독 결제 메일 (해지·만료 안내는 뺀다)
        self.messages = 0
        self.recent: List[Tuple[date, str, MailRef]] = []  # 모든 메일 (날짜, 제목, 위치). 신호 없는 곳의 AI 판단용
        self.latest_body = ""  # 약한 후보: 최근 메일 본문 앞·끝 (발신 이유가 끝에 적혀 있는 경우가 많다)
        self.service_sender = False  # noreply·info 같은 서비스 발신 주소에서 온 메일이 있다
        self.google_activity: List[Tuple[str, str, date]] = []

    def add(self, ref: MailRef, sender_name: str, subject: str, when: Optional[date]) -> None:
        if sender_name:
            self.sender_names[sender_name] += 1
        if when:
            self.first_seen = min(self.first_seen or when, when)
            self.last_seen = max(self.last_seen or when, when)
        self.messages += 1
        self.recent.append((when or date.min, subject[:120], ref))
        kind = classify(subject)
        if kind is None:
            return
        self.signals.append({"date": when.isoformat() if when else "", "type": kind, "subject": subject[:200]})
        event = Event(when, subject, ref)
        if kind == "구독":
            self.subscription.append((subscription_state(subject) or "활성", event))
        if kind == "안내" and _DORMANCY_RE.search(subject):
            self.dormancy.append(event)
        if kind == "보안" and not re.search(r"정기", subject):
            self.security.append(event)
        if kind in ("인증", "로그인"):
            self.auth.append(event)
        if kind in ("결제", "구독") and when and (self.last_payment is None or when >= (self.last_payment.date or date.min)):
            self.last_payment = event
        if when and (kind == "결제" or (kind == "구독" and (subscription_state(subject) or "활성") == "활성")):
            self.payments.append(event)

    def latest(self, *kinds: str) -> Optional[str]:
        dates = [s["date"] for s in self.signals if s["type"] in kinds and s["date"]]
        return max(dates) if dates else None

    def earliest(self, *kinds: str) -> Optional[str]:
        dates = [s["date"] for s in self.signals if s["type"] in kinds and s["date"]]
        return min(dates) if dates else None

    def latest_subscription(self) -> Optional[Tuple[str, Event]]:
        dated = [s for s in self.subscription if s[1].date]
        return max(dated, key=lambda s: s[1].date) if dated else None

    def merge(self, other: "ServiceGroup") -> None:
        """같은 서비스로 보이는 다른 도메인의 신호를 합친다 (BetterMe 여러 도메인 등)."""
        self.sender_names.update(other.sender_names)
        for d in (other.first_seen, other.last_seen):
            if d:
                self.first_seen = min(self.first_seen or d, d)
                self.last_seen = max(self.last_seen or d, d)
        self.signals += other.signals
        self.subscription += other.subscription
        self.dormancy += other.dormancy
        self.security += other.security
        self.auth += other.auth
        self.payments += other.payments
        self.messages += other.messages
        self.recent += other.recent
        self.service_sender = self.service_sender or other.service_sender
        if other.last_payment and (self.last_payment is None or (other.last_payment.date or date.min) > (self.last_payment.date or date.min)):
            self.last_payment = other.last_payment


# --- 서비스 이름 ------------------------------------------------------------------

# 발신자 이름 꼬리말 (Team, 팀, Inc. 등)
_NAME_SUFFIX_RE = _re(
    r"(\s*[,|·\-–]\s*)?\b(team|staff|notifications?|support|official|systems|corporation|corp\.?|inc\.?|"
    r"international|co\.,?\s*ltd\.?|ltd\.?|llc|korea|한국)\s*$|\s*(계정\s?팀|팀|소식|알림|고객센터|주식회사|㈜|\(주\))\s*$"
)
_NAME_PREFIX_RE = _re(r"^\s*(the|official|\(주\)|㈜|주식회사)\s+")
_PERSON_KO_RE = re.compile(r"^[가-힣]{2,3}$")
_PERSON_EN_RE = re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+$|^[a-z]+( [a-z]+)?$")


def _domain_label(domain: str) -> str:
    return domain.split(".")[0].replace("-", " ").title()


def clean_sender_name(name: str, domain: str) -> Optional[str]:
    """발신자 이름을 서비스 이름으로 다듬는다. 사람 이름·메일 주소처럼 보이면 None."""
    name = name.strip().strip('"')
    if not name or "@" in name:
        return None
    # "Luke at QR Code Generator", "David from Drawboard", "seeun lee via TestFlight" → 뒤쪽이 서비스
    m = re.search(r"\b(?:at|from|via)\s+(.+)$", name, re.I)
    if m:
        name = m.group(1)
    name = name.split(" | ")[0]  # "사람인 | 신입공채" → "사람인"
    name = re.sub(r"\[([^\]]+)\]\s*\(.*\)", r"\1", name)  # "[MARPPLE](no-reply)" → "MARPPLE"
    name = re.sub(r"\s*\((?:no-?reply|noreply)\)", "", name, flags=re.I)
    for _ in range(2):
        name = _NAME_SUFFIX_RE.sub("", name).strip()
    name = _NAME_PREFIX_RE.sub("", name).strip()
    if not name:
        return None
    label = domain.split(".")[0].lower()
    looks_person = _PERSON_KO_RE.match(name) or _PERSON_EN_RE.match(name)
    # 사람 이름처럼 보여도 도메인과 겹치면 서비스 이름이다 (멜론·넥슨 같은 짧은 이름)
    if looks_person and label not in name.lower().replace(" ", "") and name.lower().replace(" ", "") not in label:
        return None
    return name


def service_name(group: ServiceGroup) -> str:
    if group.domain in KNOWN_SERVICES:
        return KNOWN_SERVICES[group.domain][0]
    for name, _ in group.sender_names.most_common():
        cleaned = clean_sender_name(name, group.domain)
        if cleaned:
            return cleaned
    return _domain_label(group.domain)


def _name_key(name: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", name.lower())


def merge_same_services(groups: Dict[str, ServiceGroup]) -> Dict[str, ServiceGroup]:
    """이름이 같은 서비스를 하나로 (BetterMe 여러 도메인 등). 같은 회사의 다른 이름은 _DOMAIN_ALIASES로 합친다."""
    merged: Dict[str, ServiceGroup] = {}
    keys: Dict[str, str] = {}  # 이름 키 -> 대표 도메인
    # 신호가 많은 도메인을 대표로
    for domain, group in sorted(groups.items(), key=lambda kv: -len(kv[1].signals)):
        key = _name_key(service_name(group))
        target = keys.get(key)
        if target:
            merged[target].merge(group)
        else:
            merged[domain] = group
            keys[key] = domain
    return merged


def collect_groups(records: List[HeaderRecord], owner_email: str) -> Dict[str, ServiceGroup]:
    groups: Dict[str, ServiceGroup] = {}
    for r in records:
        if "@" not in r.sender_address or r.sender_address == owner_email.lower():
            continue
        local, sender_domain = r.sender_address.rsplit("@", 1)
        if sender_domain in FREEMAIL and not _SERVICE_SENDER_RE.search(local):
            continue
        domain = base_domain(sender_domain)
        if domain in PAYMENT_GATEWAYS:
            continue
        group = groups.setdefault(domain, ServiceGroup(domain))
        group.add(r.ref, r.sender_name, r.subject, r.date)
        if _SERVICE_SENDER_RE.search(local) or _BULK_SENDER_RE.search(local):
            group.service_sender = True
    # 계정 신호(가입·인증·결제 등)가 있는 곳 + 신호는 없지만 서비스 발신 주소에서 여러 번 온 곳.
    # 뒤쪽은 '약한 후보'로, AI가 회원에게만 오는 메일인지 보고 계정 여부를 정한다 (AI가 없으면 뺀다)
    return merge_same_services({d: g for d, g in groups.items() if g.signals or is_weak_candidate(g)})


# 대량 발송 주소 (뉴스레터·제품 알림)
_BULK_SENDER_RE = _re(r"news|hello|team|marketing|contact|update|digest|alert|message|mail|official|community|comms|cs$")


def is_weak_candidate(group: "ServiceGroup") -> bool:
    """계정 신호는 없지만 서비스가 보낸 메일이 여러 통인 곳. 사람끼리 주고받은 답장 위주면 뺀다."""
    if group.signals or not group.service_sender or group.messages < 2:
        return False
    replies = sum(1 for _, subj, _ref in group.recent if re.match(r"\s*(re|fw|fwd|답장|전달)\s*:", subj, re.I))
    return replies * 2 < group.messages


# --- 4단계: OpenAI 판별 (선택) --------------------------------------------------

CATEGORIES = ["쇼핑", "OTT", "음악", "배달", "여행", "교육", "생산성", "개발", "클라우드", "커리어", "금융", "게임", "SNS", "포털", "디자인", "창작", "통신", "기타"]


def _ai_input(group: ServiceGroup, body: str) -> Dict[str, Any]:
    recent = sorted(group.signals, key=lambda s: s["date"], reverse=True)[:6]
    item: Dict[str, Any] = {
        "domain": group.domain,
        "sender_names": [n for n, _ in group.sender_names.most_common(3)],
        "signals": [{"date": s["date"], "type": s["type"], "subject": redact(s["subject"])} for s in recent],
        "latest_payment_mail": redact(body)[:600],
    }
    if not group.signals:
        item["messages"] = group.messages
        subjects = list(dict.fromkeys(redact(subj) for _, subj, _ref in sorted(group.recent, key=lambda r: r[0], reverse=True)))
        item["recent_subjects"] = subjects[:15]
        item["latest_mail"] = redact(group.latest_body)
    return item


_AI_RULES_STRONG = (
    "services는 규칙으로 가입·로그인·결제·보안·회원 고지 등 계정 신호가 있는 곳입니다. "
    "로그인 코드·휴면 안내·개인정보 이용내역·약관 변경·예약·결제 메일은 회원에게 오므로 is_account는 기본적으로 true입니다. "
    "신호가 명백히 광고·뉴스레터 문구뿐일 때만 false."
)
_AI_RULES_WEAK = (
    "services는 계정 신호 없이 서비스 발신 주소에서 메일만 여러 통 온 곳입니다. recent_subjects와 latest_mail(최근 메일 본문 앞·끝)을 보고 판단합니다. "
    "본문 끝에 '계정이 있어서/회원이라서 받는 메일' 같은 문구가 있으면 true, '뉴스레터를 구독해서 받는 메일'이면 false. "
    "회원에게만 오는 메일(사용 기록·주간 통계·내 작업물·계정 상태·요금제·약관 변경·서비스 공지)이 있으면 is_account true. "
    "누구나 받을 수 있는 뉴스레터·광고·행사 안내뿐이거나, 사람이 보낸 업무 메일·대회·학교 공지면 false. 애매하면 false."
)


def ai_classify(items: List[Dict[str, Any]], api_key: str, weak: bool = False) -> Dict[str, Dict[str, Any]]:
    prompt = {
        "task": "각 domain에 대해 사용자가 그 서비스에 계정을 가지고 있는지, 서비스 이름·분류·유료 구독 여부를 판별합니다.",
        "rules": [
            "service: 한국 사용자에게 익숙한 서비스 이름. 발신 회사와 서비스가 다르면 서비스 이름 (예: data-bank.ai가 TestGlider 결제를 보내면 TestGlider).",
            f"category: {', '.join(CATEGORIES)} 중 하나.",
            _AI_RULES_WEAK if weak else _AI_RULES_STRONG,
            "subscription: 반복 결제되는 유료 구독이 확실할 때만 {plan, monthly_krw}. monthly_krw는 월 금액(원, 정수), 외화면 대략 원화로 환산. 아니면 null.",
            "교육용·학생 무료 요금제(Figma Education 등), 무료 플랜은 유료 구독이 아니므로 subscription null.",
            "메일에 없는 사실은 추측하지 않습니다.",
            "반드시 {\"services\": [{domain, service, category, is_account, subscription}]} JSON만 반환합니다.",
        ],
        "services": items,
    }
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You classify online accounts from email metadata. Return only valid JSON."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    # 분당 한도(429)·일시 오류(5xx)는 기다렸다가 다시 보낸다. 분석이 길어져도 결과 품질이 우선이다
    for attempt in range(AI_RETRIES):
        try:
            response = httpx.post(OPENAI_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=180)
        except httpx.HTTPError as exc:
            if attempt == AI_RETRIES - 1:
                raise RuntimeError("OpenAI 서버에 연결하지 못했습니다.") from exc
            time.sleep(2 ** attempt * 5)
            continue
        if response.status_code == 429 and "insufficient_quota" not in response.text and attempt < AI_RETRIES - 1:
            wait = float(response.headers.get("retry-after") or 0) or 2 ** attempt * 10
            time.sleep(min(wait, 90))
            continue
        if response.status_code >= 500 and attempt < AI_RETRIES - 1:
            time.sleep(2 ** attempt * 5)
            continue
        break
    if response.status_code == 401:
        raise RuntimeError("OpenAI API 키가 올바르지 않아요 (401). .env의 OPENAI_API_KEY를 확인해주세요.")
    if response.status_code == 429:
        raise RuntimeError("OpenAI 사용 한도를 넘었어요 (429). 요금제·크레딧을 확인해주세요.")
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI 분석 요청이 실패했습니다. ({response.status_code})")
    try:
        services = json.loads(response.json()["choices"][0]["message"]["content"])["services"]
        return {s["domain"]: s for s in services if isinstance(s, dict) and "domain" in s}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenAI 분석 결과를 읽지 못했습니다.") from exc


# --- 5단계: 화면용 계정 구조 ----------------------------------------------------

def _template(subject: str) -> str:
    """'X 인증 코드는 fjcdwn8g입니다' → 'X 인증 코드는 입니다' (코드·숫자 부분을 지운 제목 형식)"""
    return re.sub(r"\S*\d\S*", "", subject).strip()


def _md(d: Optional[date]) -> str:
    if not d:
        return "날짜 미상"
    # 올해가 아니면 연도를 붙인다
    prefix = f"{d.year}년 " if d.year != datetime.now().year else ""
    return f"{prefix}{d.month}월 {d.day}일"


# '확인이 필요한 계정'에 올릴 기간 (오래된 안내는 이미 지나간 일이라 노이즈)
INSIGHT_DAYS = {"만료 예정": 60, "만료됨": 60, "체험 종료": 60, "해지됨": 90, "결제 실패": 90, "활성": 60, "보안": 90, "휴면": 365}
# 만료 안내 제목에서 만료일을 읽는다 ("내일 만료", "3일 후 만료", "ends on Oct 4, 2025")
_EXPIRE_IN_RE = _re(r"(\d{1,3})\s?일\s?(후|뒤|남)|in (\d{1,3}) days?|(\d{1,3}) days? (left|remaining)")
_EXPIRE_ON_RE = _re(r"(?:ends?|expires?) on ([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})")


def _expiry(subject: str, sent: date) -> date:
    """만료 예정 안내의 만료일. 알 수 없으면 안내 2주 뒤로 본다."""
    m = _EXPIRE_ON_RE.search(subject)
    if m and m.group(1).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            pass
    if re.search(r"오늘|today", subject, re.I):
        return sent
    if re.search(r"내일|tomorrow", subject, re.I):
        return sent + timedelta(days=1)
    m = _EXPIRE_IN_RE.search(subject)
    if m:
        return sent + timedelta(days=int(m.group(1) or m.group(3) or m.group(4)))
    return sent + timedelta(days=14)
PLATFORM_DOMAINS = {
    "google.com", "apple.com", "microsoft.com", "kakao.com", "naver.com", "navercorp.com", "samsung.com",
    "facebook.com", "instagram.com", "x.com", "nate.com", "daum.net", "toss.im", "coupang.com", "youtube.com",
}
# 활동이 이보다 오래 없으면 탈퇴를 추천한다 (1년은 '미사용' 표시만)
WITHDRAW_DAYS = 730


def _won(n: int) -> str:
    return f"₩{n:,}"


# 교육용·학생·무료 요금제: 만료·갱신 안내가 와도 돈이 나가는 구독이 아니다 (예: Figma Education)
_FREE_PLAN_RE = _re(r"\beducation\b|\bedu (plan|license)|교육용|에듀케이션|학생\s?(플랜|요금제|인증|라이선스)|student (plan|status|license|verification)|"
                    r"free plan|무료\s?(플랜|요금제)|starter plan")
# 체험 전환일을 모를 때 체험 중으로 보는 기간 (학생 혜택은 1년까지 있다)
TRIAL_MAX_DAYS = 365


def build_billing(group: ServiceGroup, payments: List[Payment], ai: Optional[Dict[str, Any]],
                  today: date) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """결제 메일들로 (구독 정보, 1회성 결제 목록)을 만든다.

    - 구독 근거: 구독 상태 제목, 본문의 정기결제·구독·갱신 같은 말, 같은 금액이 일정한 간격으로 반복, AI 판단.
    - 주기: 결제 간격 → 본문의 월간·연간 같은 말 → 모르면 월간으로 보고 cycleKnown=False.
    - 상태: 해지·만료·체험 종료 안내가 마지막 결제보다 나중이면 그 상태.
      아니면 다음 결제일(본문 또는 마지막 결제 + 주기)이 유예 기간까지 지났는데 결제 메일이 없으면 '해지됨'(추정).
    """
    latest_state = group.latest_subscription()
    ai_sub = (ai or {}).get("subscription") if isinstance((ai or {}).get("subscription"), dict) else None
    payments = sorted(payments, key=lambda p: p.date)
    candidates = [p for p in payments if not p.one_time]
    # 같은 금액이 일정 간격으로 반복되는지 (주문이 섞인 쇼핑몰은 금액이 달라 걸러진다)
    base = next((p.amount for p in reversed(candidates) if p.amount), None)
    repeated = [p for p in candidates if _similar(p.amount, base)]
    gap_cycle = cycle_from_dates([p.date for p in repeated]) if len(repeated) >= 2 else None
    is_sub = bool(latest_state or any(p.recurring for p in candidates) or gap_cycle or (ai_sub and candidates))
    # 금액이 한 번도 안 보이고 교육용·무료 요금제 안내뿐이면 유료 구독이 아니다
    subjects = [p.subject for p in candidates] + ([latest_state[1].subject] if latest_state else [])
    if is_sub and not any(p.amount for p in candidates) and any(_FREE_PLAN_RE.search(x) for x in subjects):
        is_sub = False

    one_time_list = [p for p in payments if p.one_time or not is_sub]
    one_time = [
        {"date": p.date.isoformat(), "subject": p.subject[:120], "amount": p.amount}
        for p in sorted(one_time_list, key=lambda p: p.date, reverse=True)[:5]
    ]
    hints_all = [p.cycle_hint for p in candidates if p.cycle_hint]
    if is_sub and not latest_state and not gap_cycle and not hints_all and len(candidates) < 2 and not candidates[-1].trial:
        is_sub = False
        one_time = [{"date": p.date.isoformat(), "subject": p.subject[:120], "amount": p.amount}
                    for p in sorted(payments, key=lambda p: p.date, reverse=True)[:5]]
    if not is_sub:
        return None, one_time

    sub_payments = repeated if gap_cycle else candidates
    last = sub_payments[-1] if sub_payments else None
    hints = [p.cycle_hint for p in sub_payments if p.cycle_hint]
    cycle = gap_cycle or (max(set(hints), key=hints.count) if hints else None)
    cycle_known = cycle is not None
    cycle = cycle or "월간"

    amount = next((p.amount for p in reversed(sub_payments) if p.amount), None)
    monthly = _monthly(amount, cycle)
    if amount is None and ai_sub and isinstance(ai_sub.get("monthly_krw"), (int, float)) and ai_sub["monthly_krw"] > 0:
        monthly = int(ai_sub["monthly_krw"])
    # 마지막 결제 메일이 무료 체험·혜택이면 지금은 돈이 나가지 않는다. 금액은 체험 뒤 가격이다
    trial = bool(last and last.trial)
    trial_end = last.trial_end if trial else None

    state_date = latest_state[1].date if latest_state else None
    reference = last.date if last else state_date
    # 해지·만료 안내가 마지막 결제보다 나중에 왔으면 그 안내를 따른다
    notice = latest_state if latest_state and (last is None or (state_date and state_date >= last.date)) else None
    status, inferred, reason, next_billing = "활성", False, "", None
    if notice and notice[0] != "활성":
        status = notice[0]
        reason = f"{_md(state_date)} 안내: {notice[1].subject[:60]}"
        if status == "만료 예정" and state_date and _expiry(notice[1].subject, state_date) < today:
            status = "만료됨"
            reason = f"{_md(_expiry(notice[1].subject, state_date))}에 만료됐어요. 이후 결제 메일은 없어요."
    elif trial:
        end = date.fromisoformat(trial_end) if trial_end else None
        if (end and end + timedelta(days=CYCLE_GRACE[cycle]) < today) or (not end and reference + timedelta(days=TRIAL_MAX_DAYS) < today):
            # 체험이 끝났는데 유료 결제 메일이 없다
            status, inferred, trial = "해지됨", True, False
            reason = "무료 체험이 끝난 뒤 결제 메일이 없어요. 체험만 쓰고 해지한 것으로 보여요."
        else:
            next_billing = trial_end
            reason = f"무료 체험 중 · {_md(end)}부터 유료" if end else "무료 체험 중"
    elif reference:
        expected = (date.fromisoformat(last.next_billing) if last and last.next_billing
                    else reference + timedelta(days=CYCLE_DAYS[cycle]))
        if expected + timedelta(days=CYCLE_GRACE[cycle]) < today:
            status, inferred = "해지됨", True
            reason = (f"{cycle} 결제인데 {_md(expected)}쯤 와야 할 결제 메일이 없어요. "
                      "해지했거나 결제 수단·결제 메일 주소가 바뀐 것으로 보여요.")
        else:
            next_billing = expected.isoformat()
            reason = f"{cycle} 결제 {len(sub_payments)}번 확인" if sub_payments else ""

    # 결제 메일도 금액도 없는 '구독 중'은 근거가 제목뿐이다 (구독 권유·가입 환영 메일 등).
    # 결제 메일이 있는데 금액만 못 읽었으면 (예: 앱마다 영수증 형식이 다른 CapCut) 남기고 화면에 '확인 필요'로 보여 준다
    if status == "활성" and not amount and not monthly and not sub_payments and not trial:
        return None, [{"date": p.date.isoformat(), "subject": p.subject[:120], "amount": p.amount}
                      for p in sorted(payments, key=lambda p: p.date, reverse=True)[:5]]

    # 마지막 결제 뒤에 계정 삭제·휴면 안내가 왔으면 구독이 이어지지 않는다 (삭제될 계정에 결제가 계속될 수 없다)
    gone = [e for e in group.dormancy if e.date and reference and e.date >= reference and not _REACTIVATED_RE.search(e.subject)]
    if status in ("활성", "만료 예정") and gone:
        first = min(gone, key=lambda e: e.date)
        status, inferred, next_billing, trial = "해지됨", True, None, False
        reason = f"{_md(first.date)}에 계정 삭제·휴면 안내가 왔어요. 구독이 이어지지 않는 것으로 보여요."

    event_date = notice[1].date if notice else reference
    return {
        "plan": str((ai_sub or {}).get("plan") or "구독"),
        # 체험 중에는 매달 나가는 돈에 넣지 않는다
        "monthly": monthly if status in ("활성", "만료 예정") and not trial else None,
        "trial": trial,
        "trialEnd": trial_end if trial else None,
        "amount": amount,
        "cycle": cycle,
        "cycleKnown": cycle_known,
        "payments": len(sub_payments),
        "nextBilling": next_billing if status == "활성" else None,
        "status": status,
        "inferred": inferred,
        "statusReason": reason,
        "lastEventDate": event_date.isoformat() if event_date else None,
        "lastEventSubject": notice[1].subject if notice else (last.subject if last else ""),
    }, one_time


def build_insights(group: ServiceGroup, sub: Optional[Dict[str, Any]], today: date) -> List[Dict[str, str]]:
    """사용자가 붙여준 표처럼 '메일로 확인한 상태'와 'IDly가 제안할 행동'을 만든다."""
    insights: List[Dict[str, str]] = []

    def within(d: Optional[date], kind: str) -> bool:
        return d is not None and d >= today - timedelta(days=INSIGHT_DAYS[kind])

    sub_date = date.fromisoformat(sub["lastEventDate"]) if sub and sub["lastEventDate"] else None
    # 다음 결제일이 남아 있는 구독은 기간과 상관없이 올린다
    upcoming = bool(sub and sub["status"] == "활성" and sub["nextBilling"] and date.fromisoformat(sub["nextBilling"]) >= today)
    # 결제가 끊긴 구독은 마지막 결제 뒤 한 주기 + 90일까지만 올린다
    lapse_recent = bool(sub and sub["inferred"] and sub_date
                        and sub_date >= today - timedelta(days=CYCLE_DAYS[sub["cycle"]] + INSIGHT_DAYS["해지됨"]))
    if sub and (upcoming or lapse_recent or (not sub["inferred"] and within(sub_date, sub["status"]))):
        when = _md(sub_date)
        # 한 번 결제하는 금액 (연간이면 연 금액). 보고서 표의 월 결제는 monthly(월 환산)를 쓴다
        charge = sub.get("amount") or sub["monthly"]
        amount = f" {_won(charge)}" if charge else ""
        # 결제 메일은 있는데 금액을 못 읽었을 때 (앱 영수증 형식·PC/모바일 결제 경로에 따라 다르다)
        missing = "" if charge else " 메일에서 금액을 찾지 못했어요. 결제 내역에서 금액을 확인해 보세요."
        if sub.get("trial"):
            end = date.fromisoformat(sub["trialEnd"]) if sub.get("trialEnd") else None
            price = f"{sub['cycle']} {_won(charge)}" if charge else "유료"
            then = f"{_md(end)}부터 {price} 결제가 시작돼요." if end else f"체험이 끝나면 {price} 결제가 시작돼요."
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 무료 체험·혜택 시작. 지금은 결제되지 않고, {then}",
                             "advice": "계속 쓸 게 아니면 유료로 바뀌기 전에 해지"})
        elif upcoming:
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when}{amount} {sub['cycle']} 정기결제. 다음 결제 {_md(date.fromisoformat(sub['nextBilling']))} 예정.{missing}",
                             "advice": "계속 쓸지 확인하고, 원치 않으면 결제일 전에 구독 해지"})
        elif sub["status"] == "활성":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when}{amount} 유료 플랜·결제 안내. 이후 결제 메일은 없어요.{missing}",
                             "advice": "지금도 구독 중인지 결제 내역에서 확인"})
        elif sub["status"] == "만료 예정":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 구독 만료 예정 안내.",
                             "advice": "현재 만료·갱신 상태 확인 후, 필요할 때만 재구독"})
        elif sub["status"] == "만료됨":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 구독 만료 안내. {sub['statusReason']}",
                             "advice": "다시 쓸 계획이 없다면 결제 수단 연결을 확인하고 계정 정리"})
        elif sub["status"] == "결제 실패":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 결제 실패 안내. 결제 수단을 바꾸면 다시 청구될 수 있어요.",
                             "advice": "계속 쓸 거면 결제 수단 갱신, 아니면 구독을 확실히 해지"})
        elif sub["status"] == "체험 종료":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 무료 체험 종료 안내.",
                             "advice": "유료로 자동 전환되지 않았는지 결제 수단 확인"})
        elif sub["status"] == "해지됨" and sub["inferred"]:
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"마지막 결제 {when}. {sub['statusReason']}",
                             "advice": "결제 내역에서 해지가 맞는지 확인하고, 더 안 쓰면 계정 정리"})
        elif sub["status"] == "해지됨":
            insights.append({"kind": "구독", "date": sub["lastEventDate"],
                             "status": f"{when} 구독 해지·갱신 중단 안내.",
                             "advice": "더 쓰지 않는다면 계정 정리, 결제 수단 연결만 확인"})

    # 본인 확인을 묻거나 이상 징후를 알리는 안내만 (단순 '로그인되었습니다' 통지는 제외)
    alerts = [
        e for e in group.security
        if within(e.date, "보안") and (group.suspicious_login or _SECURITY_ALERT_RE.search(e.subject))
    ]
    if alerts:
        last = max(alerts, key=lambda e: e.date)
        repeat = f" 최근 {len(alerts)}번 반복됐어요." if len(alerts) > 1 else ""
        what = "의심스러운 로그인 시도 안내" if group.suspicious_login else f"보안 안내({last.subject[:50]})"
        if group.suspicious_login and len(alerts) >= 3:
            insights.append({"kind": "보안", "date": last.date.isoformat(),
                             "status": f"{_md(last.date)} {what}.{repeat} 이런 안내가 반복되면 다른 사람이 비밀번호를 알고 로그인하려는 것일 수 있어요.",
                             "advice": "본인 시도가 아니면 비밀번호를 바꾸고 2단계 인증 켜기. 같은 비밀번호를 쓰는 다른 서비스도 함께 변경"})
        else:
            insights.append({"kind": "보안", "date": last.date.isoformat(),
                             "status": f"{_md(last.date)} {what}.{repeat} 침해 성공의 증거는 아니에요.",
                             "advice": "본인 시도였는지 확인 → 아니라면 로그인 세션과 비밀번호 점검"})

    dormancy = [e for e in group.dormancy if within(e.date, "휴면")]
    if dormancy:
        last = max(dormancy, key=lambda e: e.date)
        backup = re.search(r"back ?up|백업|account data", last.subject, re.I)
        insights.append({"kind": "휴면", "date": last.date.isoformat() if last.date else "",
                         "status": f"{_md(last.date)} {'계정 데이터 백업·삭제' if backup else '장기 미사용·휴면'} 안내: {last.subject[:60]}",
                         "advice": "백업 파일 보관 여부와 현재 계정 상태 확인" if backup
                         else "유지할 계정인지 결정하고 현재 계정 상태 확인"})
    return insights


def to_account(group: ServiceGroup, ai: Optional[Dict[str, Any]], payments: List[Payment], owner_email: str,
               today: date) -> Dict[str, Any]:
    last_activity = group.latest("로그인", "인증", "결제", "구독")
    last_login = max(filter(None, [group.latest("로그인", "인증")] + [
        a[2].isoformat() for a in group.google_activity if a[0] == "새 기기 로그인"]), default=None)
    reference = last_activity or (group.last_seen.isoformat() if group.last_seen else None)
    idle_days = (today - date.fromisoformat(reference)).days if reference else None
    dormant_notice = any(e.date and e.date >= today - timedelta(days=INSIGHT_DAYS["휴면"]) for e in group.dormancy)
    # 로그인 수단으로 쓰는 핵심 플랫폼은 메일이 뜸해도 쓰고 있을 가능성이 높다. 휴면 안내가 왔을 때만 미사용으로 본다
    unused = dormant_notice or (idle_days is not None and idle_days > UNUSED_DAYS and group.domain not in PLATFORM_DOMAINS)

    subscription, one_time = build_billing(group, payments, ai, today)
    insights = build_insights(group, subscription, today) + google_insights(group)
    security = next((i for i in insights if i["kind"] == "보안"), None)
    paying = subscription is not None and subscription["status"] in ("활성", "만료 예정")

    # 탈퇴 추천은 휴면·삭제 안내가 왔거나 2년 넘게 활동이 없을 때만 (1년은 '미사용' 표시만)
    if unused and paying:
        recommended = "구독 해지"
    elif dormant_notice or (unused and idle_days is not None and idle_days > WITHDRAW_DAYS):
        recommended = "탈퇴"
    else:
        recommended = None

    known = KNOWN_SERVICES.get(group.domain)
    category = known[1] if known else (ai or {}).get("category")
    return {
        "id": group.domain,
        # 알려진 서비스 이름 > AI가 정한 이름 > 발신자 이름 정리
        "service": known[0] if known else ((ai or {}).get("service") or service_name(group)),
        "category": category if category in CATEGORIES else "기타",
        "email": owner_email,
        "signupDate": min(filter(None, [group.earliest("가입"), group.first_seen.isoformat() if group.first_seen else None]), default=""),
        "lastLogin": last_login,
        # 이 서비스에서 마지막으로 받은 메일 (광고·안내 포함). 로그인 메일이 없을 때 보여준다
        "lastSeen": group.last_seen.isoformat() if group.last_seen else None,
        "subscription": subscription,
        # 구독이 아닌 결제 (주문·예약 등 1회성). 최근 5건
        "oneTimePayments": one_time,
        "unused": unused,
        "securityAlert": security["status"] if security else None,
        "insights": insights,
        # 연결 권한은 메일만으로 알 수 없어 비워 둔다
        "permissions": [],
        "recommended": recommended,
        "signals": sorted(group.signals, key=lambda s: s["date"], reverse=True)[:10],
    }


_GOOGLE_SHARE_RE = _re(
    r"Google 계정 데이터 일부를 (.+?)에 공유하셨습니다|Google 계정 데이터에 대한 (.+?)의 액세스를 허용|"
    r"(?:you )?(?:gave|granted) (.+?) access to (?:some of )?your Google Account|(.+?) (?:was granted|now has) access to (?:some of )?your Google Account"
)
_GOOGLE_GENERIC_RE = _re(r"^\s*(보안 알림|중요 보안 알림|security alert|critical security alert)\s*$")
_GOOGLE_BODY = [
    ("앱 비밀번호 생성", _re(r"만든 앱 비밀번호\s+\S+@\S+\s+(.+?)에 비밀번호를 생성하지|app password (?:was )?created.{0,80}?for (.+?)[,.]")),
    ("앱 비밀번호 삭제", _re(r"앱 비밀번호가 삭제|app password (?:was )?(?:deleted|removed)")),
    ("액세스 허용", _re(r"Google 계정 데이터에 대한 (.+?)의 액세스를 허용하셨습니다|(.+?) (?:was granted|has) access to")),
    ("새 기기 로그인", _re(r"\]\s*(.+?)에서 새로 로그인함|new sign-in on (.+?)[\s.]")),
    ("의심 활동", _re(r"의심스러운|suspicious|차단|blocked|someone (?:knows|has) your password|critical")),
    ("비밀번호 변경", _re(r"비밀번호가 변경|password (?:was )?changed")),
]
GOOGLE_ALERT_DAYS = 90
MAX_GOOGLE_BODIES = 40


def read_google_activity(group: "ServiceGroup", fetch_body: Callable[[MailRef], Optional[Message]]) -> List[Dict[str, str]]:
    """Google 계정 알림을 읽어 (1) Google로 로그인한 앱 목록을 돌려주고 (2) 최근 보안 활동을 group.google_activity에 남긴다."""
    apps: Dict[str, str] = {}
    for when, subject, _ref in group.recent:
        m = _GOOGLE_SHARE_RE.search(subject)
        if m:
            name = next(x for x in m.groups() if x).strip()
            if when and when > date.min and when.isoformat() > apps.get(name, ""):
                apps[name] = when.isoformat()
    activity: List[Tuple[str, str, date]] = []  # (종류, 대상, 날짜)
    since = datetime.now().date() - timedelta(days=GOOGLE_ALERT_DAYS)
    generic = sorted((e for e in group.security if e.date and e.date >= since and _GOOGLE_GENERIC_RE.search(e.subject)),
                     key=lambda e: e.date, reverse=True)[:MAX_GOOGLE_BODIES]
    for event in generic:
        try:
            message = fetch_body(event.ref)
            text = _SPACE_RE.sub(" ", _text_body(message))[:800] if message else ""
        except Exception:
            text = ""
        for kind, pattern in _GOOGLE_BODY:
            m = pattern.search(text)
            if m:
                target = next((x for x in m.groups() if x), "") if m.groups() else ""
                activity.append((kind, target.strip()[:40], event.date))
                if kind == "액세스 허용" and target and event.date.isoformat() > apps.get(target.strip(), ""):
                    apps[target.strip()] = event.date.isoformat()
                break
    group.google_activity = activity
    return [{"app": name, "date": d, "provider": "Google"} for name, d in sorted(apps.items(), key=lambda kv: kv[1], reverse=True)]


def google_insights(group: "ServiceGroup") -> List[Dict[str, str]]:
    activity = getattr(group, "google_activity", [])
    out: List[Dict[str, str]] = []
    suspicious = [a for a in activity if a[0] in ("의심 활동", "비밀번호 변경")]
    if suspicious:
        last = max(suspicious, key=lambda a: a[2])
        out.append({"kind": "보안", "date": last[2].isoformat(),
                    "status": f"{_md(last[2])} Google 계정 {last[0]} 알림. 최근 {GOOGLE_ALERT_DAYS}일 {len(suspicious)}번.",
                    "advice": "본인이 한 일이 아니면 바로 비밀번호를 바꾸고 myaccount.google.com/notifications에서 활동 확인"})
    created = [a for a in activity if a[0] == "앱 비밀번호 생성"]
    if created:
        deleted = sum(1 for a in activity if a[0] == "앱 비밀번호 삭제")
        names = ", ".join(dict.fromkeys(a[1] for a in created if a[1]))
        last = max(created, key=lambda a: a[2])
        out.append({"kind": "보안", "date": last[2].isoformat(),
                    "status": f"최근 {GOOGLE_ALERT_DAYS}일 앱 비밀번호 {len(created)}번 생성({names}), {deleted}번 삭제. 앱 비밀번호는 2단계 인증 없이 메일을 읽을 수 있어요.",
                    "advice": "myaccount.google.com/apppasswords에서 지금 쓰지 않는 앱 비밀번호 삭제"})
    devices = [a for a in activity if a[0] == "새 기기 로그인"]
    if devices:
        names = ", ".join(dict.fromkeys(a[1] for a in devices if a[1]))
        last = max(devices, key=lambda a: a[2])
        out.append({"kind": "보안", "date": last[2].isoformat(),
                    "status": f"최근 {GOOGLE_ALERT_DAYS}일 새 기기 로그인 {len(devices)}번 ({names}).",
                    "advice": "모두 내 기기인지 확인하고, 모르는 기기는 myaccount.google.com/device-activity에서 로그아웃"})
    return out


def merge_by_final_name(groups: Dict[str, ServiceGroup], ai_results: Dict[str, Dict[str, Any]],
                        payments: Dict[str, List[Payment]]) -> Dict[str, ServiceGroup]:
    """보고서에 나갈 이름(알려진 이름 > AI 이름 > 발신자 이름)이 같은 서비스를 하나로 합친다."""
    merged: Dict[str, ServiceGroup] = {}
    keys: Dict[str, str] = {}
    for domain, group in sorted(groups.items(), key=lambda kv: -len(kv[1].signals)):
        known = KNOWN_SERVICES.get(domain)
        name = known[0] if known else ((ai_results.get(domain) or {}).get("service") or service_name(group))
        key = _name_key(name)
        target = keys.get(key) if key else None
        if target:
            merged[target].merge(group)
            payments[target] = payments.get(target, []) + payments.pop(domain, [])
        else:
            merged[domain] = group
            keys[key] = domain
    return merged


# --- App Store 영수증: 한 영수증에 여러 앱 구독이 들어 있다. 앱별 구독으로 나눈다 ------------------------

_APPLE_RECEIPT_RE = _re(r"영수증|receipt")
# "Claude by Anthropic Claude Pro - Monthly(월간) Anthropic PBC 2026년 10월 30일에 갱신 예정 ₩33,000"
_APPLE_ITEM_KO_RE = re.compile(
    r"(?P<name>[^₩]{2,120}?)\((?P<cycle>월간|연간|주간|1개월|12개월|1년|7일|1주|30일)\)\s*(?P<seller>[^₩]{0,60}?)\s*"
    r"(?P<y>\d{4})년\s*(?P<m>\d{1,2})월\s*(?P<d>\d{1,2})일에 갱신 예정[^₩]{0,40}?₩\s?(?P<amt>[\d,]+)"
)
# "Claude Pro (Monthly) Anthropic PBC Renews Oct 30, 2026 ₩33,000" / "$20.00"
_APPLE_ITEM_EN_RE = re.compile(
    r"(?P<name>[^₩$]{2,120}?)\((?P<cycle>Monthly|Yearly|Annual|Weekly)\)\s*(?P<seller>[^₩$]{0,60}?)\s*"
    r"Renews (?P<mon>[A-Z][a-z]{2})[a-z]*\.? (?P<d>\d{1,2}),? (?P<y>\d{4})\s*(?P<cur>[₩$])\s?(?P<amt>[\d,.]+)"
)
_APPLE_CYCLE = {"월간": "월간", "1개월": "월간", "30일": "월간", "Monthly": "월간", "연간": "연간", "1년": "연간", "12개월": "연간",
                "Yearly": "연간", "Annual": "연간", "주간": "주간", "7일": "주간", "1주": "주간", "Weekly": "주간"}
MAX_APPLE_RECEIPTS = 120


class AppStoreItem(NamedTuple):
    sent: date
    name: str
    seller: str
    cycle: str
    renews: date
    amount: int


def parse_apple_receipt(text: str, sent: date) -> List[AppStoreItem]:
    """App Store 영수증 본문에서 갱신되는 구독 항목만 뽑는다 (일회성 앱 구매는 갱신일이 없어서 빠진다)."""
    start = re.search(r"Apple (계정|Account|ID)\s*:?\s*\S+", text)
    body = text[start.end():] if start else text
    items: List[AppStoreItem] = []
    for m in _APPLE_ITEM_KO_RE.finditer(body):
        try:
            renews = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            continue
        items.append(AppStoreItem(sent, _clean_app_name(m.group("name")), m.group("seller").strip(),
                                  _APPLE_CYCLE[m.group("cycle")], renews, int(m.group("amt").replace(",", ""))))
    for m in _APPLE_ITEM_EN_RE.finditer(body):
        if m.group("mon").lower() not in _MONTHS:
            continue
        try:
            renews = date(int(m.group("y")), _MONTHS[m.group("mon").lower()], int(m.group("d")))
        except ValueError:
            continue
        amount = float(m.group("amt").replace(",", ""))
        won = int(round(amount * USD_KRW)) if m.group("cur") == "$" else int(amount)
        items.append(AppStoreItem(sent, _clean_app_name(m.group("name")), m.group("seller").strip(),
                                  _APPLE_CYCLE[m.group("cycle")], renews, won))
    return items


def _clean_app_name(name: str) -> str:
    name = re.sub(r"^(주문 ID|문서|Order ID|Document)\s*:?\s*\S+\s*", "", name.strip())
    name = re.sub(r"\s*[-–]\s*(Monthly|Yearly|Annual|Weekly)\s*$", "", name, flags=re.I)
    name = re.sub(r"\s*(Monthly|Yearly|Annual|Weekly) Subscription\s*$", "", name, flags=re.I)
    name = re.sub(r"\s*(Monthly|Yearly|Annual|Weekly)\s*$", "", name, flags=re.I)
    # "네이버 MYBOX - NAVER MYBOX Apple 80GB 1개월 정기 결제" → "네이버 MYBOX"
    name = re.sub(r"\s*(\d+\s?(개월|년)|월간|연간|주간)\s?(정기\s?결제|구독|이용권)?\s*$", "", name)
    # 한글 앱 이름 뒤에 " - 영문 이름·용량"이 붙으면 앞만 쓴다
    head = name.split(" - ")[0]
    if re.search(r"[가-힣]", head):
        name = head
    return name.strip()[:60]


def _app_key(name: str) -> str:
    """같은 구독을 묶는 키 (iCloud 50GB → 200GB 업그레이드도 같은 구독)."""
    return re.sub(r"\d+\s?(gb|tb)|[^a-z가-힣]", "", name.lower())


def _same_app(a: AppStoreItem, b: AppStoreItem) -> bool:
    """판매자가 같고(Apple 자체 서비스 제외) 앱 이름 앞부분이 같으면 같은 앱의 다른 요금제다."""
    if not a.seller or a.seller != b.seller or a.seller.lower().startswith("apple"):
        return False
    head = lambda n: _name_key(n.split(":")[0])[:8]
    return head(a.name) == head(b.name)


def app_store_accounts(items: List[AppStoreItem], owner_email: str, today: date,
                       categories: Dict[str, str]) -> List[Dict[str, Any]]:
    """App Store 구독 항목을 앱별 계정(구독)으로 만든다. 결제 수단은 Apple이므로 해지도 App Store에서 한다."""
    by_app: Dict[str, List[AppStoreItem]] = {}
    for item in items:
        by_app.setdefault(_app_key(item.name), []).append(item)
    accounts: List[Dict[str, Any]] = []
    for key, group in by_app.items():
        group.sort(key=lambda i: i.sent)
        last = group[-1]
        cycle = last.cycle
        status, inferred, next_billing = "활성", False, None
        # 같은 앱의 다른 요금제 영수증이 더 나중에 왔으면 요금제를 바꾼 것 (CapCut Pro → Standard)
        newer = next((g[-1] for k, g in by_app.items() if k != key and _same_app(g[-1], last) and max(i.sent for i in g) > last.sent), None)
        if newer:
            status, inferred = "해지됨", False
            reason = f"{_md(newer.sent)}에 같은 앱의 다른 요금제({newer.name[-30:]})로 바뀌었어요."
        elif last.renews >= today:
            next_billing = last.renews.isoformat()
            reason = f"App Store 영수증 {len(group)}번 확인"
        elif last.renews + timedelta(days=CYCLE_GRACE[cycle]) < today:
            status, inferred = "해지됨", True
            reason = (f"{_md(last.renews)} 갱신 예정이었는데 그 뒤 영수증이 없어요. "
                      "App Store에서 해지했거나 결제 수단이 바뀐 것으로 보여요.")
        else:
            reason = f"{_md(last.renews)} 갱신 예정 (영수증 확인 중)"
        monthly = _monthly(last.amount, cycle)
        sub = {
            "plan": last.name, "monthly": monthly if status == "활성" else None, "amount": last.amount,
            "cycle": cycle, "cycleKnown": True, "payments": len(group), "nextBilling": next_billing,
            "status": status, "inferred": inferred, "statusReason": reason,
            "lastEventDate": last.sent.isoformat(), "lastEventSubject": "Apple 영수증",
            "billedVia": "App Store",
        }
        insights: List[Dict[str, str]] = []
        if status == "활성" and next_billing:
            insights.append({"kind": "구독", "date": last.sent.isoformat(),
                             "status": f"{_md(last.sent)} {_won(last.amount)} {cycle} 정기결제 (App Store). 다음 결제 {_md(last.renews)} 예정.",
                             "advice": "계속 쓸지 확인하고, 원치 않으면 결제일 전에 iPhone 설정 → Apple 계정 → 구독에서 해지"})
        elif inferred and last.renews >= today - timedelta(days=INSIGHT_DAYS["해지됨"]):
            insights.append({"kind": "구독", "date": last.sent.isoformat(),
                             "status": f"마지막 결제 {_md(last.sent)}. {reason}",
                             "advice": "iPhone 설정 → Apple 계정 → 구독에서 해지됐는지 확인"})
        name_key = _name_key(last.name)
        first = _name_key(re.split(r"[\s:]", last.name.strip())[0])
        category = next((c for k, c in categories.items() if k and len(k) >= 3 and (k in name_key or (len(first) >= 4 and first in k))), None)
        accounts.append({
            "id": f"apps.apple.com/{key[:40]}", "service": last.name,
            # Apple 자체 서비스는 이름으로 먼저 정한다 (Apple 계정의 분류를 따라가지 않게)
            "category": ("클라우드" if "icloud" in name_key else "음악" if "music" in name_key else category or "기타"),
            "email": owner_email, "signupDate": group[0].sent.isoformat(), "lastLogin": None,
            "lastSeen": last.sent.isoformat(), "subscription": sub, "oneTimePayments": [], "unused": False,
            "securityAlert": None, "insights": insights, "permissions": [], "recommended": None,
            "signals": [{"date": i.sent.isoformat(), "type": "결제", "subject": f"Apple 영수증: {i.name} {_won(i.amount)}"}
                        for i in reversed(group[-10:])],
        })
    return accounts


def _keep_account(group: ServiceGroup, ai: Optional[Dict[str, Any]]) -> bool:
    """계정 신호가 있는 곳은 AI가 아니라고 할 때만 뺀다. 약한 후보는 AI가 계정이라고 확인한 곳만 남긴다."""
    if any(sig["type"] != "가입" for sig in group.signals):
        return True
    if group.signals:
        return (ai or {}).get("is_account", True) is not False
    return (ai or {}).get("is_account") is True


def plan_bodies(groups: Dict[str, "ServiceGroup"]) -> List[MailRef]:
    """분석 단계마다 읽을 본문을 미리 정한다 (아래 discover_accounts의 각 단계와 같은 기준)."""
    refs: List[MailRef] = []
    budget = MAX_TOTAL_BODIES
    for g in groups.values():
        events = sorted(set(g.payments), key=lambda e: e.date, reverse=True)
        for event in events[:MAX_PAYMENT_BODIES]:
            if budget <= 0:
                break
            budget -= 1
            refs.append(event.ref)
        latest = g.latest_subscription()
        if not events and latest and budget > 0:
            budget -= 1
            refs.append(latest[1].ref)
    apple = groups.get("apple.com")
    if apple:
        refs += [e.ref for e in sorted((e for e in apple.payments if e.date and _APPLE_RECEIPT_RE.search(e.subject)),
                                       key=lambda e: e.date, reverse=True)[:MAX_APPLE_RECEIPTS]]
    recent = datetime.now().date() - timedelta(days=RECENT_DAYS)
    for g in groups.values():
        auth = sorted((e for e in g.auth if e.date and e.date >= recent), key=lambda e: e.date, reverse=True)
        refs += [e.ref for e in auth[:MAX_AUTH_BODIES]]
    google = groups.get("google.com")
    if google:
        since = datetime.now().date() - timedelta(days=GOOGLE_ALERT_DAYS)
        refs += [e.ref for e in sorted((e for e in google.security if e.date and e.date >= since and _GOOGLE_GENERIC_RE.search(e.subject)),
                                       key=lambda e: e.date, reverse=True)[:MAX_GOOGLE_BODIES]]
    if os.getenv("OPENAI_API_KEY", "").strip():
        refs += [max(g.recent, key=lambda r: r[0])[2] for g in groups.values() if not g.signals and g.recent]
    return list(dict.fromkeys(refs))


def prefetch_bodies(refs: List[MailRef], fetch_body: Callable[[MailRef], Optional[Message]],
                    report: Callable[..., None]) -> Dict[MailRef, Optional[Message]]:
    """본문을 동시에 읽는다. 하나가 실패해도 나머지는 계속 읽는다."""
    cache: Dict[MailRef, Optional[Message]] = {}
    lock = threading.Lock()
    report("메일 본문 확인", 0, len(refs))

    def one(ref: MailRef) -> None:
        try:
            message = fetch_body(ref)
        except Exception:
            message = None
        with lock:
            cache[ref] = message
            report("메일 본문 확인", len(cache), len(refs))

    with ThreadPoolExecutor(max_workers=BODY_PARALLEL) as pool:
        list(pool.map(one, refs))
    return cache


def discover_accounts(
    records: List[HeaderRecord],
    owner_email: str,
    fetch_body: Callable[[MailRef], Optional[Message]],
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def report(step: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
        if on_progress is None:
            return
        try:
            on_progress(step, done, total)
        except TypeError:  # 단계 이름만 받는 콜백
            on_progress(step)

    report("헤더 분석")
    groups = collect_groups(records, owner_email)

    # 읽을 본문을 먼저 모두 정해 한꺼번에 동시에 읽는다 (메일 서버 연결 여러 개). 아래 단계는 읽어 둔 본문을 쓴다
    cache = prefetch_bodies(plan_bodies(groups), fetch_body, report)

    def fetched(ref: MailRef) -> Optional[Message]:
        if ref in cache:
            return cache[ref]
        try:
            return fetch_body(ref)
        except Exception:
            return None

    def read(ref: MailRef) -> str:
        message = fetched(ref)
        try:
            return _text_body(message) if message else ""
        except Exception:
            return ""

    # 결제·구독 메일은 서비스마다 최근 몇 통의 본문을 서버 안에서만 읽는다
    # (금액·다음 결제일, 1회성 결제인지 구독인지). 더 오래된 결제 메일은 제목·날짜만으로 주기 판단에 쓴다
    report("결제 메일 확인")
    bodies: Dict[str, str] = {}  # 서비스별 가장 최근 결제 메일 본문 (AI 입력용)
    payments: Dict[str, List[Payment]] = {}
    budget = MAX_TOTAL_BODIES

    for d, g in groups.items():
        events = sorted(set(g.payments), key=lambda e: e.date, reverse=True)
        latest = g.latest_subscription()
        if not events and latest is None:
            continue
        items: List[Payment] = []
        for i, event in enumerate(events):
            body = ""
            if i < MAX_PAYMENT_BODIES and budget > 0:
                budget -= 1
                body = read(event.ref)
                if i == 0:
                    bodies[d] = body
            items.append(read_payment(event, body))
        payments[d] = items
        # 결제 메일은 없고 구독 안내만 있으면 그 본문을 AI 입력으로
        if d not in bodies and latest and budget > 0:
            budget -= 1
            bodies[d] = read(latest[1].ref)

    # App Store 영수증은 앱별 구독으로 나눈다. 영수증마다 본문을 읽는다 (Apple 계정 자체의 구독으로 합치지 않는다)
    app_items: List[AppStoreItem] = []
    apple = groups.get("apple.com")
    if apple:
        receipts = sorted((e for e in apple.payments if e.date and _APPLE_RECEIPT_RE.search(e.subject)),
                          key=lambda e: e.date, reverse=True)[:MAX_APPLE_RECEIPTS]
        for event in receipts:
            app_items += parse_apple_receipt(read(event.ref), event.date)
        # 영수증은 앱 구독·앱 내 구입이지 Apple 자체 구독이 아니다 (Apple 계정에는 기기 주문 같은 것만 남긴다)
        if receipts:
            payments["apple.com"] = [p for p in payments.get("apple.com", []) if not _APPLE_RECEIPT_RE.search(p.subject)]

    # 인증 코드 메일은 제목만으로는 모른다. 최근 것 1통의 본문에 의심 로그인 안내가 있으면
    # 같은 형식의 최근 인증 메일을 모두 보안 안내로 본다 (X 등)
    report("보안 메일 확인")
    recent = datetime.now().date() - timedelta(days=RECENT_DAYS)
    for g in groups.values():
        auth = [e for e in g.auth if e.date and e.date >= recent]
        if not auth:
            continue
        # 최근 것부터 몇 통까지 본문을 본다. 같은 제목 형식이라도 평범한 로그인 코드와 의심 로그인 안내가 섞여 온다 (X 등)
        for event in sorted(auth, key=lambda e: e.date, reverse=True)[:MAX_AUTH_BODIES]:
            text = read(event.ref)[:1500]
            if _SUSPICIOUS_BODY_RE.search(text):
                g.suspicious_login = True
                g.security.append(event)

    # Google 계정 알림: '보안 알림' 같은 짧은 제목은 본문을 읽어야 무슨 일인지 안다.
    # 'Google 계정 데이터를 ○○에 공유' 메일은 Google로 로그인한 서비스 목록이 된다
    google = groups.get("google.com")
    connected_apps = read_google_activity(google, fetched) if google else []

    ai_results: Dict[str, Dict[str, Any]] = {}
    ai_error: Optional[str] = None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key and groups:
        report("AI 판별")
        for g in groups.values():
            if not g.signals and g.recent:
                ref = max(g.recent, key=lambda r: r[0])[2]
                text = read(ref)
                g.latest_body = f"{text[:400]} … {text[-500:]}" if len(text) > 900 else text
        strong = [_ai_input(g, bodies.get(d, "")) for d, g in groups.items() if g.signals]
        weak = [_ai_input(g, "") for g in groups.values() if not g.signals]
        # 묶음을 동시에 보낸다. AI가 실패해도(키 오류·한도 초과 등) 그 묶음만 규칙 결과로 보고서를 낸다
        batches = [(items[i:i + AI_CHUNK], is_weak) for items, is_weak in ((strong, False), (weak, True))
                   for i in range(0, len(items), AI_CHUNK)]
        report("AI 판별", 0, len(batches))
        with ThreadPoolExecutor(max_workers=AI_PARALLEL) as pool:
            futures = [pool.submit(ai_classify, batch, api_key, is_weak) for batch, is_weak in batches]
            for n, future in enumerate(as_completed(futures), 1):
                try:
                    ai_results.update(future.result())
                except RuntimeError as e:
                    ai_error = str(e)
                    print(f"[analysis] AI 판별 일부 실패, 그 부분은 규칙 결과만 사용: {e}")
                report("AI 판별", n, len(batches))

    groups = merge_by_final_name(groups, ai_results, payments)

    today = datetime.now().date()
    accounts = [
        to_account(g, ai_results.get(d), payments.get(d, []), owner_email, today)
        for d, g in groups.items()
        if _keep_account(g, ai_results.get(d))
    ]
    # 확인할 것이 있는 계정을 먼저
    if app_items:
        categories = {_name_key(a["service"]): a["category"] for a in accounts}
        accounts += app_store_accounts(app_items, owner_email, today, categories)

    # Google로 로그인한 앱과 이름이 같은 계정에 표시한다
    by_name = {_name_key(a["service"]): a for a in accounts}
    for app in connected_apps:
        key = _name_key(app["app"])
        account = by_name.get(key) or (next(
            (a for a in accounts if len(key) >= 4 and (key in _name_key(a["service"]) or key == a["id"].split(".")[0])), None))
        if account is not None:
            app["account"] = account["id"]
            account["permissions"].append(f"Google 로그인 ({app['date']})")
    accounts.sort(key=lambda a: (not a["insights"], a["subscription"] is None, not a["unused"], a["service"]))
    return {
        "messages_scanned": len(records),
        "candidates": len(groups),
        "ai_used": bool(ai_results),
        "ai_error": ai_error,
        "model": OPENAI_MODEL if ai_results else None,
        "accounts": accounts,
        "connected_apps": connected_apps,
    }
