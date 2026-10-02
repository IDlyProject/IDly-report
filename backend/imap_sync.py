"""IMAP으로 메일 헤더를 받아 계정 탐색(analysis.py)까지 이어서 돌린다.

- 받은편지함뿐 아니라 보관·사용자 폴더까지 본다. 스팸·휴지통·보낸편지함·임시보관함은 뺀다.
  Gmail처럼 전체보관함(\\All)이 있으면 그것 하나만 본다 (다른 폴더와 중복).
- 개수 제한 없이 헤더(보낸 곳·제목·날짜)만 받는다. 본문은 분석 중 필요한 결제 메일만 따로 받는다.
- 메일은 디스크에 저장하지 않는다. 헤더는 작업이 끝날 때까지 메모리에만 둔다.
"""

import base64
import email
import imaplib
import re
import queue
import threading
import traceback
import uuid
from email.message import Message
from email.parser import BytesHeaderParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from analysis import HeaderRecord, discover_accounts, header_record

HEADER_BATCH = 500
# 본문을 동시에 읽을 메일 서버 연결 수 (메일 서비스가 동시 연결을 막지 않는 선에서)
BODY_WORKERS = 4
# 한 폴더에서 최근 몇 통까지 볼지 (안전 상한)
MAX_PER_FOLDER = 100_000
# 본문은 통째로 받는다 (앞부분만 받으면 MIME 구조가 잘려 금액을 못 읽는다). 이보다 크면 건너뛴다
MAX_BODY_BYTES = 5_000_000
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')
_UID_RE = re.compile(rb"UID (\d+)")
_SKIP_FLAGS = (b"\\Noselect", b"\\NonExistent", b"\\Junk", b"\\Trash", b"\\Sent", b"\\Drafts")
_SKIP_NAME_RE = re.compile(r"\b(spam|junk|trash|deleted|bin|sent|drafts?|outbox)\b|스팸|휴지통|보낸|임시|내게\s*쓴", re.I)


class Credential:
    """메모리에만 둔다. 파일로 저장하지 않는다."""

    def __init__(self, email: str, host: str, port: int, username: str, password: str):
        self.email = email
        self.host = host
        self.port = port
        self.username = username
        self.password = password


def connect(cred: Credential) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cred.host, cred.port, timeout=60)
    conn.login(cred.username, cred.password)
    return conn


def decode_folder_name(raw: str) -> str:
    """IMAP 폴더 이름(modified UTF-7, 예: &vBTHfA-)을 사람이 읽을 수 있게 바꾼다."""
    name = raw.strip('"')

    def repl(m: "re.Match[str]") -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except Exception:
            return m.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def pick_folders(conn: imaplib.IMAP4_SSL) -> List[str]:
    """탐색할 폴더의 원래 이름 목록."""
    typ, lines = conn.list()
    if typ != "OK":
        return ["INBOX"]
    folders: List[str] = []
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue
        flags, raw = m.group("flags"), m.group("name").decode()
        if b"\\All" in flags:
            return [raw]
        if any(f in flags for f in _SKIP_FLAGS):
            continue
        if raw.strip('"').upper() != "INBOX" and _SKIP_NAME_RE.search(decode_folder_name(raw)):
            continue
        folders.append(raw)
    return folders or ["INBOX"]


def _select(conn: imaplib.IMAP4_SSL, folder: str) -> bool:
    quoted = folder if folder.startswith('"') else f'"{folder}"'
    typ, _ = conn.select(quoted, readonly=True)
    return typ == "OK"


def _fetch_parts(data: List[Any]) -> List[Tuple[Optional[bytes], bytes]]:
    """FETCH 응답에서 (UID, 리터럴) 쌍을 뽑는다. UID가 리터럴 뒤에 오는 서버도 있다."""
    out: List[Tuple[Optional[bytes], bytes]] = []
    for i, part in enumerate(data):
        if not isinstance(part, tuple):
            continue
        m = _UID_RE.search(part[0])
        if not m and i + 1 < len(data) and isinstance(data[i + 1], bytes):
            m = _UID_RE.search(data[i + 1])
        out.append((m.group(1) if m else None, part[1]))
    return out


class SyncJob:
    def __init__(self, cred: Credential, limit: int, owner_id: int,
                 on_done: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.id = uuid.uuid4().hex
        self.owner_id = owner_id
        self._on_done = on_done
        self.email = cred.email
        self.limit = limit
        self.status = "대기"
        self.folders: List[str] = []
        self.total = 0
        self.fetched = 0
        self.error: Optional[str] = None
        self.trace: Optional[str] = None  # 실패했을 때 스택 (디버그용)
        self.failed_step: Optional[str] = None
        # 분석 단계 이름 (헤더 분석 / 결제 메일 확인 / AI 판별)
        self.step: Optional[str] = None
        self.report: Optional[Dict[str, Any]] = None
        # 단계 안 진행률 (결제 메일 확인 34/120, AI 판별 2/6)
        self.step_done: Optional[int] = None
        self.step_total: Optional[int] = None
        self._cred: Optional[Credential] = cred
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._selected: Optional[str] = None
        # 본문용 연결 묶음: (연결, 선택된 폴더). 필요할 때 BODY_WORKERS개까지 연다
        self._pool: "queue.Queue[list]" = queue.Queue()
        self._pool_size = 0
        self._pool_lock = threading.Lock()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "step": self.step,
            "step_done": self.step_done,
            "step_total": self.step_total,
            "folders": [decode_folder_name(f) for f in self.folders],
            "total": self.total,
            "fetched": self.fetched,
            "accounts_found": len(self.report["accounts"]) if self.report else None,
            "error": self.error,
            "debug": {"failed_at": self.failed_step, "traceback": self.trace} if self.trace else None,
        }

    def run(self) -> None:
        self.status = "받는 중"
        try:
            self._conn = connect(self._cred)
            try:
                records = self._fetch_headers()
                self.status = "분석 중"
                self.report = discover_accounts(records, self.email, self._fetch_body, on_progress=self._set_step)
            finally:
                # 분석이 끝나면 비밀번호를 버리고 모든 연결을 닫는다
                self._cred = None
                for conn in [self._conn] + [c for c, _ in self._drain_pool()]:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            if self._on_done:
                self._on_done(self.report)
            self.status = "완료"
        except Exception as e:  # 진행 상태로 화면에 보여준다
            self.failed_step = f"{self.status} / {self.step or '-'} ({self.fetched}/{self.total})"
            self.status = "실패"
            self.error = str(e)
            self.trace = traceback.format_exc()
            print(f"[sync] {self.email} failed at {self.failed_step}\n{self.trace}")

    def _fetch_headers(self) -> List[HeaderRecord]:
        conn = self._conn
        assert conn is not None
        self.folders = pick_folders(conn)

        # 폴더별 UID를 먼저 모아 전체 개수를 알린다
        plan: List[Tuple[str, List[bytes]]] = []
        for folder in self.folders:
            if not _select(conn, folder):
                continue
            typ, data = conn.uid("SEARCH", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                continue
            uids = data[0].split()[-min(self.limit, MAX_PER_FOLDER):]
            plan.append((folder, uids))
        self.total = sum(len(u) for _, u in plan)

        parser = BytesHeaderParser()
        records: List[HeaderRecord] = []
        seen_ids = set()
        for folder, uids in plan:
            _select(conn, folder)
            self._selected = folder
            for i in range(0, len(uids), HEADER_BATCH):
                chunk = b",".join(uids[i:i + HEADER_BATCH])
                typ, data = conn.uid("FETCH", chunk, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                if typ != "OK":
                    raise RuntimeError("메일 목록을 가져오지 못했습니다.")
                for uid, raw in _fetch_parts(data):
                    self.fetched += 1
                    headers = parser.parsebytes(raw)
                    message_id = (headers.get("Message-ID") or "").strip()
                    # 여러 폴더에 같은 메일이 있으면 한 번만 센다
                    if message_id:
                        if message_id in seen_ids:
                            continue
                        seen_ids.add(message_id)
                    if uid is not None:
                        records.append(header_record((folder, uid.decode()), headers))
        return records

    def _drain_pool(self) -> List[list]:
        items = []
        while True:
            try:
                items.append(self._pool.get_nowait())
            except queue.Empty:
                return items

    def _borrow(self) -> Optional[list]:
        """본문용 연결 하나를 빌린다. 모자라면 BODY_WORKERS개까지 새로 연다."""
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._pool_lock:
            if self._pool_size < BODY_WORKERS and self._cred is not None:
                self._pool_size += 1
                try:
                    return [connect(self._cred), None]
                except Exception:
                    self._pool_size -= 1
                    if self._pool_size == 0:
                        raise
        return self._pool.get()

    def _fetch_body(self, ref: Tuple[str, str]) -> Optional[Message]:
        """분석 중 필요한 메일만 본문을 받는다. 여러 스레드에서 동시에 불러도 된다 (연결마다 따로 쓴다)."""
        slot = self._borrow()
        if slot is None:
            return None
        try:
            return self._fetch_with(slot, ref)
        finally:
            self._pool.put(slot)

    def _fetch_with(self, slot: list, ref: Tuple[str, str]) -> Optional[Message]:
        conn = slot[0]
        folder, uid = ref
        if slot[1] != folder:
            if not _select(conn, folder):
                return None
            slot[1] = folder
        typ, data = conn.uid("FETCH", uid, "(RFC822.SIZE)")
        size = _SIZE_RE.search(data[0]) if typ == "OK" and data and isinstance(data[0], bytes) else None
        if size and int(size.group(1)) > MAX_BODY_BYTES:
            return None
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            return None
        parts = _fetch_parts(data)
        return email.message_from_bytes(parts[0][1]) if parts else None

    def _set_step(self, step: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
        self.step, self.step_done, self.step_total = step, done, total


_jobs: Dict[str, SyncJob] = {}


def start_sync(cred: Credential, limit: int, owner_id: int,
               on_done: Optional[Callable[[Dict[str, Any]], None]] = None) -> SyncJob:
    job = SyncJob(cred, limit, owner_id, on_done)
    _jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job


def get_job(job_id: str) -> Optional[SyncJob]:
    return _jobs.get(job_id)
