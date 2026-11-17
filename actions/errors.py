"""에이전트 실패 이유를 사용자가 알아들을 수 있는 말로 바꾼다.

Playwright·크롬 확장이 내는 원문 오류(영어, 기술 용어)를 원인과 할 일로 옮긴다.
원문은 서버 로그에 남기고, 화면에는 옮긴 문장만 보여준다.
"""

import re
from typing import Optional
from urllib.parse import urlparse

# (원문에 들어 있는 조각, 사용자에게 보여줄 설명). 위에서부터 먼저 맞는 것을 쓴다
_RULES = [
    ("Executable doesn't exist", "서버에 에이전트용 브라우저가 아직 준비되지 않았어요. 잠시 뒤 다시 시도해 주세요."),
    ("ERR_NAME_NOT_RESOLVED", "사이트 주소를 찾지 못했어요. 서비스 주소가 바뀌었거나 없어진 곳일 수 있어요."),
    ("ERR_CONNECTION_REFUSED", "사이트가 접속을 거부했어요."),
    ("ERR_CONNECTION_RESET", "사이트가 연결을 끊었어요. 해외 서버나 자동 접속을 막는 사이트일 수 있어요."),
    ("ERR_CONNECTION_CLOSED", "사이트가 연결을 끊었어요. 해외 서버나 자동 접속을 막는 사이트일 수 있어요."),
    ("ERR_TIMED_OUT", "사이트가 응답하지 않았어요. 해외 서버 접속을 막거나 사이트가 느린 상태일 수 있어요."),
    ("ERR_CERT", "사이트 보안 인증서에 문제가 있어 열지 않았어요."),
    ("ERR_TOO_MANY_REDIRECTS", "사이트가 페이지를 계속 다른 곳으로 넘겨서 열 수 없었어요."),
    ("ERR_INTERNET_DISCONNECTED", "인터넷 연결이 끊겼어요."),
    ("Target page, context or browser has been closed", "작업 중에 브라우저가 닫혔어요. 서버 메모리가 부족했을 수 있어요."),
    ("Target closed", "작업 중에 브라우저가 닫혔어요. 서버 메모리가 부족했을 수 있어요."),
    ("Browser closed", "작업 중에 브라우저가 닫혔어요. 서버 메모리가 부족했을 수 있어요."),
    ("Cannot access contents of", "크롬이 이 페이지에서는 확장 프로그램 동작을 막아서 진행할 수 없었어요."),
    ("Cannot access a chrome", "크롬 설정 페이지에서는 확장 프로그램이 동작할 수 없어요."),
    ("No tab with id", "작업하던 탭을 찾지 못했어요. 탭이 닫혔을 수 있어요."),
    ("Failed to fetch", "IDly 서버에 연결하지 못했어요. 서버가 깨어나는 중일 수 있으니 잠시 뒤 다시 시도해 주세요."),
]


def _host(url: Optional[str]) -> str:
    return (urlparse(url).hostname or "") if url else ""


def explain(raw: str, step: Optional[str] = None, url: Optional[str] = None) -> str:
    """원문 오류를 '어디서 + 왜' 한두 문장으로. 이미 한국어로 쓴 오류(우리 코드가 낸 것)는 그대로 둔다."""
    raw = (raw or "").strip()
    first = raw.splitlines()[0] if raw else ""
    where = f"'{step}' 단계에서 " if step else ""
    host = _host(url)

    if re.search(r"[가-힣]", first):
        return f"{where}{first}"[:300]

    for needle, text in _RULES:
        if needle in raw:
            return f"{where}{text}"[:300]

    if "Timeout" in raw:
        if "goto" in raw or "navigat" in raw.lower():
            site = f"{host} " if host else "사이트 "
            return (f"{where}{site}페이지가 30초 안에 열리지 않았어요. "
                    "사이트가 해외 서버·자동 접속을 막거나 너무 느린 상태일 수 있어요.")[:300]
        return f"{where}화면에서 필요한 버튼이나 입력칸을 제때 찾지 못했어요. 사이트 화면이 예상과 달라요."[:300]

    return f"{where}예상하지 못한 오류가 났어요 ({first[:120]})"[:300]
