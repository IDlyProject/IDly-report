"""크롬 확장 에이전트 작업.

브라우저 조작은 사용자 크롬의 IDly 확장이 하고, 서버는 다음 행동을 AI로 정하고 상태·기록만 맡는다.
- 비밀번호·쿠키는 사용자 브라우저를 떠나지 않는다.
- 확장은 작업마다 발급한 토큰(Bearer)으로만 이 작업의 API를 부를 수 있다.
- 웹(정리 내역)에서 누른 이어서 진행·확정·취소는 명령 큐로 쌓였다가 확장이 가져간다.
"""

import re
import secrets
import uuid
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import urljoin
from action_links import is_valid

from .ai_agent import MAX_STEPS, _DESTRUCTIVE_RE, _ask
from .errors import explain

RUN_TTL_SECONDS = 60 * 60

STATUSES = {"대기", "진행 중", "입력 필요", "확인 필요", "완료", "실패", "취소됨"}


def _same_site(url: str, domain: str) -> bool:
    return is_valid(url, domain)


def start_url(domain: str) -> str:
    return f"https://{domain}"


class ExtensionRun:
    def __init__(self, owner_id: str, domain: str, action: str, service: str, account_email: str,
                 on_event: Optional[Callable[[str, str], None]] = None):
        self.created = time.monotonic()
        self.id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.owner_id = owner_id
        self.domain = domain
        self.action = action
        self.service = service
        self.account_email = account_email
        self.mode = "extension"
        self.status = "대기"
        self.step = 0
        self.message = "IDly 확장이 새 탭을 여는 중"
        self.error: Optional[str] = None
        self.commands: Deque[str] = deque()
        self._on_event = on_event or (lambda event, detail: None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "domain": self.domain,
            "action": self.action,
            "mode": self.mode,
            "status": self.status,
            "step": self.step + 1,
            "steps": MAX_STEPS,
            "message": self.message,
            "error": self.error,
            "hasScreen": False,
        }

    def launch_info(self, api_base: str) -> dict:
        """웹이 확장에 넘기는 시작 정보 (토큰 포함, 이 응답은 작업을 만든 사용자에게만 간다)."""
        return {**self.to_dict(), "token": self.token, "startUrl": start_url(self.domain), "apiBase": api_base}

    # --- 웹에서 오는 명령 ---

    def send(self, command: Dict[str, Any]) -> None:
        kind = command.get("type")
        if kind in ("resume", "confirm", "cancel"):
            self.commands.append(kind)

    # --- 확장에서 오는 요청 ---

    def take_commands(self) -> List[str]:
        out = list(self.commands)
        self.commands.clear()
        return out

    def report(self, status: str, message: str, step: int, error: Optional[str]) -> None:
        if status not in STATUSES:
            return
        if status != self.status:
            self._on_event({"입력 필요": "사용자 입력 요청", "확인 필요": "최종 확인 요청"}.get(status, status), message[:200])
        if error:
            print(f"[agent] {self.id} {self.domain} 확장 실패: {error}")
            error = explain(error, url=start_url(self.domain))
        self.status, self.message, self.step, self.error = status, message[:300], step, error

    def decide(self, api_key: str, url: str, state: Dict[str, Any], history: List[str]) -> Dict[str, Any]:
        """AI에게 다음 행동을 묻고, 안전 규칙을 서버에서 한 번 더 적용한다."""
        if not _same_site(url, self.domain):
            return {"decision": "need_login", "reason": "서비스 사이트에서 로그인 후 돌아와 이어서 진행해주세요.", "confirm": False}
        decision = _ask(api_key, self.service, self.domain, self.action, url, state, history)
        kind = decision.get("decision")
        reason = str(decision.get("reason") or "")
        self._on_event("AI 판단", f"{kind}: {reason}"[:200])
        out: Dict[str, Any] = {"decision": kind, "reason": reason, "confirm": False}
        if kind == "goto":
            target = str(decision.get("url") or "")
            if not target.startswith("http") or not _same_site(target, self.domain):
                return {"decision": "skip", "reason": f"다른 사이트 주소는 열지 않아요: {target[:80]}", "confirm": False}
            out["url"] = target
        if kind in ("click", "confirm_final"):
            index = decision.get("index")
            elements = state.get("elements") or []
            if type(index) is not int or not 0 <= index < len(elements):
                return {"decision": "skip", "reason": "AI가 없는 요소를 골랐어요. 다시 볼게요.", "confirm": False}
            el = elements[index]
            href = str(el.get("href") or "")
            if href and not _same_site(urljoin(url, href), self.domain):
                return {"decision": "skip", "reason": f"다른 사이트로 가는 링크는 누르지 않아요: {el.get('text')}", "confirm": False}
            out["index"] = index
            out["label"] = el.get("text")
            # AI가 그냥 click이라 해도, 되돌릴 수 없어 보이는 버튼이면 사용자 확정을 받게 한다
            out["confirm"] = kind == "confirm_final" or (
                bool(_DESTRUCTIVE_RE.search(str(el.get("text") or "")))
            )
            out["decision"] = "click"
        return out


_runs: Dict[str, ExtensionRun] = {}


def create(run: ExtensionRun) -> ExtensionRun:
    for run_id, existing in list(_runs.items()):
        if time.monotonic() - existing.created > RUN_TTL_SECONDS:
            _runs.pop(run_id, None)
    _runs[run.id] = run
    return run


def get(run_id: str) -> Optional[ExtensionRun]:
    return _runs.get(run_id)


def list_for(owner_id: str) -> List[ExtensionRun]:
    out = [r for r in _runs.values() if r.owner_id == owner_id]
    for run in out:
        if time.monotonic() - run.created >= RUN_TTL_SECONDS and run.status not in ("완료", "실패", "취소됨"):
            run.report("실패", "작업 유효 시간이 지났어요. 다시 시작해주세요.", run.step, None)
    return out


_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$")


def by_token(run_id: str, authorization: str) -> Optional[ExtensionRun]:
    run = _runs.get(run_id)
    m = _BEARER_RE.match(authorization or "")
    if run and time.monotonic() - run.created < RUN_TTL_SECONDS and m and secrets.compare_digest(m.group(1), run.token) and run.status not in ("완료", "실패", "취소됨"):
        return run
    return None
