"""리포트 조치 링크: 계정마다 해지·탈퇴·보안 설정 페이지로 바로 가는 링크를 고른다.

고르는 순서 (docs/superpowers/specs/2026-10-19-action-links-design.md):
1. 큐레이션 — 공식 도움말·공식 페이지에서 직접 확인한 링크 (CURATED, 칸마다 확인한 출처를 함께 둔다)
2. 검색 캐시 — 예전에 웹 검색으로 찾아 도메인 검증을 통과한 링크 (DB action_links, 90일)
3. 웹 검색 — 결제한 리포트에서만. 서비스 이름·도메인·조치만 보낸다
4. 구글 검색 대체 링크 — 실패하지 않으므로 버튼이 항상 붙는다

IDly가 사용자를 엉뚱한 사이트로 보내는 것이 가장 나쁜 결과라, 큐레이션 밖의 링크는 is_valid를 통과해야만 쓴다.
서버는 찾은 URL을 직접 가져오지 않는다.
"""

import ipaddress
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

import db

ACTIONS = ("해지", "탈퇴", "보안")
CACHE_DAYS = 90

# App Store 구독(계정 id가 apps.apple.com/…)은 앱마다 해지 페이지가 따로 없고 Apple 구독 관리에서 끈다
APPLE_SUBSCRIPTIONS = "https://apps.apple.com/account/subscriptions"

# 도메인 → {조치: (URL, 확인한 출처)}. 확인하지 못한 칸은 비워 둔다 (추측으로 채우지 않는다)
CURATED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "netflix.com": {
        "해지": ("https://www.netflix.com/cancelplan", "https://help.netflix.com/en/node/407"),
    },
    "google.com": {
        "보안": ("https://myaccount.google.com/security", "https://support.google.com/accounts/answer/46526"),
    },
}

# 기준 도메인 밖에 있는 같은 회사의 페이지 (검색 결과 검증에도 쓴다)
ALLOWED_DOMAINS: Dict[str, List[str]] = {
    "youtube.com": ["google.com"],
    "google.com": ["youtube.com"],
}

_LABELS = {
    "해지": ("해지하러 가기", "해지 방법 찾아보기"),
    "탈퇴": ("탈퇴하러 가기", "탈퇴 방법 찾아보기"),
    "보안": ("보안 설정 열기", "보안 설정 방법 찾아보기"),
}


def base_domain(account_id: str) -> str:
    account_id = (account_id or "").strip().lower()
    if account_id.startswith("apps.apple.com/"):
        return "apple.com"
    return account_id


def _host_allowed(host: str, domain: str) -> bool:
    allowed = [domain] + ALLOWED_DOMAINS.get(domain, [])
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_valid(url: str, domain: str) -> bool:
    """https, 사용자 정보·포트 없음, IP 아닌 호스트가 그 계정의 도메인(하위 도메인 포함) 또는 허용 도메인일 때만."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme != "https" or parts.username is not None or parts.password is not None:
            return False
        # 포트는 빈 값(`host:`)까지 거절한다. IPv6 호스트도 콜론이 있지만 어차피 IP라 거절된다
        if ":" in parts.netloc:
            return False
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return _host_allowed(host, base_domain(domain))


def fallback_link(service: str, action: str) -> Dict[str, str]:
    return {"url": "https://www.google.com/search?q=" + quote_plus(f"{service} {action} 방법"), "source": "fallback"}


def _cached(domain: str, action: str) -> Optional[str]:
    try:
        row = db.get_action_link(domain, action)
    except Exception as e:  # 캐시가 안 되면 없는 것으로 본다
        print(f"[links] 캐시 조회 실패 {domain} {action}: {e!r}")
        return None
    if not row or not is_valid(row["url"], domain):
        return None
    if datetime.fromisoformat(row["found_at"]) < datetime.now() - timedelta(days=CACHE_DAYS):
        return None
    return row["url"]


def resolve_offline(account: Dict[str, Any], action: str, use_cache: bool = True) -> Dict[str, str]:
    """네트워크 없이 고른다: 큐레이션 → (캐시) → 구글 검색 대체."""
    account_id = account.get("id") or ""
    domain = base_domain(account_id)
    if action == "해지" and account_id.lower().startswith("apps.apple.com/"):
        return {"url": APPLE_SUBSCRIPTIONS, "source": "curated"}
    curated = CURATED.get(domain, {}).get(action)
    if curated:
        return {"url": curated[0], "source": "curated"}
    if use_cache:
        url = _cached(domain, action)
        if url:
            return {"url": url, "source": "cache"}
    return fallback_link(account.get("service") or domain, action)


def label(action: str, source: str) -> str:
    found, search = _LABELS[action]
    return search if source == "fallback" else found
