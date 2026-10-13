# bot_agent.py
"""
AI-АГЕНТ для клиента.
Запускается как отдельный процесс: python bot_agent.py <CLIENT_ID> <BOT_TOKEN>

Особенности:
- Кеш настроек клиента (reload раз в 30 секунд)
- DeepSeek AI через aiohttp (async)
- RAG по предвычисленному индексу
- Fallback на скрипт, если DeepSeek не настроен
- Уведомления через таблицу notifications (bot_main рассылает)
- НЕ пишет напрямую клиенту (только через bot_main)
"""

import os
import sys
import json
import logging
import time
import re
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

from config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, is_deepseek_configured
from database import db_users, db_leads, db_stats, db_notifications

# ========================================================================
# АРГУМЕНТЫ
# ========================================================================

if len(sys.argv) < 3:
    print("❌ Использование: python bot_agent.py <CLIENT_ID> <BOT_TOKEN>")
    sys.exit(1)

try:
    CLIENT_ID = int(sys.argv[1])
except ValueError:
    print(f"❌ CLIENT_ID должен быть числом: {sys.argv[1]}")
    sys.exit(1)

BOT_TOKEN = sys.argv[2]

# ========================================================================
# ЛОГИ
# ========================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(f"agent_{CLIENT_ID}.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========================================================================
# КЛИЕНТСКИЕ НАСТРОЙКИ С КЕШЕМ
# ========================================================================

class ClientSettings:
    """Настройки клиента с кешем на 30 секунд."""

    CACHE_TTL = 30

    def __init__(self, client_id: int):
        self.client_id = client_id
        self._last_reload = 0.0
        self._loaded = False

        # Значения по умолчанию
        self.is_active = True
        self.is_banned = False
        self.managers: List = []
        self.bot_settings: Dict = {}
        self.lead_settings: Dict = {}
        self.settings: Dict = {}
        self.rag_data: List[str] = []
        self.rag_index: List[Tuple[str, set]] = []
        self.custom_fields: List = []
        self.language: str = "ru"

        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        """Загружает настройки с кешем. Возвращает True при успехе."""
        now = time.time()
        if not force and self._loaded and (now - self._last_reload) < self.CACHE_TTL:
            return True

        data = db_users.get(self.client_id)
        if not data:
            logger.error(f"Клиент {self.client_id} не найден в БД")
            self._loaded = False
            return False

        self.is_active = data.get("is_bot_active", True)
        self.is_banned = data.get("is_banned", False)
        self.managers = data.get("managers") or []
        self.bot_settings = data.get("bot_settings") or {}
        self.lead_settings = data.get("lead_settings") or {}
        self.settings = data.get("settings") or {}
        self.rag_data = data.get("rag_data") or []
        self.custom_fields = data.get("custom_fields") or []
        self.language = data.get("language") or "ru"

        # Предвычисленный индекс RAG
        self.rag_index = []
        for doc in self.rag_data:
            if not isinstance(doc, str):
                continue
            words = {w for w in doc.lower().split() if len(w) > 3}
            if words:
                self.rag_index.append((doc, words))

        self._last_reload = now
        self._loaded = True
        return True

    @property
    def script(self) -> str:
        return self.bot_settings.get(
            "script",
            "Ты — профессиональный менеджер по продажам."
        )

    @property
    def products(self) -> List[str]:
        return self.bot_settings.get("products") or []

    @property
    def features(self) -> List[str]:
        return self.bot_settings.get("features") or []

    @property
    def faq(self) -> List[Dict]:
        return self.bot_settings.get("faq") or []

    @property
    def voice_error_text(self) -> str:
        return self.bot_settings.get(
            "voice_error_text",
            "Извините, я не смог распознать голосовое сообщение. Напишите текстом."
        )

    @property
    def voice_enabled(self) -> bool:
        return self.settings.get("voice_enabled", True)

# Инициализация настроек
client_settings = ClientSettings(CLIENT_ID)
if not client_settings._loaded:
    logger.error(f"❌ Клиент {CLIENT_ID} не найден, выходим")
    sys.exit(1)

logger.info(f"✅ Агент {CLIENT_ID} загружен. Менеджеров: {len(client_settings.managers)}")

# ========================================================================
# DEEPSEEK AI
# ========================================================================

class DeepSeekAI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.timeout = aiohttp.ClientTimeout(total=20)

    def is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "YOUR_DEEPSEEK_API_KEY"

    async def get_response(self, messages: List[Dict], max_tokens: int = 250) -> Optional[str]:
        if not self.is_configured():
            return None

        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    DEEPSEEK_API_URL, json=payload, headers=self.headers
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
                    text = await r.text()
                    logger.error(f"DeepSeek: {r.status} {text[:200]}")
                    return None
        except asyncio.TimeoutError:
            logger.error("DeepSeek timeout")
            return None
        except Exception as e:
            logger.error(f"DeepSeek exception: {e}")
            return None

    async def extract_structured_data(self, text: str) -> Dict:
        """Извлекает problem/budget/deadline из текста."""
        if not self.is_configured():
            return self._extract_with_regex(text)

        prompt = (
            "Извлеки из текста информацию и верни ТОЛЬКО JSON без пояснений:\n"
            '{"problem": "строка или null", "budget": число или null, "deadline": число дней или null}\n\n'
            f"Текст: {text}"
        )
        messages = [{"role": "user", "content": prompt}]
        response = await self.get_response(messages, max_tokens=100)
        if not response:
            return self._extract_with_regex(text)

        try:
            response = response.strip()
            if response.startswith("```"):
                response = re.sub(r"```(?:json)?", "", response).strip()
                response = response.rstrip("`").strip()
            result = json.loads(response)
            return result if isinstance(result, dict) else {}
        except Exception:
            return self._extract_with_regex(text)

    @staticmethod
    def _extract_with_regex(text: str) -> Dict:
        """Fallback. Ищет только рядом с ключевыми словами."""
        result = {}
        lower = text.lower()

        # Бюджет — только рядом с валютой
        budget_match = re.search(r"(\d{3,})\s*(?:₽|руб|тыс|к\b|k\b)", lower)
        if budget_match:
            try:
                num = int(budget_match.group(1))
                if "тыс" in lower or num < 1000:
                    num *= 1000
                result["budget"] = num
            except (ValueError, TypeError):
                pass

        # Сроки — только рядом с днями/неделями/месяцами
        deadline_match = re.search(r"(\d+)\s*(?:дн|день|дней|дня|недел|месяц)", lower)
        if deadline_match:
            try:
                num = int(deadline_match.group(1))
                if "недел" in lower:
                    num *= 7
                elif "месяц" in lower:
                    num *= 30
                result["deadline"] = num
            except (ValueError, TypeError):
                pass

        return result

    def system_prompt(self, session: dict) -> str:
        script = client_settings.script
        products = ", ".join(client_settings.products) or "не указаны"
        features = ", ".join(client_settings.features) or "не указаны"
        faq = client_settings.faq[:5]
        faq_text = "\n".join(
            [f"Q: {f['question']}\nA: {f['answer']}" for f in faq if f.get("question")]
        )

        history = ""
        for msg in session.get("messages", [])[-6:]:
            role = "Клиент" if msg["role"] == "user" else "Бот"
            history += f"{role}: {msg['content']}\n"

        return f"""Ты — менеджер по продажам. Квалифицируй лидов.

БИЗНЕС:
- Скрипт: {script}
- Товары: {products}
- Особенности: {features}

FAQ:
{faq_text}

ИСТОРИЯ:
{history}

ПРАВИЛА:
1. Отвечай кратко (2-3 предложения).
2. Если клиент спрашивает о товарах — упомяни их.
3. Если клиент злой — предложи переключить на менеджера.
4. Если клиент готов купить — скажи, что менеджер свяжется.
5. Отвечай на языке клиента.
"""

# ========================================================================
# АГЕНТ
# ========================================================================

class SmartAgent:
    def __init__(self):
        self.sessions: Dict[int, dict] = {}
        self.last_message: Dict[int, float] = {}
        self.ai = DeepSeekAI(DEEPSEEK_API_KEY)

    def start_session(self, user_id: int, username: str = None, first_name: str = None) -> str:
        """Создаёт сессию, если её нет. Существующую не сбрасывает."""
        if user_id in self.sessions:
            self.last_message[user_id] = time.time()
            return "Продолжаем. Чем могу помочь?"

        self.sessions[user_id] = {
            "username": username,
            "first_name": first_name,
            "stage": "welcome",
            "problem": None,
            "budget": None,
            "deadline": None,
            "score": 0,
            "ready": False,
            "hot": False,
            "tags": [],
            "sentiment": "neutral",
            "messages": [],
            "manager_assigned": False,
            "contact": None,
            "language": client_settings.language if client_settings.language != "auto" else "ru",
            "notified": False,
            "lead_id": None,
            "created_at": datetime.now().isoformat()
        }
        self.last_message[user_id] = time.time()
        return self._initial_message()

    def _initial_message(self) -> str:
        return f"👋 {client_settings.script}\n\nРасскажите, с какой проблемой вы столкнулись?"

    def analyze(self, text: str) -> Tuple[str, List[str]]:
        t = text.lower()
        sentiment = "neutral"
        tags = []

        if client_settings.settings.get("sentiment_enabled", True):
            negative = ["плохо", "ужасно", "недоволен", "злой", "разочарован", "кошмар"]
            positive = ["отлично", "спасибо", "хорошо", "супер", "доволен"]
            if any(w in t for w in negative):
                sentiment = "negative"
                tags.append("злой")
            elif any(w in t for w in positive):
                sentiment = "positive"
                tags.append("довольный")

        if client_settings.settings.get("autotag_enabled", True):
            if any(w in t for w in ["срочно", "сейчас", "быстро"]):
                tags.append("срочно")
            if any(w in t for w in ["купить", "хочу", "оплатить", "заказ"]):
                tags.append("горячий")
            if any(w in t for w in ["цена", "бюджет", "стоимость"]):
                tags.append("бюджет")

        return sentiment, tags

    def search_faq(self, text: str) -> Optional[str]:
        for item in client_settings.faq:
            q = (item.get("question") or "").lower()
            if q and q in text.lower():
                return item.get("answer", "")
        return None

    def search_rag(self, text: str) -> Optional[str]:
        """RAG по предвычисленному индексу. Минимум 2 общих слова."""
        if not client_settings.rag_index:
            return None

        words = {w for w in text.lower().split() if len(w) > 3}
        if not words:
            return None

        best_doc = None
        best_score = 0
        for doc, doc_words in client_settings.rag_index:
            common = words & doc_words
            if len(common) >= 2 and len(common) > best_score:
                best_score = len(common)
                best_doc = doc
        return best_doc

    def is_hot(self, session: dict) -> bool:
        budget = session.get("budget")
        deadline = session.get("deadline")
        b_thresh = client_settings.lead_settings.get("budget_threshold", 0) or 0
        d_limit = client_settings.lead_settings.get("deadline_days", 0) or 0
        rule = client_settings.lead_settings.get("rule", "AND")

        # Если фильтры не заданы — работаем по score
        if b_thresh == 0 and d_limit == 0:
            return session.get("score", 0) >= 60

        budget_ok = True
        deadline_ok = True

        if b_thresh > 0:
            if budget is None:
                budget_ok = False
            else:
                try:
                    budget_ok = int(budget) >= b_thresh
                except (ValueError, TypeError):
                    budget_ok = False

        if d_limit > 0:
            if deadline is None:
                deadline_ok = False
            else:
                try:
                    deadline_ok = int(deadline) <= d_limit
                except (ValueError, TypeError):
                    deadline_ok = False

        if rule == "AND":
            return budget_ok and deadline_ok
        return budget_ok or deadline_ok

    def calculate_score(self, session: dict) -> int:
        score = 0
        if session.get("problem"): score += 10
        if session.get("budget"): score += 20
        if session.get("deadline"): score += 20
        if session.get("sentiment") == "positive": score += 10
        if "горячий" in session.get("tags", []): score += 15
        if session.get("contact"): score += 10
        return min(100, max(0, score))

    async def process(self, user_id: int, text: str, context) -> Optional[str]:
        """Основной метод. Возвращает ответ или None (бот выключен/забанен)."""
        client_settings.reload()

        if client_settings.is_banned:
            return None
        if not client_settings.is_active:
            return None

        if user_id not in self.sessions:
            self.start_session(user_id, None, None)

        session = self.sessions[user_id]
        session["messages"].append({"role": "user", "content": text})
        self.last_message[user_id] = time.time()

        # Анализ тональности и тегов
        sentiment, tags = self.analyze(text)
        session["sentiment"] = sentiment
        session["tags"] = list(set(session.get("tags", []) + tags))

        # Извлечение структурированных данных (ДО FAQ — чтобы не потерять бюджет)
        should_extract = (
            len(session["messages"]) >= 3
            or any(kw in text.lower() for kw in
                   ["бюджет", "руб", "₽", "стоимость", "цена", "срок", "дн", "недел", "тыс"])
        )
        if should_extract:
            structured = await self.ai.extract_structured_data(text)
            if structured.get("problem") and not session.get("problem"):
                session["problem"] = structured["problem"]
            if structured.get("budget") and not session.get("budget"):
                session["budget"] = structured["budget"]
            if structured.get("deadline") and not session.get("deadline"):
                session["deadline"] = structured["deadline"]

        # FAQ
        faq = self.search_faq(text)
        if faq:
            session["messages"].append({"role": "assistant", "content": faq})
            return faq

        # RAG
        rag = self.search_rag(text)
        if rag:
            session["messages"].append({"role": "assistant", "content": rag})
            return rag

        # DeepSeek
        if self.ai.is_configured():
            messages = [{"role": "system", "content": self.ai.system_prompt(session)}]
            messages += session["messages"][-6:]
            ai_answer = await self.ai.get_response(messages)
            if ai_answer:
                session["messages"].append({"role": "assistant", "content": ai_answer})
                session["score"] = self.calculate_score(session)

                # Проверка на горячего лида
                if session.get("problem") and (session.get("budget") or session.get("deadline")):
                    session["ready"] = True
                    session["hot"] = self.is_hot(session)
                    if session["hot"]:
                        await self.notify_client(session, user_id, context)

                if any(w in ai_answer.lower() for w in ["менеджер", "свяжется", "переключу"]):
                    session["manager_assigned"] = True

                return ai_answer

        # Fallback — простой скрипт
        return self._fallback_response(session, text)

    def _fallback_response(self, session: dict, text: str) -> str:
        """Простой диалог, если DeepSeek не настроен/не ответил."""
        if not session.get("problem"):
            session["problem"] = text
            return "💰 Какой бюджет вы рассматриваете?"

        if not session.get("budget"):
            structured = DeepSeekAI._extract_with_regex(text)
            session["budget"] = structured.get("budget")
            return "📅 В какие сроки нужно решение?"

        if not session.get("deadline"):
            structured = DeepSeekAI._extract_with_regex(text)
            session["deadline"] = structured.get("deadline")
            session["ready"] = True
            session["score"] = self.calculate_score(session)
            session["hot"] = self.is_hot(session)
            if session["hot"]:
                return "✅ Спасибо! Менеджер свяжется с вами."
            return "📚 Спасибо! Мы отправим вам полезные материалы."

        return "Чем могу ещё помочь?"

    async def notify_client(self, session: dict, lead_id: int, context):
        """Сохраняет лид и добавляет уведомления в очередь (bot_main разошлёт)."""
        if session.get("notified"):
            return
        session["notified"] = True

        summary = (
            f"🔥 **Новый лид!**\n\n"
            f"👤 {session.get('first_name') or 'Неизвестно'}\n"
            f"🆔 `{lead_id}`\n"
            f"📝 {session.get('problem') or 'Не указана'}\n"
            f"💰 Бюджет: {session.get('budget') or 'Не указан'}\n"
            f"📅 Сроки: {session.get('deadline') or 'Не указаны'}\n"
            f"🏷️ Теги: {', '.join(session.get('tags', [])) or 'Нет'}\n"
            f"📊 Оценка: {session.get('score', 0)}/100"
        )

        # Сохраняем лид в БД
        lead_db_id = db_leads.create(CLIENT_ID, {
            "telegram_id": lead_id,
            "username": session.get("username"),
            "name": session.get("first_name"),
            "messages": session.get("messages", []),
            "summary": summary,
            "score": session.get("score", 0),
            "is_hot": session.get("hot", False),
            "tags": session.get("tags", []),
            "sentiment": session.get("sentiment", "neutral")
        })
        session["lead_id"] = lead_db_id

        # Статистика
        db_stats.log_lead(CLIENT_ID, session.get("hot", False))

        # Уведомление владельцу (через БД)
        db_notifications.add(CLIENT_ID, summary)

        # Уведомления менеджерам
        for manager in client_settings.managers:
            try:
                m_id = manager.get("id") if isinstance(manager, dict) else manager
                m_id = int(m_id)
                db_notifications.add(m_id, summary)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(f"Некорректный manager: {manager}, ошибка: {e}")

# Инициализация агента
agent = SmartAgent()

# ========================================================================
# ОБРАБОТЧИКИ
# ========================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client_settings.reload()

    if client_settings.is_banned:
        await update.message.reply_text("⛔ Бот недоступен.")
        return
    if not client_settings.is_active:
        await update.message.reply_text("⏳ Бот временно отключён.")
        return

    u = update.effective_user
    response = agent.start_session(u.id, u.username, u.first_name)
    await update.message.reply_text(response)

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text:
        return

    try:
        response = await agent.process(uid, text, context)
        if response is None:
            return  # Бот выключен/забанен — тихо игнорируем
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
        await update.message.reply_text("Извините, произошла ошибка. Попробуйте снова.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client_settings.reload()

    if client_settings.is_banned or not client_settings.is_active:
        return

    if not client_settings.voice_enabled:
        await update.message.reply_text("🎤 Голосовые сообщения отключены.")
        return

    await update.message.reply_text(client_settings.voice_error_text)

# ========================================================================
# MAIN
# ========================================================================

def main():
    if not BOT_TOKEN:
        logger.error("Нет токена")
        return

    if client_settings.is_banned:
        logger.error(f"Клиент {CLIENT_ID} забанен, выходим")
        return

    if not is_deepseek_configured():
        logger.warning("⚠️ DeepSeek не настроен — работаем в fallback-режиме")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info(f"🤖 Агент {CLIENT_ID} запущен")
    app.run_polling()

if __name__ == "__main__":
    main()