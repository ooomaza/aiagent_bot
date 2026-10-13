# database.py
"""
SQLite-модуль.

Схема таблиц:
- users — клиенты (владельцы ботов)
- leads — лиды (диалоги с клиентами бизнеса)
- stats — статистика по дням
- notifications — очередь уведомлений от агентов

WAL-режим включён для параллельной записи из нескольких процессов.
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, List, Dict

from config import DB_PATH
from secure_config import encrypt, decrypt

logger = logging.getLogger(__name__)

# ========================================================================
# СОЕДИНЕНИЕ
# ========================================================================

@contextmanager
def get_connection():
    """Открывает соединение, коммитит при успехе, откатывает при ошибке."""
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        conn.close()

# ========================================================================
# ИНИЦИАЛИЗАЦИЯ (вызывать один раз в main())
# ========================================================================

_db_initialized = False

def init_db():
    """Создаёт таблицы и включает WAL. Идемпотентно."""
    global _db_initialized
    if _db_initialized:
        return

    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT,
                tariff TEXT DEFAULT 'trial',
                tariff_expires TEXT,
                bot_token TEXT,
                is_bot_active INTEGER DEFAULT 1,
                is_banned INTEGER DEFAULT 0,
                language_chosen INTEGER DEFAULT 0,
                language TEXT DEFAULT 'ru',
                subscribed INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_count INTEGER DEFAULT 0,
                manual_discount INTEGER DEFAULT 0,
                managers TEXT DEFAULT '[]',
                managers_stats TEXT DEFAULT '{}',
                bots TEXT DEFAULT '[]',
                support_requests TEXT DEFAULT '[]',
                ideas TEXT DEFAULT '[]',
                rag_data TEXT DEFAULT '[]',
                custom_fields TEXT DEFAULT '[]',
                lead_settings TEXT DEFAULT '{}',
                bot_settings TEXT DEFAULT '{}',
                settings TEXT DEFAULT '{}',
                schedules TEXT DEFAULT '{}',
                games TEXT DEFAULT '{}'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                lead_telegram_id INTEGER,
                lead_username TEXT,
                lead_name TEXT,
                messages TEXT,
                summary TEXT,
                score INTEGER DEFAULT 0,
                is_hot INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]',
                sentiment TEXT DEFAULT 'neutral',
                manager_id INTEGER,
                status TEXT DEFAULT 'new',
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                date TEXT,
                leads_count INTEGER DEFAULT 0,
                hot_leads_count INTEGER DEFAULT 0,
                UNIQUE(client_id, date)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_id INTEGER,
                message TEXT,
                created_at TEXT,
                sent INTEGER DEFAULT 0
            )
        """)

        conn.commit()

    _db_initialized = True
    logger.info("✅ БД инициализирована (WAL)")

# ========================================================================
# КОНСТАНТЫ ДЛЯ JSON-ПОЛЕЙ
# ========================================================================

JSON_FIELDS = [
    "managers", "managers_stats", "bots", "support_requests",
    "ideas", "rag_data", "custom_fields", "lead_settings",
    "bot_settings", "settings", "schedules", "games"
]
LIST_FIELDS = [
    "managers", "bots", "support_requests",
    "ideas", "rag_data", "custom_fields"
]
BOOL_FIELDS = ["is_bot_active", "is_banned", "language_chosen", "subscribed"]

# ========================================================================
# USERS
# ========================================================================

class UserDB:
    @staticmethod
    def _row_to_dict(row) -> Dict:
        """Строка SQLite → dict. Расшифровывает bot_token, парсит JSON."""
        data = dict(row)

        # Расшифровка токена
        if data.get("bot_token"):
            decrypted = decrypt(data["bot_token"])
            if decrypted is None:
                logger.error(f"Не удалось расшифровать bot_token для user_id={data.get('user_id')}")
                data["bot_token"] = None
            else:
                data["bot_token"] = decrypted

        # JSON-поля
        for f in JSON_FIELDS:
            raw = data.get(f)
            if raw:
                try:
                    data[f] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[f] = [] if f in LIST_FIELDS else {}
            else:
                data[f] = [] if f in LIST_FIELDS else {}

        # Булевы поля
        for f in BOOL_FIELDS:
            data[f] = bool(data.get(f, 0))

        return data

    @staticmethod
    def get(user_id: int) -> Optional[Dict]:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            return UserDB._row_to_dict(row) if row else None

    @staticmethod
    def create_default(user_id: int, username: str, first_name: str):
        """Создаёт пользователя с дефолтными настройками. Идемпотентно."""
        now = datetime.now().isoformat()

        data = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "created_at": now,
            "tariff": "trial",
            "tariff_expires": (datetime.now() + timedelta(days=2)).isoformat(),
            "bot_token": None,
            "is_bot_active": 1,
            "is_banned": 0,
            "language_chosen": 0,
            "language": "ru",
            "subscribed": 0,
            "referral_code": f"REF{user_id}{datetime.now().strftime('%y%m%d')}",
            "referred_count": 0,
            "manual_discount": 0,
            "managers": "[]",
            "managers_stats": "{}",
            "bots": "[]",
            "support_requests": "[]",
            "ideas": "[]",
            "rag_data": "[]",
            "custom_fields": "[]",
            "lead_settings": json.dumps({"budget_threshold": 0, "deadline_days": 0, "rule": "AND"}),
            "bot_settings": json.dumps({
                "script": "Ты — профессиональный менеджер по продажам.",
                "products": [],
                "features": [],
                "faq": [],
                "voice_error_text": "Извините, я не смог распознать голосовое сообщение.",
                "auto_reply_texts": ["📌 Ещё думаете? Я готов помочь!"],
                "auto_reply_timeout": 300
            }),
            "settings": json.dumps({
                "voice_enabled": True,
                "autotag_enabled": True,
                "sentiment_enabled": True,
                "crm_integration": {
                    "enabled": False, "type": "",
                    "access_token": "", "subdomain": "",
                    "webhook_url": "", "api_key": ""
                }
            }),
            "schedules": json.dumps({"enabled": False, "start_time": "09:00", "end_time": "21:00"}),
            "games": json.dumps({"level": 1, "xp": 0, "leads_handled": 0})
        }

        with get_connection() as conn:
            c = conn.cursor()
            cols = ", ".join(data.keys())
            phs = ", ".join(["?"] * len(data))
            c.execute(f"INSERT OR IGNORE INTO users ({cols}) VALUES ({phs})", list(data.values()))

    @staticmethod
    def update(user_id: int, **kwargs):
        """Обновляет поля. JSON-поля сериализуются, bot_token шифруется, bool → int."""
        if "bot_token" in kwargs and kwargs["bot_token"]:
            kwargs["bot_token"] = encrypt(kwargs["bot_token"])

        for f in JSON_FIELDS:
            if f in kwargs and not isinstance(kwargs[f], str):
                kwargs[f] = json.dumps(kwargs[f], ensure_ascii=False)

        for f in BOOL_FIELDS:
            if f in kwargs:
                kwargs[f] = int(bool(kwargs[f]))

        if not kwargs:
            return

        with get_connection() as conn:
            c = conn.cursor()
            set_clause = ", ".join([f"{k} = ?" for k in kwargs])
            values = list(kwargs.values()) + [user_id]
            c.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", values)

    @staticmethod
    def get_all() -> List[Dict]:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users ORDER BY created_at DESC")
            return [UserDB._row_to_dict(r) for r in c.fetchall()]

    @staticmethod
    def get_paginated(page: int = 0, size: int = 10) -> List[Dict]:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (size, page * size)
            )
            return [UserDB._row_to_dict(r) for r in c.fetchall()]

    @staticmethod
    def count() -> int:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users")
            return c.fetchone()[0]

    @staticmethod
    def get_expired_tariffs() -> List[Dict]:
        """Истёкшие тарифы, кроме забаненных и уже отмеченных expired."""
        now = datetime.now().isoformat()
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT * FROM users
                WHERE tariff_expires < ?
                  AND tariff NOT IN ('expired')
                  AND is_banned = 0
            """, (now,))
            return [UserDB._row_to_dict(r) for r in c.fetchall()]

# ========================================================================
# LEADS
# ========================================================================

class LeadDB:
    @staticmethod
    def create(client_id: int, lead_data: Dict) -> int:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO leads (
                    client_id, lead_telegram_id, lead_username, lead_name,
                    messages, summary, score, is_hot, tags, sentiment, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                lead_data.get("telegram_id"),
                lead_data.get("username"),
                lead_data.get("name"),
                json.dumps(lead_data.get("messages", []), ensure_ascii=False),
                lead_data.get("summary", ""),
                lead_data.get("score", 0),
                int(lead_data.get("is_hot", False)),
                json.dumps(lead_data.get("tags", []), ensure_ascii=False),
                lead_data.get("sentiment", "neutral"),
                datetime.now().isoformat()
            ))
            return c.lastrowid

    @staticmethod
    def get_by_client(client_id: int, limit: int = 50) -> List[Dict]:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM leads WHERE client_id = ? ORDER BY created_at DESC LIMIT ?",
                (client_id, limit)
            )
            return [dict(r) for r in c.fetchall()]

    @staticmethod
    def count_by_client(client_id: int) -> int:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM leads WHERE client_id = ?", (client_id,))
            return c.fetchone()[0]

    @staticmethod
    def count_hot_by_client(client_id: int) -> int:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT COUNT(*) FROM leads WHERE client_id = ? AND is_hot = 1",
                (client_id,)
            )
            return c.fetchone()[0]

    @staticmethod
    def get_global_stats():
        """(всего лидов, всего горячих) — одним SQL."""
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*), COALESCE(SUM(is_hot), 0) FROM leads")
            row = c.fetchone()
            return row[0] or 0, row[1] or 0

# ========================================================================
# STATS
# ========================================================================

class StatsDB:
    @staticmethod
    def log_lead(client_id: int, is_hot: bool = False):
        """Инкрементирует счётчики за сегодня. Если записи нет — создаёт."""
        today = datetime.now().strftime("%Y-%m-%d")
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM stats WHERE client_id = ? AND date = ?",
                (client_id, today)
            )
            row = c.fetchone()
            if row:
                c.execute("""
                    UPDATE stats
                    SET leads_count = leads_count + 1,
                        hot_leads_count = hot_leads_count + ?
                    WHERE id = ?
                """, (int(is_hot), row["id"]))
            else:
                c.execute("""
                    INSERT INTO stats (client_id, date, leads_count, hot_leads_count)
                    VALUES (?, ?, 1, ?)
                """, (client_id, today, int(is_hot)))

    @staticmethod
    def get_client_stats(client_id: int, period: str = "month") -> List[Dict]:
        days = {"day": 1, "week": 7, "month": 30}.get(period, 30)
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM stats WHERE client_id = ? AND date >= ? ORDER BY date",
                (client_id, start)
            )
            return [dict(r) for r in c.fetchall()]

# ========================================================================
# NOTIFICATIONS
# ========================================================================

class NotificationDB:
    @staticmethod
    def add(recipient_id: int, message: str):
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO notifications (recipient_id, message, created_at, sent)
                VALUES (?, ?, ?, 0)
            """, (recipient_id, message, datetime.now().isoformat()))

    @staticmethod
    def get_unsent(limit: int = 50) -> List[Dict]:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM notifications WHERE sent = 0 ORDER BY id LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in c.fetchall()]

    @staticmethod
    def mark_sent(notif_id: int):
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE notifications SET sent = 1 WHERE id = ?", (notif_id,))

    @staticmethod
    def cleanup_old(days: int = 7):
        """Удаляет старые отправленные уведомления."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM notifications WHERE created_at < ? AND sent = 1",
                (cutoff,)
            )

# ========================================================================
# ПСЕВДОНИМЫ
# ========================================================================

db_users = UserDB
db_leads = LeadDB
db_stats = StatsDB
db_notifications = NotificationDB