# bot_agent.py
"""
AI-АГЕНТ для клиента.
Запускается как отдельный процесс: python bot_agent.py <CLIENT_ID>
Токен читается из env AGENT_TOKEN (безопасно) или argv[2] (legacy).

Возможности:
- DeepSeek AI, RAG, FAQ, fallback
- Lead Scoring, теги, тональность
- Расписание, реаниматор (3 follow-up)
- Автоопределение языка
- Отправка горячих лидов в CRM
- facts (budget/deadline/problem) сохраняются для инсайтов
"""

import os
import sys
import json
import logging
import time
import re
import asyncio
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
from langdetect import detect, DetectorFactory, LangDetectException
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from telegram.error import Forbidden, BadRequest
from telegram.constants import ChatAction

from config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, is_deepseek_configured
import crm_client
from database import db_users, db_leads, db_stats, db_notifications, db_crm_log

# Детерминизм langdetect (иначе на одном тексте может выдавать разные языки)
DetectorFactory.seed = 0

# ========================================================================
# АРГУМЕНТЫ
# ========================================================================

if len(sys.argv) < 2:
    print("Использование: python bot_agent.py <CLIENT_ID>")
    print("   Токен читается из env-переменной AGENT_TOKEN (безопасно)")
    print("   или из argv[2] (legacy, для ручного запуска)")
    sys.exit(1)

try:
    CLIENT_ID = int(sys.argv[1])
except ValueError:
    print(f"❌ CLIENT_ID должен быть числом: {sys.argv[1]}")
    sys.exit(1)

BOT_TOKEN = os.environ.get("AGENT_TOKEN")
if not BOT_TOKEN and len(sys.argv) >= 3:
    BOT_TOKEN = sys.argv[2]

if not BOT_TOKEN:
    print("Токен не задан. Установи AGENT_TOKEN в env или передай 2-м аргументом.")
    sys.exit(1)

LANGUAGE_NAMES = {
    "ru": "русском",
    "en": "английском",
    "es": "испанском",
    "ar": "арабском",
    "zh": "китайском",
}

MIN_DETECT_LENGTH = 15

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
# КЛИЕНТСКИЕ НАСТРОЙКИ
# ========================================================================

class ClientSettings:
    CACHE_TTL = 30

    def __init__(self, client_id: int):
        self.client_id = client_id
        self._last_reload = 0.0
        self._loaded = False

        self.is_active = True
        self.is_banned = False
        self.managers: List = []
        self.bot_settings: Dict = {}
        self.lead_settings: Dict = {}
        self.settings: Dict = {}
        self.schedules: Dict = {}
        self.rag_data: List[str] = []
        self.rag_index: List[Tuple[str, set]] = []
        self.custom_fields: List = []
        self.language: str = "ru"

        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
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
        self.schedules = data.get("schedules") or {}
        self.rag_data = data.get("rag_data") or []
        self.custom_fields = data.get("custom_fields") or []
        self.language = data.get("language") or "ru"

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

    @property
    def schedule_enabled(self) -> bool:
        return bool(self.schedules.get("enabled", False))

    @property
    def schedule_start(self) -> str:
        return self.schedules.get("start_time", "09:00")

    @property
    def schedule_end(self) -> str:
        return self.schedules.get("end_time", "21:00")

    @property
    def ab_enabled(self) -> bool:
        return bool(self.bot_settings.get("ab_enabled", False))

    @property
    def ab_variant_a(self) -> str:
        return (self.bot_settings.get("ab_variant_a") or "").strip()

    @property
    def ab_variant_b(self) -> str:
        return (self.bot_settings.get("ab_variant_b") or "").strip()

client_settings = ClientSettings(CLIENT_ID)
if not client_settings._loaded:
    logger.error(f"❌ Клиент {CLIENT_ID} не найден, выходим")
    sys.exit(1)

logger.info(f"✅ Агент {CLIENT_ID} загружен. Менеджеров: {len(client_settings.managers)}")

# ========================================================================
# РАСПИСАНИЕ
# ========================================================================

def is_within_schedule() -> bool:
    if not client_settings.schedule_enabled:
        return True

    try:
        start_t = datetime.strptime(client_settings.schedule_start, "%H:%M").time()
        end_t = datetime.strptime(client_settings.schedule_end, "%H:%M").time()
    except ValueError:
        logger.warning(
            f"Битое расписание клиента {CLIENT_ID}: "
            f"{client_settings.schedule_start}-{client_settings.schedule_end}, "
            f"игнорирую"
        )
        return True

    now_t = datetime.now().time()

    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t

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
        result = {}
        lower = text.lower()

        # Бюджет — ищем любые числа от 100
        budget_match = re.search(r"(\d{3,})", lower)
        if budget_match:
            try:
                num = int(budget_match.group(1))
                if "тыс" in lower or num < 1000:
                    num *= 1000
                result["budget"] = num
            except (ValueError, TypeError):
                pass

        # Сроки — ищем цифры (7 дней) ИЛИ слова (семь дней)
        days_map = {"один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, 
                    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
        
        deadline_match = re.search(r"(\d+)\s*(?:дн|день|дней|дня|недел|месяц)", lower)
        if deadline_match:
            try:
                num = int(deadline_match.group(1))
                if "недел" in lower: num *= 7
                elif "месяц" in lower: num *= 30
                result["deadline"] = num
            except (ValueError, TypeError):
                pass
        else:
            for word, num in days_map.items():
                if f"{word} дн" in lower or f"{word} нед" in lower:
                    result["deadline"] = num * (7 if "нед" in lower else 1)
                    break
                
        return result

    def system_prompt(self, session: dict) -> str:
        script = client_settings.script
        products = ", ".join(client_settings.products) or "не указаны"
        features = ", ".join(client_settings.features) or "не указаны"
        faq = client_settings.faq[:5]
        faq_text = "\n".join(
            [f"Q: {f['question']}\nA: {f['answer']}" for f in faq if f.get("question")]
        )

        lang_code = session.get("language") or client_settings.language or "ru"
        lang_name = LANGUAGE_NAMES.get(lang_code, "русском")

        return f"""Ты — менеджер по продажам. Квалифицируй лидов.

БИЗНЕС:
- Скрипт: {script}
- Товары: {products}
- Особенности: {features}

FAQ:
{faq_text}

ПРАВИЛА:
1. Отвечай кратко (2-3 предложения).
2. Если клиент спрашивает о товарах — упомяни их.
3. Если клиент злой — предложи переключить на менеджера.
4. Если клиент готов купить — скажи, что менеджер свяжется.
5. Отвечай ТОЛЬКО на {lang_name} языке. Не переключайся, даже если клиент смешивает языки.
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
        if user_id in self.sessions:
            self.last_message[user_id] = time.time()
            return "Продолжаем. Чем могу помочь?"

        ab_variant = ""
        initial_text = client_settings.script
        if client_settings.ab_enabled:
            a = client_settings.ab_variant_a
            b = client_settings.ab_variant_b
            if a and b:
                ab_variant = random.choice(["A", "B"])
                initial_text = a if ab_variant == "A" else b

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
            "followup_count": 0,
            "last_followup_at": None,
            "fallback_attempts": 0,
            "last_api_call": 0,
            "lead_id": None,
            "stats_logged": False,
            "hot_logged": False,
            "ab_variant": ab_variant,
            "created_at": datetime.now().isoformat()
        }
        self.last_message[user_id] = time.time()
        return f"👋 {initial_text}\n\nРасскажите, с какой проблемой вы столкнулись?"

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

        if b_thresh == 0 and d_limit == 0:
            return session.get("score", 0) >= 40

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

    def detect_language(self, text: str) -> Optional[str]:
        if not text or len(text) < MIN_DETECT_LENGTH:
            return None
        try:
            lang = detect(text)
        except LangDetectException:
            return None

        mapping = {
            "ru": "ru", "uk": "ru", "be": "ru", "bg": "ru",
            "en": "en",
            "es": "es", "pt": "es", "it": "es", "fr": "es",
            "ar": "ar", "fa": "ar", "ur": "ar",
            "zh": "zh", "zh-cn": "zh", "zh-tw": "zh",
        }
        return mapping.get(lang)

    async def process(self, user_id: int, text: str, context,
                      username: str = None, first_name: str = None) -> Optional[str]:
        client_settings.reload()

        if client_settings.is_banned:
            return None
        if not client_settings.is_active:
            return None
        if not is_within_schedule():
            return None

        is_new_session = False
        welcome_text = None
        if user_id not in self.sessions:
            welcome_text = self.start_session(user_id, username, first_name)
            is_new_session = True

        session = self.sessions[user_id]
        session["messages"].append({"role": "user", "content": text})
        
        # Ограничиваем историю 20 сообщениями
        if len(session["messages"]) > 20:
            session["messages"] = session["messages"][-20:]

        self.last_message[user_id] = time.time()

        detected_lang = self.detect_language(text)
        if detected_lang:
            session["language"] = detected_lang

        # 1. Создаём или восстанавливаем лид
        if session.get("lead_id") is None:
            existing = db_leads.get_by_telegram_id(CLIENT_ID, user_id)
            if existing:
                session["lead_id"] = existing["id"]
                try:
                    facts = json.loads(existing.get("facts") or "{}")
                    session["problem"] = facts.get("problem")
                    session["budget"] = facts.get("budget")
                    session["deadline"] = facts.get("deadline")
                    session["score"] = existing.get("score", 0)
                    session["hot"] = bool(existing.get("is_hot"))
                except:
                    pass
            else:
                try:
                    lead_id = db_leads.create(CLIENT_ID, {
                        "telegram_id": user_id,
                        "username": session.get("username"),
                        "name": session.get("first_name"),
                        "messages": session.get("messages", []),
                        "summary": "",
                        "score": 0,
                        "is_hot": False,
                        "tags": [],
                        "sentiment": "neutral",
                        "facts": {},
                        "ab_variant": session.get("ab_variant", "")
                    })
                    session["lead_id"] = lead_id
                except Exception as e:
                    logger.error(f"Не удалось создать лид: {e}")

        sentiment, tags = self.analyze(text)
        session["sentiment"] = sentiment
        session["tags"] = list(set(session.get("tags", []) + tags))

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

        # Ответ: FAQ → RAG → AI → fallback
        response = None

        faq = self.search_faq(text)
        if faq:
            session["messages"].append({"role": "assistant", "content": faq})
            response = faq

        if response is None:
            rag = self.search_rag(text)
            if rag:
                session["messages"].append({"role": "assistant", "content": rag})
                response = rag

        if response is None and self.ai.is_configured():
            # АНТИ-ДДОС
            now_ts = time.time()
            if now_ts - session.get("last_api_call", 0) > 3:
                session["last_api_call"] = now_ts
                messages = [{"role": "system", "content": self.ai.system_prompt(session)}]
                messages += session["messages"][-6:]
                ai_answer = await self.ai.get_response(messages)
                if ai_answer:
                    session["messages"].append({"role": "assistant", "content": ai_answer})
                    if any(w in ai_answer.lower() for w in ["менеджер", "свяжется", "переключу"]):
                        session["manager_assigned"] = True
                    response = ai_answer
            else:
                logger.warning(f"Rate limit для лида {user_id}. Слишком частые сообщения.")
                response = "Пожалуйста, подождите пару секунд перед следующим сообщением."

        if response is None:
            response = self._fallback_response(session, text)

        session["score"] = self.calculate_score(session)
        if session.get("problem") and (session.get("budget") or session.get("deadline")):
            session["ready"] = True
            session["hot"] = self.is_hot(session)

        if session.get("lead_id"):
            try:
                db_leads.update(
                    session["lead_id"],
                    messages=session.get("messages", []),
                    score=session.get("score", 0),
                    is_hot=session.get("hot", False),
                    tags=session.get("tags", []),
                    sentiment=session.get("sentiment", "neutral"),
                    facts={
                        "budget": session.get("budget"),
                        "deadline": session.get("deadline"),
                        "problem": session.get("problem")
                    }
                )
            except Exception as e:
                logger.error(f"Не удалось обновить лид: {e}")

        if not session.get("stats_logged") and (session.get("problem") or session.get("hot") or len(session["messages"]) >= 4):
            try:
                db_stats.log_lead(CLIENT_ID, False)
                session["stats_logged"] = True
            except Exception as e:
                logger.error(f"Не удалось записать статистику: {e}")

        if session.get("hot") and not session.get("hot_logged"):
            try:
                db_stats.increment_hot(CLIENT_ID)
                session["hot_logged"] = True
            except Exception as e:
                logger.error(f"Не удалось записать hot-статистику: {e}")

        if session.get("hot") and not session.get("notified"):
            await self.notify_client(session, user_id, context)

        if is_new_session and welcome_text:
            return f"{welcome_text}\n\n{response}"
            
        return response

    def _fallback_response(self, session: dict, text: str) -> str:
        session["fallback_attempts"] = session.get("fallback_attempts", 0) + 1

        if session.get("problem") is None:
            session["problem"] = text
            session["score"] = self.calculate_score(session)
            return "💰 Какой бюджет вы рассматриваете?"

        if session.get("budget") is None:
            structured = DeepSeekAI._extract_with_regex(text)
            if structured.get("budget"):
                session["budget"] = structured["budget"]
            else:
                session["budget"] = 0
            session["score"] = self.calculate_score(session)
            return "📅 В какие сроки нужно решение?"

        if session.get("deadline") is None:
            structured = DeepSeekAI._extract_with_regex(text)
            if structured.get("deadline"):
                session["deadline"] = structured["deadline"]
            else:
                session["deadline"] = 0
            
            session["score"] = self.calculate_score(session)
            session["hot"] = self.is_hot(session)
            if session["hot"]:
                return "✅ Спасибо! Менеджер свяжется с вами."
            return "📚 Спасибо! Мы отправим вам полезные материалы."

        return "Чем могу ещё помочь?"

    async def notify_client(self, session: dict, lead_id: int, context):
        if session.get("notified"):
            return
        session["notified"] = True

        summary = (
            f"🔥 **Новый горячий лид!**\n\n"
            f"👤 {session.get('first_name') or 'Неизвестно'}\n"
            f"🆔 `{lead_id}`\n"
            f"📝 {session.get('problem') or 'Не указана'}\n"
            f"💰 Бюджет: {session.get('budget') or 'Не указан'}\n"
            f"📅 Сроки: {session.get('deadline') or 'Не указаны'}\n"
            f"🏷️ Теги: {', '.join(session.get('tags', [])) or 'Нет'}\n"
            f"📊 Оценка: {session.get('score', 0)}/100"
        )

        if session.get("lead_id"):
            try:
                db_leads.update(session["lead_id"], summary=summary, is_hot=True)
            except Exception as e:
                logger.error(f"Не удалось обновить лид при уведомлении: {e}")

        try:
            db_notifications.add(CLIENT_ID, summary)
            for manager in client_settings.managers:
                try:
                    m_id = manager.get("id") if isinstance(manager, dict) else manager
                    m_id = int(m_id)
                    db_notifications.add(m_id, summary)
                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning(f"Некорректный manager: {manager}, ошибка: {e}")
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")

        crm = (client_settings.settings.get("crm_integration") or {})
        if crm.get("enabled"):
            lead_for_crm = {
                "name": session.get("first_name"),
                "problem": session.get("problem"),
                "budget": session.get("budget"),
                "deadline": session.get("deadline"),
                "telegram_id": lead_id,
                "username": session.get("username"),
                "tags": session.get("tags", []),
                "score": session.get("score", 0),
            }
            try:
                ok, crm_msg = await crm_client.push_lead(crm, lead_for_crm)
            except Exception as e:
                ok = False
                crm_msg = f"exception: {e}"

            try:
                db_crm_log.add(
                    CLIENT_ID,
                    session.get("lead_id") or 0,
                    crm.get("type", ""),
                    "success" if ok else "error",
                    crm_msg,
                )
            except Exception as e:
                logger.warning(f"Не удалось записать crm_log: {e}")

            if ok:
                logger.info(f"CRM: {crm_msg}")
            else:
                logger.warning(f"CRM ошибка: {crm_msg}")

agent = SmartAgent()

# ========================================================================
# FOLLOW-UP / РЕАНИМАТОР
# ========================================================================

DEFAULT_FOLLOWUP_INTERVALS = [3600, 86400, 259200]

def _get_followup_intervals() -> List[int]:
    raw = client_settings.bot_settings.get("followup_intervals")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_FOLLOWUP_INTERVALS)
    result = []
    for v in raw:
        try:
            n = int(v)
            if n >= 60:
                result.append(n)
        except (ValueError, TypeError):
            continue
    return result or list(DEFAULT_FOLLOWUP_INTERVALS)

async def check_followups(context: ContextTypes.DEFAULT_TYPE):
    client_settings.reload()

    if client_settings.is_banned or not client_settings.is_active:
        return
    if not is_within_schedule():
        return

    if not client_settings.bot_settings.get("followup_enabled", True):
        return

    texts = client_settings.bot_settings.get("auto_reply_texts") or []
    if not texts:
        return

    intervals = _get_followup_intervals()
    now = time.time()
    sent = 0

    for user_id, session in list(agent.sessions.items()):
        if session.get("notified"):
            continue
        if session.get("manager_assigned"):
            continue

        count = int(session.get("followup_count", 0))
        if count >= len(intervals):
            continue

        last_event = max(
            agent.last_message.get(user_id, 0),
            session.get("last_followup_at") or 0
        )
        if last_event == 0:
            continue
        if now - last_event < intervals[count]:
            continue

        text = texts[count % len(texts)]

        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            session["followup_count"] = count + 1
            session["last_followup_at"] = now
            session["messages"].append({"role": "assistant", "content": text})
            sent += 1
            logger.info(f"Follow-up #{count + 1}/{len(intervals)} → лид {user_id}")
        except Forbidden:
            logger.warning(f"Follow-up {user_id}: лид заблокировал бота")
            session["followup_count"] = len(intervals)
        except BadRequest as e:
            logger.error(f"Follow-up {user_id}: BadRequest: {e}")
            session["followup_count"] = len(intervals)
        except Exception as e:
            logger.error(f"Follow-up {user_id}: {e}")

    if sent:
        logger.info(f"Follow-up отправлено: {sent}")

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
    if not is_within_schedule():
        await update.message.reply_text(
            f"⏳ Бот работает с {client_settings.schedule_start} "
            f"до {client_settings.schedule_end}. Напишите позже."
        )
        return

    u = update.effective_user
    response = agent.start_session(u.id, u.username, u.first_name)
    await update.message.reply_text(response)

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = update.message.text
    if not text:
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        response = await agent.process(u.id, text, context, u.username, u.first_name)
        if response is None:
            return
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
        try:
            await update.message.reply_text("Извините, произошла ошибка. Попробуйте снова.")
        except Exception:
            pass

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client_settings.reload()

    if client_settings.is_banned or not client_settings.is_active:
        return
    if not is_within_schedule():
        return

    if not client_settings.voice_enabled:
        await update.message.reply_text("🎤 Голосовые сообщения отключены.")
        return

    await update.message.reply_text(client_settings.voice_error_text)

# ========================================================================
# MAIN
# ========================================================================

from andro_cfw import CFWSession

def main():
    # ... init_db() ...
    session = CFWSession.load()
    app = Application.builder().token(BOT_TOKEN) \
        .base_url(session.ptb_base_url()) \
        .base_file_url(session.ptb_base_file_url()) \
        .build()
    # ...