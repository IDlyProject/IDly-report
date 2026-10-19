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
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

import httpx

import db

ACTIONS = ("해지", "탈퇴", "보안")
CACHE_DAYS = 90
ACTIVE = ("활성", "만료 예정")

# 웹 검색 상한: 리포트 하나당
MAX_SEARCHES = 20
SEARCH_WORKERS = 5
SEARCH_TIMEOUT = 10
SEARCH_DEADLINE = 20
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

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


# --- 리포트 데이터에 붙이기 ----------------------------------------------------------

_GOOGLE_PAGE_RE = re.compile(r"myaccount\.google\.com/([a-z-]+)")


def needed_actions(account: Dict[str, Any]) -> List[str]:
    """리포트가 버튼을 다는 조치: 구독 중이면 해지, 안 쓰는 계정이면 해지(구독 해지 권장) 또는 탈퇴."""
    actions: List[str] = []
    if account.get("subscription") and account["subscription"].get("status") in ACTIVE:
        actions.append("해지")
    if account.get("unused"):
        actions.append("해지" if account.get("recommended") == "구독 해지" else "탈퇴")
    return list(dict.fromkeys(actions))


def security_link(account: Dict[str, Any], insight: Dict[str, Any], use_cache: bool = True) -> Dict[str, str]:
    """보안 알림 하나의 링크. 안내 문구가 Google 계정의 특정 페이지를 가리키면 그 페이지로."""
    if base_domain(account.get("id") or "") == "google.com":
        match = _GOOGLE_PAGE_RE.search(insight.get("advice") or "")
        if match:
            return {"url": f"https://myaccount.google.com/{match.group(1)}", "source": "curated"}
    return resolve_offline(account, "보안", use_cache=use_cache)


def attach_offline(report: Dict[str, Any]) -> None:
    """네트워크 없이 모든 칸을 채운다. 이미 정해진 칸은 그대로 둔다. 어떤 실패도 리포트를 깨지 않는다."""
    for account in report.get("accounts") or []:
        service = account.get("service") or account.get("id") or ""
        actions = account.setdefault("actions", {})
        for action in needed_actions(account):
            if action not in actions:
                try:
                    actions[action] = resolve_offline(account, action)
                except Exception as e:
                    print(f"[links] {account.get('id')} {action}: {e!r}")
                    actions[action] = fallback_link(service, action)
        for insight in account.get("insights") or []:
            if insight.get("kind") == "보안" and "action" not in insight:
                try:
                    insight["action"] = security_link(account, insight)
                except Exception as e:
                    print(f"[links] {account.get('id')} 보안: {e!r}")
                    insight["action"] = fallback_link(service, "보안")


# --- 웹 검색 (결제한 리포트에서만) -----------------------------------------------------

_URL_RE = re.compile(r"https://[^\s)\]>\"'<]+")
_QUERY_WHAT = {"해지": "구독 해지(취소) 페이지", "탈퇴": "회원 탈퇴(계정 삭제) 페이지", "보안": "비밀번호·로그인 보안 설정 페이지"}


def _candidates(body: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            urls += [u.rstrip(".,;:!?)") for u in _URL_RE.findall(part.get("text") or "")]
            urls += [a.get("url") or "" for a in part.get("annotations") or [] if isinstance(a, dict)]
    return urls


def search_link(service: str, domain: str, action: str, api_key: str) -> Optional[str]:
    """OpenAI 웹 검색으로 그 조치 페이지 URL을 찾는다. 검증을 통과한 첫 URL, 없으면 None. 예외를 던지지 않는다.
    질의에는 서비스 이름·도메인·조치만 넣는다 (사용자 메일 주소·메일 내용은 보내지 않는다)."""
    query = (f"{service}({domain}) 계정의 {_QUERY_WHAT[action]} 공식 URL 하나만 답해. "
             f"{domain}의 공식 페이지나 공식 도움말에 있는 주소만. 모르면 NONE이라고만 답해.")
    try:
        response = httpx.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": os.getenv("OPENAI_MODEL", "gpt-4.1"), "tools": [{"type": "web_search"}], "input": query},
            timeout=SEARCH_TIMEOUT,
        )
        response.raise_for_status()
        candidates = _candidates(response.json())
    except Exception as e:
        print(f"[links] 검색 실패 {domain} {action}: {type(e).__name__}")
        return None
    return next((u for u in candidates if is_valid(u, domain)), None)


def upgrade_report(report: Dict[str, Any], api_key: Optional[str] = None) -> int:
    """구글 검색 대체 링크로 채운 칸만 웹 검색으로 다시 찾는다. 바꾼 칸 수를 돌려준다."""
    api_key = (os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key).strip()
    if not api_key:
        return 0
    slots: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], str]]] = {}
    services: Dict[Tuple[str, str], str] = {}
    for account in report.get("accounts") or []:
        domain = base_domain(account.get("id") or "")
        if not domain or "/" in domain:
            continue
        for action, link in (account.get("actions") or {}).items():
            if link.get("source") == "fallback":
                slots.setdefault((domain, action), []).append((account["actions"], action))
                services[(domain, action)] = account.get("service") or domain
        for insight in account.get("insights") or []:
            if (insight.get("action") or {}).get("source") == "fallback":
                slots.setdefault((domain, "보안"), []).append((insight, "action"))
                services[(domain, "보안")] = account.get("service") or domain
    keys = list(slots)[:MAX_SEARCHES]
    if not keys:
        return 0
    pool = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="links")
    futures = {pool.submit(search_link, services[k], k[0], k[1], api_key): k for k in keys}
    done, _ = wait(futures, timeout=SEARCH_DEADLINE)
    pool.shutdown(wait=False, cancel_futures=True)
    changed = 0
    for future in done:
        url = future.result()
        if not url:
            continue
        domain, action = futures[future]
        for holder, key in slots[(domain, action)]:
            holder[key] = {"url": url, "source": "search"}
            changed += 1
        try:
            db.put_action_link(domain, action, url, "search")
        except Exception as e:
            print(f"[links] 캐시 저장 실패 {domain} {action}: {e!r}")
    return changed
