"""카카오·Apple 로그인.

mongle(웹)과 같은 방식이다.

카카오 (REST API, 인가 코드 방식) — 팝업 창에서 카카오 로그인 → 콜백에서 서버가 code를 토큰으로 바꾼다
- KAKAO_REST_API_KEY, KAKAO_REDIRECT_URI(카카오 콘솔에 등록한 주소와 정확히 같아야 한다), KAKAO_CLIENT_SECRET(켰을 때만)
- 동의 항목: 카카오계정(이메일) — 비즈 앱이 아니면 이메일을 못 받을 수 있다. 그래도 로그인은 된다

Apple (Sign in with Apple JS, 팝업)
- 화면에서 AppleID.auth.signIn(usePopup)으로 받은 id_token을 서버로 보내, Apple 공개키로 서명·발급자·대상·nonce를 확인한다
- APPLE_SERVICES_ID(웹용 Services ID), APPLE_WEB_REDIRECT_URI(Services ID에 등록한 https 주소)
- 토큰 교환(client_secret)이 필요 없어서 개인키 없이 동작한다

목업 로그인 (AUTH_MODE=mock, 지금 기본값)
- 카카오·네이버·Google·Apple 버튼은 화면만 있고, 누르면 그 서비스 이름의 테스트 계정으로 바로 로그인한다
- 실제 연동은 AUTH_MODE=oauth로 켠다 (지금은 카카오·Apple만 구현돼 있다)
"""

import os
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

APP_URL = os.getenv("APP_URL", "http://127.0.0.1:47830").rstrip("/")
KAKAO_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_SECRET = os.getenv("KAKAO_CLIENT_SECRET", "")
KAKAO_REDIRECT_URI = os.getenv("KAKAO_REDIRECT_URI", f"{APP_URL}/auth/kakao/callback")
APPLE_CLIENT_ID = os.getenv("APPLE_SERVICES_ID") or os.getenv("APPLE_CLIENT_ID", "")
APPLE_WEB_REDIRECT_URI = os.getenv("APPLE_WEB_REDIRECT_URI") or APP_URL


class AuthError(Exception):
    pass


AUTH_MODE = os.getenv("AUTH_MODE", "mock")
MOCK_PROVIDERS = {"kakao": "카카오", "naver": "네이버", "google": "Google", "apple": "Apple"}


def providers(debug: bool) -> Dict[str, Any]:
    if AUTH_MODE == "mock":
        return {"mode": "mock", **{p: True for p in MOCK_PROVIDERS}}
    return {"mode": "oauth", "kakao": bool(KAKAO_KEY), "apple": bool(APPLE_CLIENT_ID), "naver": False, "google": False}


def new_state() -> str:
    return secrets.token_urlsafe(24)


# --- 카카오 ------------------------------------------------------------------------

def kakao_authorize_url(state: str) -> str:
    return "https://kauth.kakao.com/oauth/authorize?" + urlencode({
        "client_id": KAKAO_KEY,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })


def kakao_profile(code: str) -> Dict[str, Optional[str]]:
    data = {
        "grant_type": "authorization_code",
        "client_id": KAKAO_KEY,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "code": code,
    }
    if KAKAO_SECRET:
        data["client_secret"] = KAKAO_SECRET
    token = httpx.post("https://kauth.kakao.com/oauth/token", data=data, timeout=15)
    if token.status_code != 200:
        raise AuthError(f"카카오 토큰 발급 실패 ({token.status_code}): {token.text[:200]}")
    me = httpx.get("https://kapi.kakao.com/v2/user/me",
                   headers={"Authorization": f"Bearer {token.json()['access_token']}"}, timeout=15)
    if me.status_code != 200:
        raise AuthError(f"카카오 사용자 정보 조회 실패 ({me.status_code}): {me.text[:200]}")
    body = me.json()
    account = body.get("kakao_account") or {}
    profile = account.get("profile") or {}
    return {"uid": str(body["id"]), "email": account.get("email"), "name": profile.get("nickname")}


# --- Apple -------------------------------------------------------------------------

_apple_keys = None


def apple_profile(id_token: str, nonce: str, user_json: Optional[str]) -> Dict[str, Optional[str]]:
    """id_token 서명·발급자·대상·nonce를 확인한다. 이름은 첫 로그인 때만 user 필드로 온다."""
    import json

    import jwt

    global _apple_keys
    if _apple_keys is None:
        _apple_keys = jwt.PyJWKClient("https://appleid.apple.com/auth/keys")
    try:
        key = _apple_keys.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=APPLE_CLIENT_ID,
                            issuer="https://appleid.apple.com")
    except jwt.PyJWTError as e:
        raise AuthError(f"Apple 로그인 토큰을 확인하지 못했어요: {e}")
    if claims.get("nonce") != nonce:
        raise AuthError("Apple 로그인 요청이 맞지 않아요 (nonce).")
    name = None
    if user_json:
        try:
            n = json.loads(user_json).get("name") or {}
            name = " ".join(x for x in (n.get("lastName"), n.get("firstName")) if x) or None
        except ValueError:
            pass
    return {"uid": claims["sub"], "email": claims.get("email"), "name": name}


def mock_profile(provider: str, device: str) -> Dict[str, Any]:
    """브라우저(device)마다 다른 테스트 계정. 같은 버튼을 누른 다른 사람과 리포트가 섞이지 않게 한다."""
    label = MOCK_PROVIDERS[provider]
    return {"uid": f"mock-{provider}-{device}", "email": None, "name": f"{label} 테스트"}
