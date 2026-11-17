# secure_config.py
"""
AES-256-GCM шифрование для токенов и конфигов.

Схема:
- secret.key — 32 байта, мастер-ключ AES-256
- secret.salt — 16 байт, соль для PBKDF2

Защита от багов:
- Ленивая инициализация (файлы создаются при первом использовании)
- O_CREAT | O_EXCL — защита от race condition между процессами
- Проверка длины key/salt при чтении
- decrypt() возвращает None при ошибке (не путается с "")
- os.chmod обёрнут в try/except (Windows игнорирует)
"""

import os
import base64
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

# ========================================================================
# КОНСТАНТЫ
# ========================================================================

SECRET_FILE = "secret.key"
SALT_FILE = "secret.salt"

KEY_SIZE = 32               # AES-256
SALT_SIZE = 16              # 128 бит
NONCE_SIZE = 12             # GCM nonce
TAG_SIZE = 16               # GCM tag
MIN_CIPHERTEXT_SIZE = NONCE_SIZE + TAG_SIZE  # 28 байт

# ========================================================================
# ЛЕНИВАЯ ИНИЦИАЛИЗАЦИЯ
# ========================================================================

_MASTER_KEY = None
_SALT = None

# ========================================================================
# СОЗДАНИЕ / ЧТЕНИЕ МАСТЕР-КЛЮЧА
# ========================================================================

def _get_or_create_master_key() -> bytes:
    """
    Возвращает 32-байтовый ключ AES-256.
    Если файла нет — создаёт атомарно через O_CREAT | O_EXCL.
    """
    # Случай 1: файл существует — читаем
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            key = f.read()
        if len(key) != KEY_SIZE:
            raise ValueError(
                f"secret.key повреждён: ожидалось {KEY_SIZE} байт, получено {len(key)}"
            )
        return key

    # Случай 2: файла нет — создаём атомарно
    try:
        # O_EXCL гарантирует, что только один процесс создаст файл
        fd = os.open(SECRET_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Другой процесс создал файл между проверкой и open
        with open(SECRET_FILE, "rb") as f:
            key = f.read()
        if len(key) != KEY_SIZE:
            raise ValueError(f"secret.key повреждён: {len(key)} байт")
        return key

    # Записываем ключ
    try:
        key = AESGCM.generate_key(bit_length=256)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except Exception:
        # При ошибке удаляем файл, чтобы не оставить мусор
        try:
            os.remove(SECRET_FILE)
        except OSError:
            pass
        raise

    # Права только для владельца (Windows игнорирует)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except (OSError, NotImplementedError):
        pass

    return key

# ========================================================================
# СОЗДАНИЕ / ЧТЕНИЕ СОЛИ
# ========================================================================

def _get_or_create_salt() -> bytes:
    """Возвращает 16-байтовую соль для PBKDF2."""
    # Случай 1: файл существует
    if os.path.exists(SALT_FILE):
        with open(SALT_FILE, "rb") as f:
            salt = f.read()
        if len(salt) != SALT_SIZE:
            raise ValueError(
                f"secret.salt повреждён: ожидалось {SALT_SIZE} байт, получено {len(salt)}"
            )
        return salt

    # Случай 2: создаём атомарно
    try:
        fd = os.open(SALT_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        with open(SALT_FILE, "rb") as f:
            salt = f.read()
        if len(salt) != SALT_SIZE:
            raise ValueError(f"secret.salt повреждён: {len(salt)} байт")
        return salt

    try:
        salt = os.urandom(SALT_SIZE)
        with os.fdopen(fd, "wb") as f:
            f.write(salt)
    except Exception:
        try:
            os.remove(SALT_FILE)
        except OSError:
            pass
        raise

    try:
        os.chmod(SALT_FILE, 0o600)
    except (OSError, NotImplementedError):
        pass

    return salt

# ========================================================================
# ПОЛУЧЕНИЕ KEY / SALT (кешируется)
# ========================================================================

def _master_key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is None:
        _MASTER_KEY = _get_or_create_master_key()
    return _MASTER_KEY

def _salt() -> bytes:
    global _SALT
    if _SALT is None:
        _SALT = _get_or_create_salt()
    return _SALT

# ========================================================================
# ШИФРОВАНИЕ AES-256-GCM
# ========================================================================

def encrypt(plaintext: str) -> str:
    """
    Шифрует строку AES-256-GCM.

    Возвращает base64url(nonce(12) + ciphertext + tag(16)).
    Пустая строка на входе → пустая строка на выходе.
    При ошибке → пустая строка (и лог).
    """
    if not plaintext:
        return ""

    try:
        aesgcm = AESGCM(_master_key())
        nonce = os.urandom(NONCE_SIZE)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    except Exception as e:
        logger.error(f"Ошибка шифрования: {e}")
        return ""

def decrypt(encrypted: str):
    """
    Расшифровывает base64url(nonce + ciphertext + tag).

    Возвращает:
      - str — при успехе
      - "" — если вход пустой
      - None — при ошибке (важно отличать от пустой строки!)

    Почему None: если расшифровка провалилась, это ошибка,
    а не "пустое значение". Вызывающий код должен обработать это отдельно.
    """
    if not encrypted:
        return ""

    # Декодируем base64
    try:
        raw = base64.urlsafe_b64decode(encrypted.encode("ascii"))
    except Exception as e:
        logger.error(f"Ошибка base64: {e}")
        return None

    # Проверка минимальной длины
    if len(raw) < MIN_CIPHERTEXT_SIZE:
        logger.error(f"decrypt: слишком короткие данные ({len(raw)} байт)")
        return None

    nonce = raw[:NONCE_SIZE]
    ct = raw[NONCE_SIZE:]

    # Расшифровка
    try:
        aesgcm = AESGCM(_master_key())
        plaintext = aesgcm.decrypt(nonce, ct, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        logger.error(f"Ошибка расшифровки: {e}")
        return None

# ========================================================================
# ХЕШИРОВАНИЕ ПАРОЛЕЙ
# ========================================================================

def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256, 100 000 итераций. Возвращает base64url-хэш."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_salt(),
        iterations=100_000,
        backend=default_backend()
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8"))).decode("ascii")

def verify_password(password: str, hashed: str) -> bool:
    """Проверяет пароль. Возвращает False при любой ошибке."""
    if not password or not hashed:
        return False
    try:
        return hash_password(password) == hashed
    except Exception:
        return False