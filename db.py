import os
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet

# База живёт в DATA_DIR на Amvera. Это persistent storage: контейнер можно
# пересобрать, а users.db с логинами, зашифрованными паролями и настройками
# пользователей останется на месте.
DB_PATH = Path(os.environ.get("DATA_DIR", ".")) / "users.db"
_fernet = None


def _get_fernet() -> Fernet:
    """Лениво создаёт Fernet-объект, чтобы падать только при реальной работе с секретами."""
    global _fernet
    if _fernet is None:
        key = os.environ["FERNET_KEY"]
        _fernet = Fernet(key.encode())
    return _fernet


def _enc(s: str) -> str:
    return _get_fernet().encrypt(s.encode()).decode()


def _dec(s: str) -> str:
    return _get_fernet().decrypt(s.encode()).decode()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id       INTEGER PRIMARY KEY,
            portal_login      TEXT NOT NULL,
            portal_pass_enc   TEXT NOT NULL,
            approved          INTEGER NOT NULL DEFAULT 0,
            banned            INTEGER NOT NULL DEFAULT 0,
            yandex_login      TEXT,
            yandex_pass_enc   TEXT,
            student_name      TEXT,
            attestations_json TEXT,
            reminder_minutes  INTEGER NOT NULL DEFAULT 0,
            quiet_until_date  TEXT
        )
    """)
    for col, definition in [
        ("yandex_login",      "TEXT"),
        ("yandex_pass_enc",   "TEXT"),
        ("banned",            "INTEGER NOT NULL DEFAULT 0"),
        ("student_name",      "TEXT"),
        ("attestations_json", "TEXT"),
        ("reminder_minutes",  "INTEGER NOT NULL DEFAULT 0"),
        ("quiet_until_date",  "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def add_user(telegram_id: int, login: str, password: str, student_name: str = ""):
    """Создаёт или обновляет заявку пользователя.

    Пароль портала никогда не пишется в БД открытым текстом: сохраняется только
    Fernet-токен. При повторной регистрации заявка остаётся неподтверждённой,
    чтобы владелец бота снова проверил пользователя.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO users (telegram_id, portal_login, portal_pass_enc, approved, student_name)
           VALUES (?, ?, ?, 0, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
               portal_login    = excluded.portal_login,
               portal_pass_enc = excluded.portal_pass_enc,
               student_name    = excluded.student_name""",
        (telegram_id, login, _enc(password), student_name),
    )
    conn.commit()
    conn.close()


def set_yandex(telegram_id: int, ylogin: str, ypass: str):
    """Сохраняет личные CalDAV-данные пользователя для синхронизации его календаря."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET yandex_login=?, yandex_pass_enc=? WHERE telegram_id=?",
        (ylogin, _enc(ypass), telegram_id),
    )
    conn.commit()
    conn.close()


def approve_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET approved=1 WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def ban_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET banned=1 WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def unban_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET banned=0 WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def set_student_name(telegram_id: int, student_name: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET student_name=? WHERE telegram_id=?", (student_name, telegram_id))
    conn.commit()
    conn.close()


def set_attestations(telegram_id: int, json_str: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET attestations_json=? WHERE telegram_id=?", (json_str, telegram_id))
    conn.commit()
    conn.close()


def set_reminder_minutes(telegram_id: int, minutes: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET reminder_minutes=? WHERE telegram_id=?", (minutes, telegram_id))
    conn.commit()
    conn.close()


def set_quiet_until(telegram_id: int, date_str: str | None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET quiet_until_date=? WHERE telegram_id=?", (date_str, telegram_id))
    conn.commit()
    conn.close()


def clear_yandex(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET yandex_login=NULL, yandex_pass_enc=NULL WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def remove_user(telegram_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def get_user(telegram_id: int) -> dict | None:
    """Возвращает одного пользователя с расшифрованными паролями для runtime-операций.

    Эта функция нужна боту и парсерам. Не логировать возвращаемый dict целиком:
    внутри есть пароль портала и пароль приложения Яндекса.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT portal_login, portal_pass_enc, approved, banned, yandex_login, yandex_pass_enc,
                  student_name, attestations_json, reminder_minutes, quiet_until_date
           FROM users WHERE telegram_id=?""",
        (telegram_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    login, enc, approved, banned, ylogin, yenc, sname, att_json, rem_min, quiet_date = row
    return {
        "login":             login,
        "password":          _dec(enc),
        "approved":          bool(approved),
        "banned":            bool(banned),
        "yandex_login":      ylogin,
        "yandex_pass":       _dec(yenc) if yenc else None,
        "student_name":      sname or "",
        "attestations_json": att_json,
        "reminder_minutes":  rem_min or 0,
        "quiet_until_date":  quiet_date,
    }


def get_all_users() -> list[dict]:
    """Возвращает пользователей для админки, рассылок и массовой синхронизации.

    Как и get_user(), возвращает расшифрованные секреты. Использовать только в
    trusted-коде бота, не отдавать результат наружу.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT telegram_id, portal_login, portal_pass_enc, approved, banned, yandex_login,
                  yandex_pass_enc, student_name, reminder_minutes, quiet_until_date
           FROM users"""
    ).fetchall()
    conn.close()
    return [
        {
            "telegram_id":     tid,
            "login":           login,
            "password":        _dec(enc),
            "approved":        bool(approved),
            "banned":          bool(banned),
            "yandex_login":    ylogin,
            "yandex_pass":     _dec(yenc) if yenc else None,
            "student_name":    sname or "",
            "reminder_minutes": rem_min or 0,
            "quiet_until_date": quiet_date,
        }
        for tid, login, enc, approved, banned, ylogin, yenc, sname, rem_min, quiet_date in rows
    ]
