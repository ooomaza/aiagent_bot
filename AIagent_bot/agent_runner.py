# agent_runner.py
"""
Управление процессами AI-агентов.

Функции:
- validate_bot_token(token) — async, проверка токена через getMe
- kill_existing_agent(client_id) — убить процесс агента по PID-файлу
- activate_client_bot(client_id, token) — запустить агента через subprocess
- restore_agents() — поднять всех активных агентов при старте
- is_agent_alive(client_id) — проверка, жив ли процесс агента

Безопасность:
- Токен передаётся через env AGENT_TOKEN, а не argv (чтобы не светился в ps).
"""

import os
import sys
import logging
import subprocess

import aiohttp

from database import db_users

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ========================================================================
# ВАЛИДАЦИЯ ТОКЕНА
# ========================================================================

async def validate_bot_token(token: str) -> tuple[bool, str]:
    """Проверяет токен бота через Telegram getMe."""
    if not token or ":" not in token:
        return False, "токен пустой или не содержит ':'"

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(url) as r:
                try:
                    data = await r.json()
                except Exception:
                    return False, f"Telegram вернул не-JSON (HTTP {r.status})"

                if data.get("ok"):
                    username = data.get("result", {}).get("username", "")
                    return True, f"@{username}" if username else "ok"

                return False, data.get("description", f"HTTP {r.status}")
    except aiohttp.ClientError as e:
        return False, f"сетевая ошибка: {e}"
    except Exception as e:
        return False, f"непредвиденная ошибка: {e}"

# ========================================================================
# PID-ФАЙЛЫ И ПРОЦЕССЫ
# ========================================================================

def _pid_file(client_id: int) -> str:
    return os.path.join(BASE_DIR, f"agent_{client_id}.pid")

def _pid_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс по PID. Кроссплатформенно."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            # Если процесса нет, Windows пишет "No tasks are running..."
            return ("No tasks are running" not in result.stdout
                    and str(pid) in result.stdout)
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False

def is_agent_alive(client_id: int) -> bool:
    """True, если PID-файл существует и процесс жив."""
    pid_file = _pid_file(client_id)
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False
    return _pid_alive(pid)

def kill_existing_agent(client_id: int) -> bool:
    """Убивает процесс агента по PID-файлу. True если убил (или его не было)."""
    pid_file = _pid_file(client_id)
    if not os.path.exists(pid_file):
        return True

    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return True

    killed = False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5
            )
            killed = (result.returncode == 0)
        else:
            os.kill(pid, 15)
            killed = True
        if killed:
            logger.info(f"[{client_id}] Убит старый агент (PID {pid})")
    except (ProcessLookupError, PermissionError, FileNotFoundError):
        killed = True
    except Exception as e:
        logger.warning(f"[{client_id}] Не удалось убить PID {pid}: {e}")

    try:
        os.remove(pid_file)
    except OSError:
        pass

    return killed

def activate_client_bot(client_id: int, token: str) -> bool:
    """
    Запускает bot_agent.py для клиента.
    Токен передаётся через env AGENT_TOKEN (не argv — безопасно для ps aux).
    """
    script = os.path.join(BASE_DIR, "bot_agent.py")
    if not os.path.exists(script):
        logger.error(f"bot_agent.py не найден: {script}")
        return False

    kill_existing_agent(client_id)

    try:
        env = os.environ.copy()
        env["AGENT_TOKEN"] = token

        proc = subprocess.Popen(
            [sys.executable, script, str(client_id)],
            cwd=BASE_DIR,
            env=env
        )
        with open(_pid_file(client_id), "w") as f:
            f.write(str(proc.pid))
        logger.info(f"[{client_id}] Агент запущен (PID {proc.pid})")
        return True
    except Exception as e:
        logger.error(f"Ошибка запуска агента для {client_id}: {e}")
        return False

def restore_agents():
    """При старте поднимает всех активных агентов, кроме уже работающих."""
    users = db_users.get_all()
    count = 0
    skipped = 0
    for u in users:
        if not u.get("bot_token"):
            continue
        if not u.get("is_bot_active"):
            continue
        if u.get("is_banned"):
            continue
        if u.get("tariff") in ("expired",):
            continue

        # Не перезапускаем, если уже работает
        if is_agent_alive(u["user_id"]):
            skipped += 1
            continue

        if activate_client_bot(u["user_id"], u["bot_token"]):
            count += 1

    if count or skipped:
        logger.info(f"✅ Запущено агентов: {count} (уже работали: {skipped})")