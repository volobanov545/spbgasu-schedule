import os
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet

DB_PATH = Path(os.environ.get("DATA_DIR", ".")) / "users.db"
_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ["FERNET_KEY"]
        _fernet = Fernet(key.encode())
    return _fernet


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id  INTEGER PRIMARY KEY,
            portal_login TEXT NOT NULL,
            portal_pass_enc TEXT NOT NULL,
            approved     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def add_user(telegram_id: int, login: str, password: str):
    f = _get_fernet()
    enc = f.encrypt(password.encode()).decode()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO users (telegram_id, portal_login, portal_pass_enc, approved) VALUES (?, ?, ?, 0)",
        (telegram_id, login, enc),
    )
    conn.commit()
    conn.close()


def approve_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET approved=1 WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def remove_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def get_user(telegram_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT portal_login, portal_pass_enc, approved FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    login, enc, approved = row
    password = _get_fernet().decrypt(enc.encode()).decode()
    return {"login": login, "password": password, "approved": bool(approved)}


def get_all_users() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT telegram_id, portal_login, portal_pass_enc, approved FROM users"
    ).fetchall()
    conn.close()
    f = _get_fernet()
    return [
        {
            "telegram_id": tid,
            "login": login,
            "password": f.decrypt(enc.encode()).decode(),
            "approved": bool(approved),
        }
        for tid, login, enc, approved in rows
    ]
