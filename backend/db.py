"""저장소 (SQLite). 사용자·로그인 세션·리포트·결제.

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

DB_PATH = Path(os.getenv("IDLY_DB", Path(__file__).parent / "data" / "idly.db"))
SESSION_DAYS = 30

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

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
    order_id TEXT
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
-- 열린 메일함: 계정마다 첫 메일함 1개는 무료, 그다음은 결제한 메일함. 한 번 열리면 다시 분석해도 열려 있다
CREATE TABLE IF NOT EXISTS unlocks (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mailbox TEXT NOT NULL,
    how TEXT NOT NULL,                -- free | paid
    order_id TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (user_id, mailbox)
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
    return _conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


def _one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    with _lock:
        row = conn().execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


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


# --- 리포트 -----------------------------------------------------------------------

def save_report(report_id: str, user_id: Optional[str], mailbox: str, created: datetime,
                data: Optional[Dict[str, Any]], paid: bool) -> None:
    _exec(
        """INSERT INTO reports (id, user_id, mailbox, created, data, paid) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET data = excluded.data, paid = MAX(reports.paid, excluded.paid)""",
        (report_id, user_id, mailbox, created.isoformat(timespec="seconds"),
         json.dumps(data, ensure_ascii=False) if data is not None else None, int(paid)),
    )


def load_reports() -> List[Dict[str, Any]]:
    rows = _all("SELECT * FROM reports")
    for r in rows:
        r["data"] = json.loads(r["data"]) if r["data"] else None
    return rows


def user_reports(user_id: str) -> List[Dict[str, Any]]:
    return _all("SELECT id FROM reports WHERE user_id = ? ORDER BY created DESC", (user_id,))


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
    """이 메일함이 열려 있으면 'free' 또는 'paid'."""
    row = _one("SELECT how FROM unlocks WHERE user_id = ? AND mailbox = ?", (user_id, mailbox.lower()))
    return row["how"] if row else None


def claim_free(user_id: str, mailbox: str) -> bool:
    """아직 무료 메일함을 쓰지 않았으면 이 메일함을 무료로 연다. 리포트를 지워도 무료는 다시 생기지 않는다."""
    if _one("SELECT 1 FROM unlocks WHERE user_id = ? AND how = 'free'", (user_id,)):
        return False
    _exec("INSERT OR IGNORE INTO unlocks (user_id, mailbox, how, created) VALUES (?, ?, 'free', ?)",
          (user_id, mailbox.lower(), _now()))
    return True


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
            _exec("INSERT OR REPLACE INTO unlocks (user_id, mailbox, how, order_id, created) VALUES (?, ?, 'paid', ?, ?)",
                  (report["user_id"], report["mailbox"].lower(), order_id, _now()))
        else:
            _exec("DELETE FROM unlocks WHERE user_id = ? AND mailbox = ? AND order_id = ?",
                  (report["user_id"], report["mailbox"].lower(), order_id))
        _exec("UPDATE reports SET paid = ?, paid_at = ?, order_id = ? WHERE user_id = ? AND lower(mailbox) = ?",
              (int(paid), _now() if paid else None, order_id, report["user_id"], report["mailbox"].lower()))
    return True
