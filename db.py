"""저장소 (SQLite / PostgreSQL). 사용자·로그인 세션·리포트·결제.

- 메일 원문과 메일 비밀번호는 저장하지 않는다. 리포트에는 분석 결과(계정 목록)만 있다.
- 회원 탈퇴하면 그 사용자의 리포트·결제 기록·세션을 모두 지운다 (결제사 거래 기록은 결제사가 보관).
"""

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 테스트 단계: 로컬은 기본이 메모리 DB (디스크에 남기지 않고, 서버를 다시 켜면 비워진다).
# 파일에 남기려면 IDLY_DB에 경로를 준다. DATABASE_URL(PostgreSQL)이 있으면 그쪽을 쓴다
DB_PATH = os.getenv("IDLY_DB", ":memory:")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
POSTGRES_SCHEMA = "idly_report"
SESSION_DAYS = 30

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,          -- kakao | apple | dev
    provider_uid TEXT NOT NULL,
    email TEXT,
    name TEXT,
    created TEXT NOT NULL,
    terms_at TEXT,                   -- 필수 약관 동의 시각
    marketing INTEGER NOT NULL DEFAULT 0,
    UNIQUE (provider, provider_uid)
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    mailbox TEXT NOT NULL,
    created TEXT NOT NULL,
    data TEXT,                        -- 분석 결과 JSON. 없으면 분석 중이었거나 끊긴 것
    paid INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    order_id TEXT,
    guest TEXT                        -- 로그인 없이 만든 리포트: 브라우저에 저장한 게스트 토큰의 해시 (user_id는 비어 있다)
);
CREATE TABLE IF NOT EXISTS payments (
    order_id TEXT PRIMARY KEY,
    report_id TEXT,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL,             -- paid | refunded
    amount INTEGER,
    currency TEXT,
    event TEXT,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_user ON reports(user_id, created);
-- 열린 메일함: 결제한 메일함. 한 번 열리면 다시 분석해도 열려 있다
CREATE TABLE IF NOT EXISTS unlocks (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mailbox TEXT NOT NULL,
    how TEXT NOT NULL,                -- paid (예전 무료 정책의 free 행은 열린 것으로 보지 않는다)
    order_id TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (user_id, mailbox)
);
-- 내부 통계용 선택 정보. 답하지 않은 칸은 비워 둔다. 건너뛰어도 행을 남겨 다시 묻지 않는다. 탈퇴하면 같이 지워진다
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    age TEXT,                         -- 연령대 (10대 … 60대 이상)
    gender TEXT,
    job TEXT,
    updated TEXT NOT NULL
);
"""


def _initialize(conn) -> None:
    if USE_POSTGRES:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA}")
        conn.execute(f"SET search_path TO {POSTGRES_SCHEMA}")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
    else:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
    # 예전 DB에 게스트 열 추가
    if USE_POSTGRES:
        conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS guest TEXT")
    elif "guest" not in [r[1] for r in conn.execute("PRAGMA table_info(reports)").fetchall()]:
        conn.execute("ALTER TABLE reports ADD COLUMN guest TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS reports_guest ON reports(guest, created)")


def conn():
    global _conn
    if _conn is None:
        if os.getenv("VERCEL") and not USE_POSTGRES:
            raise RuntimeError("Vercel requires a PostgreSQL DATABASE_URL; configure the Supabase connection string.")
        if DATABASE_URL and not USE_POSTGRES:
            raise RuntimeError("DATABASE_URL must be a PostgreSQL connection string.")
        if USE_POSTGRES:
            import psycopg
            from psycopg.rows import dict_row

            _conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10)
        else:
            if DB_PATH != ":memory:":
                Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
        _initialize(_conn)
    return _conn


def _sql(query: str) -> str:
    return query.replace("?", "%s") if USE_POSTGRES else query


def _execute(sql: str, args: tuple = ()):
    global _conn
    for attempt in (1, 2):
        try:
            return conn().execute(_sql(sql), args)
        except Exception as error:
            if not USE_POSTGRES or attempt == 2 or type(error).__name__ not in ("OperationalError", "InterfaceError"):
                raise
            _conn = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _exec(sql: str, args: tuple = ()):
    with _lock:
        cur = _execute(sql, args)
        if not USE_POSTGRES:
            conn().commit()
        return cur


def _one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _execute(sql, args).fetchall()]


# --- 사용자·세션 -----------------------------------------------------------------

def upsert_user(provider: str, provider_uid: str, email: Optional[str], name: Optional[str]) -> Dict[str, Any]:
    user = _one("SELECT * FROM users WHERE provider = ? AND provider_uid = ?", (provider, provider_uid))
    if user:
        # Apple은 이메일·이름을 첫 로그인 때만 준다. 이미 있으면 덮어쓰지 않는다
        _exec("UPDATE users SET email = COALESCE(?, email), name = COALESCE(?, name) WHERE id = ?",
              (email, name, user["id"]))
        return get_user(user["id"])
    user_id = "u_" + secrets.token_urlsafe(12)
    _exec("INSERT INTO users (id, provider, provider_uid, email, name, created) VALUES (?, ?, ?, ?, ?, ?)",
          (user_id, provider, provider_uid, email, name, _now()))
    return get_user(user_id)


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def agree_terms(user_id: str, marketing: bool) -> None:
    _exec("UPDATE users SET terms_at = ?, marketing = ? WHERE id = ?", (_now(), int(marketing), user_id))


def delete_user(user_id: str) -> None:
    _exec("DELETE FROM users WHERE id = ?", (user_id,))


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _exec("INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
          (token, user_id, (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def session_user(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    row = _one("SELECT u.* , s.expires FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token,))
    if not row or row["expires"] < _now():
        return None
    return row


def delete_session(token: str) -> None:
    _exec("DELETE FROM sessions WHERE token = ?", (token,))


# --- 선택 정보 (내부 통계) ---------------------------------------------------------

def get_profile(user_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT age, gender, job FROM profiles WHERE user_id = ?", (user_id,))


def save_profile(user_id: str, age: Optional[str], gender: Optional[str], job: Optional[str]) -> None:
    _exec("""INSERT INTO profiles (user_id, age, gender, job, updated) VALUES (?, ?, ?, ?, ?)
             ON CONFLICT(user_id) DO UPDATE SET age = excluded.age, gender = excluded.gender,
             job = excluded.job, updated = excluded.updated""",
          (user_id, age, gender, job, _now()))


# --- 리포트 -----------------------------------------------------------------------

def save_report(report_id: str, user_id: Optional[str], mailbox: str, created: datetime,
                data: Optional[Dict[str, Any]], paid: bool, guest: Optional[str] = None) -> None:
    _exec(
        """INSERT INTO reports (id, user_id, mailbox, created, data, paid, guest) VALUES (?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(id) DO UPDATE SET data = excluded.data,
              paid = CASE WHEN reports.paid > excluded.paid THEN reports.paid ELSE excluded.paid END""",
        (report_id, user_id, mailbox, created.isoformat(timespec="seconds"),
         json.dumps(data, ensure_ascii=False) if data is not None else None, int(paid), guest),
    )


def load_reports() -> List[Dict[str, Any]]:
    rows = _all("SELECT * FROM reports")
    for r in rows:
        r["data"] = json.loads(r["data"]) if r["data"] else None
    return rows


def user_reports(user_id: str) -> List[Dict[str, Any]]:
    return _all("SELECT id FROM reports WHERE user_id = ? ORDER BY created DESC", (user_id,))


def guest_reports(guest: str) -> List[Dict[str, Any]]:
    return _all("SELECT id FROM reports WHERE guest = ? ORDER BY created DESC", (guest,))


def claim_guest_reports(guest: str, user_id: str) -> List[str]:
    """로그인 없이 만든 리포트를 로그인한 계정으로 옮긴다. 옮긴 id를 돌려준다."""
    ids = [r["id"] for r in guest_reports(guest)]
    # 게스트로 결제한 메일함은 계정에서도 열린 메일함으로 (다시 분석해도 추가 결제 없이)
    for r in _all("SELECT mailbox, order_id FROM reports WHERE guest = ? AND paid = 1", (guest,)):
        _exec("INSERT INTO unlocks (user_id, mailbox, how, order_id, created) VALUES (?, ?, 'paid', ?, ?) "
              "ON CONFLICT(user_id, mailbox) DO NOTHING", (user_id, r["mailbox"].lower(), r["order_id"], _now()))
    _exec("UPDATE reports SET user_id = ?, guest = NULL WHERE guest = ?", (user_id, guest))
    return ids


def delete_report(report_id: str) -> None:
    _exec("DELETE FROM reports WHERE id = ?", (report_id,))


def delete_reports_before(cutoff: datetime, paid: bool) -> List[str]:
    """보관 기간이 지난 리포트를 지우고 지운 id를 돌려준다."""
    rows = _all("SELECT id FROM reports WHERE created < ? AND paid = ?", (cutoff.isoformat(timespec="seconds"), int(paid)))
    for r in rows:
        delete_report(r["id"])
    return [r["id"] for r in rows]


# --- 열린 메일함 -------------------------------------------------------------------

def unlock_of(user_id: str, mailbox: str) -> Optional[str]:
    """이 메일함을 결제했으면 'paid'."""
    row = _one("SELECT how FROM unlocks WHERE user_id = ? AND mailbox = ?", (user_id, mailbox.lower()))
    return row["how"] if row else None


# --- 결제 -------------------------------------------------------------------------

def record_payment(order_id: str, report_id: str, user_id: Optional[str], status: str,
                   amount: Optional[int], currency: Optional[str], event: str) -> bool:
    """결제사 웹훅을 기록한다. 같은 주문의 같은 상태가 다시 오면(재전송) False."""
    existing = _one("SELECT status FROM payments WHERE order_id = ?", (order_id,))
    if existing and existing["status"] == status:
        return False
    _exec(
        """INSERT INTO payments (order_id, report_id, user_id, status, amount, currency, event, created)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(order_id) DO UPDATE SET status = excluded.status, event = excluded.event""",
        (order_id, report_id, user_id, status, amount, currency, event, _now()),
    )
    paid = status == "paid"
    report = _one("SELECT user_id, mailbox FROM reports WHERE id = ?", (report_id,))
    if report and report["user_id"]:
        # 결제는 메일함 단위: 결제하면 그 메일함의 리포트가 모두 열리고, 환불하면 모두 다시 잠긴다
        if paid:
            _exec("INSERT INTO unlocks (user_id, mailbox, how, order_id, created) VALUES (?, ?, 'paid', ?, ?) "
                "ON CONFLICT(user_id, mailbox) DO UPDATE SET how = excluded.how, "
                "order_id = excluded.order_id, created = excluded.created",
                  (report["user_id"], report["mailbox"].lower(), order_id, _now()))
        else:
            _exec("DELETE FROM unlocks WHERE user_id = ? AND mailbox = ? AND order_id = ?",
                  (report["user_id"], report["mailbox"].lower(), order_id))
        _exec("UPDATE reports SET paid = ?, paid_at = ?, order_id = ? WHERE user_id = ? AND lower(mailbox) = ?",
              (int(paid), _now() if paid else None, order_id, report["user_id"], report["mailbox"].lower()))
    elif report:
        # 로그인 없이 만든 리포트: 결제한 그 리포트만 연다 (메일함 단위로 묶을 계정이 없다)
        _exec("UPDATE reports SET paid = ?, paid_at = ?, order_id = ? WHERE id = ?",
              (int(paid), _now() if paid else None, order_id, report_id))
    return True
