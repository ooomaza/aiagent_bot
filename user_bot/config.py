# config.py
"""
Конфигурация проекта.

Особенности:
- Токены читаются из .env
- При первом чтении шифруются в .env.encrypted
- Если .env.encrypted существует — читаются оттуда
- Перезапись (не append) — не растёт бесконечно
"""

import os
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

ENCRYPTED_STORE = ".env.encrypted"

# ========================================================================
# ЧТЕНИЕ / ЗАПИСЬ ЗАШИФРОВАННОГО ХРАНИЛИЩА
# ========================================================================

def _read_store() -> dict:
    """Читает .env.encrypted в словарь {key: value}."""
    if not os.path.exists(ENCRYPTED_STORE):
        return {}
    result = {}
    try:
        with open(ENCRYPTED_STORE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
    except Exception as e:
        logger.error(f"Ошибка чтения {ENCRYPTED_STORE}: {e}")
    return result

def _write_store(store: dict):
    """Перезаписывает .env.encrypted целиком (не append)."""
    try:
        with open(ENCRYPTED_STORE, "w", encoding="utf-8") as f:
            for k, v in store.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        logger.error(f"Ошибка записи {ENCRYPTED_STORE}: {e}")

# ========================================================================
# ЗАГРУЗКА С АВТО-ШИФРОВАНИЕМ
# ========================================================================

def _load_env(key: str, default: str = "") -> str:
    """
    Логика:
    1. Пробуем найти в .env.encrypted (расшифрованное)
    2. Если нет — берём из .env
    3. Если взяли из .env — шифруем и сохраняем в .env.encrypted
    """
    store = _read_store()

    # 1. Пробуем зашифрованное хранилище
    if key in store and store[key].startswith("enc:"):
        try:
            from secure_config import decrypt
            decrypted = decrypt(store[key][4:])
            if decrypted is not None and decrypted:
                return decrypted
            logger.warning(f"{key}: расшифровка вернула пусто, fallback на .env")
        except Exception as e:
            logger.error(f"{key}: ошибка расшифровки: {e}")

    # 2. Берём из .env
    value = os.getenv(key, default)
    if not value or value == default:
        return value

    # 3. Шифруем и сохраняем
    try:
        from secure_config import encrypt
        encrypted = encrypt(value)
        if encrypted:
            store[key] = f"enc:{encrypted}"
            _write_store(store)
    except Exception as e:
        logger.error(f"{key}: ошибка шифрования: {e}")

    return value

# ========================================================================
# ТОКЕНЫ
# ========================================================================

MAIN_BOT_TOKEN = _load_env("MAIN_BOT_TOKEN", "YOUR_MAIN_BOT_TOKEN")
ADMIN_BOT_TOKEN = _load_env("ADMIN_BOT_TOKEN", "YOUR_ADMIN_BOT_TOKEN")
DEEPSEEK_API_KEY = _load_env("DEEPSEEK_API_KEY", "YOUR_DEEPSEEK_API_KEY")

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# ========================================================================
# АДМИНЫ
# ========================================================================

ADMIN_IDS = [123456789]  # ЗАМЕНИ на свой Telegram ID

# ========================================================================
# КАНАЛ (проверка подписки)
# ========================================================================

CHANNEL_URL = ""  # Пусто = проверка отключена
# Пример: "https://t.me/your_channel"

def channel_is_configured() -> bool:
    return bool(CHANNEL_URL) and "your_channel" not in CHANNEL_URL

# ========================================================================
# БАЗА ДАННЫХ
# ========================================================================

DB_PATH = "fox_bot.db"

# ========================================================================
# ТАРИФЫ
# ========================================================================

TARIFFS = {
    "trial":     {"name": "Пробный",  "price": 0,     "leads": 10,  "days": 2,  "is_trial": True},
    "basic":     {"name": "Базовый",  "price": 1500,  "leads": 30,  "days": 30, "is_trial": False},
    "pro":       {"name": "Про",      "price": 4500,  "leads": 100, "days": 30, "is_trial": False},
    "unlimited": {"name": "Безлимит", "price": 12000, "leads": "∞", "days": 30, "is_trial": False}
}

PAID_TARIFFS = ["basic", "pro", "unlimited"]

# ========================================================================
# ЛИМИТЫ МУЛЬТИ-БОТ
# ========================================================================

BOT_LIMITS = {
    "trial": 1,
    "basic": 1,
    "pro": 3,
    "unlimited": 10
}

# ========================================================================
# ЯЗЫКИ
# ========================================================================

LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "es": "🇪🇸 Español",
    "ar": "🇸🇦 العربية",
    "zh": "🇨🇳 中文"
}

# ========================================================================
# ПРОВЕРКА КОНФИГА
# ========================================================================

def is_configured() -> bool:
    """Проверяет, что все обязательные токены заданы."""
    return (
        bool(MAIN_BOT_TOKEN) and MAIN_BOT_TOKEN != "YOUR_MAIN_BOT_TOKEN"
        and bool(ADMIN_BOT_TOKEN) and ADMIN_BOT_TOKEN != "YOUR_ADMIN_BOT_TOKEN"
    )

def is_deepseek_configured() -> bool:
    return bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "YOUR_DEEPSEEK_API_KEY"