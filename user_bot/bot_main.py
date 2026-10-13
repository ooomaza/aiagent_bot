# bot_main.py
"""
ГЛАВНЫЙ БОТ.
- Продажа тарифов, активация агентов, админ-команды
- Управление настройками клиента (скрипт, товары, RAG, FAQ, лиды, CRM и т.д.)
- Восстановление агентов при старте
- Очередь уведомлений от bot_agent
"""

import os
import sys
import logging
import subprocess
import asyncio
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import Forbidden, BadRequest

from config import (
    MAIN_BOT_TOKEN, ADMIN_IDS, CHANNEL_URL, channel_is_configured,
    TARIFFS, PAID_TARIFFS, BOT_LIMITS, LANGUAGES, is_configured
)
from database import (
    init_db, db_users, db_leads, db_stats, db_notifications
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("main_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========================================================================
# ХЕЛПЕРЫ
# ========================================================================

def is_admin(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except (ValueError, TypeError):
        return False

def get_referral_discount(user_id) -> float:
    """Максимум из реферальной и manual-скидки."""
    user_data = db_users.get(int(user_id))
    if not user_data:
        return 0.0

    count = user_data.get("referred_count", 0)
    if count >= 6:
        referral = 0.40
    elif count >= 3:
        referral = 0.20
    else:
        referral = 0.0

    manual = (user_data.get("manual_discount") or 0) / 100.0

    return min(0.40, max(referral, manual))

def get_price_with_discount(tariff_key: str, user_id=None) -> int:
    base = TARIFFS[tariff_key]["price"]
    discount = 0.0
    if user_id and tariff_key != "trial":
        discount = get_referral_discount(user_id)
    discount = min(discount, 0.40)
    return int(base * (1 - discount))

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка подписки. Использовать только в /start и client_panel."""
    user_id = update.effective_user.id
    if is_admin(user_id):
        return True
    if not channel_is_configured():
        return True

    user_data = db_users.get(user_id)
    if user_data and user_data.get("subscribed"):
        return True

    try:
        channel_username = CHANNEL_URL.rstrip("/").split("/")[-1]
        member = await context.bot.get_chat_member(
            chat_id=f"@{channel_username}", user_id=user_id
        )
        if member.status in ("member", "administrator", "creator"):
            db_users.update(user_id, subscribed=True)
            return True
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")

    return False

# ========================================================================
# УПРАВЛЕНИЕ АГЕНТАМИ
# ========================================================================

def _pid_file(client_id: int) -> str:
    return f"agent_{client_id}.pid"

def kill_existing_agent(client_id: int):
    """Убивает старый процесс агента, если PID-файл существует."""
    pid_file = _pid_file(client_id)
    if not os.path.exists(pid_file):
        return

    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return

    # Проверяем, что процесс ещё жив
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5
            )
            # Возвращает 0 если убил
        else:
            os.kill(pid, 15)  # SIGTERM
        logger.info(f"[{client_id}] Убит старый агент (PID {pid})")
    except (ProcessLookupError, PermissionError, FileNotFoundError):
        pass
    except Exception as e:
        logger.warning(f"[{client_id}] Не удалось убить PID {pid}: {e}")

    try:
        os.remove(pid_file)
    except OSError:
        pass

def activate_client_bot(client_id: int, token: str) -> bool:
    """Запускает bot_agent.py для клиента с сохранением PID."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_agent.py")
    if not os.path.exists(script):
        logger.error(f"bot_agent.py не найден: {script}")
        return False

    kill_existing_agent(client_id)

    try:
        proc = subprocess.Popen(
            [sys.executable, script, str(client_id), token],
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        with open(_pid_file(client_id), "w") as f:
            f.write(str(proc.pid))
        logger.info(f"[{client_id}] Агент запущен (PID {proc.pid})")
        return True
    except Exception as e:
        logger.error(f"Ошибка запуска агента для {client_id}: {e}")
        return False

def restore_agents():
    """При старте поднимает агентов для всех активных клиентов."""
    users = db_users.get_all()
    count = 0
    for u in users:
        if not u.get("bot_token"):
            continue
        if not u.get("is_bot_active"):
            continue
        if u.get("is_banned"):
            continue
        if activate_client_bot(u["user_id"], u["bot_token"]):
            count += 1
    if count:
        logger.info(f"✅ Восстановлено агентов: {count}")

# ========================================================================
# КЛАВИАТУРЫ
# ========================================================================

def language_choice_keyboard():
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"lang_{code}")]
        for code, name in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard(user_id=None):
    keyboard = [
        [InlineKeyboardButton("📦 Купить AI-менеджера", callback_data="buy")],
        [InlineKeyboardButton("🔑 Активировать бота", callback_data="add_token")],
        [InlineKeyboardButton("⚙️ Управление ботом", callback_data="client_panel")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("❓ Инструкция", callback_data="help")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")],
        [InlineKeyboardButton("💡 Предложить идею", callback_data="suggest_idea")]
    ]
    return InlineKeyboardMarkup(keyboard)

def client_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 НАСТРОЙКА ЛИЧНОСТИ", callback_data="header_personality")],
        [InlineKeyboardButton("   ⚙️ Скрипт и товары", callback_data="bot_settings")],
        [InlineKeyboardButton("   📋 Товары", callback_data="show_products")],
        [InlineKeyboardButton("   📚 RAG", callback_data="rag_settings")],
        [InlineKeyboardButton("   ❓ FAQ", callback_data="faq_settings")],
        [InlineKeyboardButton("   🎯 Лиды", callback_data="lead_settings")],
        [InlineKeyboardButton("   📝 Кастомные поля", callback_data="custom_fields_settings")],
        [InlineKeyboardButton("🎤 КОММУНИКАЦИИ", callback_data="header_communication")],
        [InlineKeyboardButton("   🎤 Голос", callback_data="voice_settings")],
        [InlineKeyboardButton("   💬 Шаблоны", callback_data="template_settings")],
        [InlineKeyboardButton("   🌍 Языки", callback_data="language_settings")],
        [InlineKeyboardButton("👥 КОМАНДА", callback_data="header_team")],
        [InlineKeyboardButton("   👥 Менеджеры", callback_data="manager_settings")],
        [InlineKeyboardButton("   🏆 Геймификация", callback_data="gamification_settings")],
        [InlineKeyboardButton("📊 ИНТЕГРАЦИИ", callback_data="header_integrations")],
        [InlineKeyboardButton("   📊 CRM", callback_data="crm_settings")],
        [InlineKeyboardButton("   🤖 Мульти-бот", callback_data="multi_bot_settings")],
        [InlineKeyboardButton("⚙️ УПРАВЛЕНИЕ", callback_data="header_management")],
        [InlineKeyboardButton("   🟢 Вкл/Выкл бота", callback_data="bot_toggle")],
        [InlineKeyboardButton("   🕐 Расписание", callback_data="schedule_settings")],
        [InlineKeyboardButton("   📊 Экспорт", callback_data="export_stats")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ])

def tariffs_keyboard():
    keyboard = []
    for key in PAID_TARIFFS:
        t = TARIFFS[key]
        price = get_price_with_discount(key)
        keyboard.append([InlineKeyboardButton(
            f"{t['name']} — {price} ₽/мес ({t['leads']} лидов)",
            callback_data=f"tariff_{key}"
        )])
    keyboard.append([InlineKeyboardButton("🎁 Пробный период 2 дня", callback_data="tariff_trial")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)

# ========================================================================
# /start И КОМАНДЫ
# ========================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    context.user_data.clear()

    user_data = db_users.get(user_id)
    if not user_data:
        db_users.create_default(
            user_id,
            user.username or "Unknown",
            user.first_name or "User"
        )
        user_data = db_users.get(user_id)

    if user_data.get("is_banned"):
        await update.message.reply_text("⛔ Вы забанены.")
        return

    if not await check_subscription(update, context):
        keyboard = [[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_URL)]]
        await update.message.reply_text(
            "🔔 **Для использования бота подпишитесь на канал.**\n\n"
            "После подписки нажмите /start снова.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if not user_data.get("language_chosen"):
        await update.message.reply_text(
            "🌍 **Выберите язык:**",
            reply_markup=language_choice_keyboard()
        )
        return

    discount = get_referral_discount(user_id)
    await update.message.reply_text(
        f"🤖 **Добро пожаловать в AI-менеджер!**\n\n"
        f"🎁 Реферальный код: `{user_data.get('referral_code', '')}`\n"
        f"💰 Скидка: {int(discount * 100)}%\n\n"
        "📦 Тарифы: /tariffs\n📖 Инструкция: /help",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(user_id)
    )

async def tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = "Выберите тариф:\n\n"
    for key in PAID_TARIFFS:
        t = TARIFFS[key]
        price = get_price_with_discount(key, user_id)
        text += f"• **{t['name']}** — {price} ₽/мес ({t['leads']} лидов)\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=tariffs_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Инструкция:**\n\n"
        "1. Купите тариф (/tariffs)\n"
        "2. Создайте бота у @BotFather\n"
        "3. Вставьте токен (кнопка «Активировать»)\n"
        "4. Настройте бота в панели\n\n"
        "**Команды:**\n"
        "/tariffs — тарифы\n"
        "/start — меню\n"
        "/help — справка"
    )

# ========================================================================
# CALLBACK
# ========================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    user_data = db_users.get(user_id)

    # ===== ВЫБОР ЯЗЫКА =====
    if data.startswith("lang_"):
        lang = data.replace("lang_", "")
        if lang in LANGUAGES:
            db_users.update(user_id, language=lang, language_chosen=True)
            await query.message.edit_text(
                f"✅ Язык: **{LANGUAGES[lang]}**\n\n"
                "🤖 Добро пожаловать в AI-менеджер!\n\n"
                "📦 Тарифы: /tariffs\n📖 Инструкция: /help",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id)
            )
        return

    # ===== ПРОБНЫЙ ПЕРИОД (ДО startswith!) =====
    if data == "tariff_trial":
        if user_data and user_data.get("tariff") not in (None, "trial", "expired"):
            await query.message.edit_text("❌ Пробный период доступен только новым пользователям.")
            return

        db_users.update(
            user_id,
            tariff="trial",
            tariff_expires=(datetime.now() + timedelta(days=2)).isoformat(),
            is_bot_active=True
        )
        await query.message.edit_text("🎁 **Пробный период активирован!**\n\n2 дня бесплатно.")
        return

    # ===== ПОКУПКА ТАРИФА =====
    if data == "buy":
        await query.message.edit_text("Выберите тариф:", reply_markup=tariffs_keyboard())
        return

    if data.startswith("tariff_"):
        tariff_key = data.replace("tariff_", "")
        if tariff_key not in PAID_TARIFFS:
            await query.message.edit_text("❌ Неверный тариф.")
            return
        price = get_price_with_discount(tariff_key, user_id)
        await query.message.edit_text(
            f"Тариф: **{TARIFFS[tariff_key]['name']}**\n"
            f"Стоимость: {price} ₽\n\n"
            f"💳 Переведите {price} ₽ на карту **1234 5678 9012 3456**\n"
            f"После оплаты напишите /confirm_payment",
            parse_mode="Markdown"
        )
        return

    # ===== АКТИВАЦИЯ БОТА =====
    if data == "add_token":
        context.user_data["awaiting_token"] = True
        await query.message.edit_text("🔑 Отправьте токен вашего бота от @BotFather:")
        return

    # ===== ПАНЕЛЬ УПРАВЛЕНИЯ =====
    if data == "client_panel":
        if not await check_subscription(update, context):
            await query.message.edit_text("🔔 Подпишитесь на канал и нажмите /start.")
            return
        await query.message.edit_text(
            "⚙️ **Управление ботом**",
            reply_markup=client_panel_keyboard()
        )
        return

    # ===== ЗАГОЛОВКИ =====
    headers = {
        "header_personality": "🔥 **НАСТРОЙКА ЛИЧНОСТИ БОТА**\n\nСкрипт, товары, RAG, FAQ, лиды, кастомные поля.",
        "header_communication": "🎤 **КОММУНИКАЦИИ**\n\nГолос, шаблоны, языки.",
        "header_team": "👥 **КОМАНДА**\n\nМенеджеры и геймификация.",
        "header_integrations": "📊 **ИНТЕГРАЦИИ**\n\nCRM и мульти-бот.",
        "header_management": "⚙️ **УПРАВЛЕНИЕ**\n\nВкл/выкл, расписание, экспорт."
    }
    if data in headers:
        await query.message.edit_text(headers[data], reply_markup=client_panel_keyboard())
        return

    # ===== СКРИПТ / ТОВАРЫ =====
    if data == "bot_settings":
        s = (user_data or {}).get("bot_settings", {})
        script_preview = (s.get("script") or "Не настроен")[:80]
        await query.message.edit_text(
            f"⚙️ **Скрипт и товары**\n\n"
            f"📝 Скрипт: {script_preview}...\n"
            f"📦 Товаров: {len(s.get('products', []))}\n\n"
            "**Команды:**\n"
            "`/set_script <текст>`\n"
            "`/set_products <товар1, товар2>`\n"
            "`/show_script`\n"
            "`/reset_script`",
            parse_mode="Markdown"
        )
        return

    if data == "show_products":
        products = (user_data or {}).get("bot_settings", {}).get("products", [])
        if products:
            text = "📦 **Товары:**\n\n" + "\n".join([f"{i+1}. {p}" for i, p in enumerate(products)])
            text += "\n\n`/add_product <название>`\n`/remove_product <номер>`\n`/clear_products`"
        else:
            text = "📦 Товаров нет.\n\nДобавьте: `/add_product <название>`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== RAG =====
    if data == "rag_settings":
        rag_count = len((user_data or {}).get("rag_data", []))
        await query.message.edit_text(
            f"📚 **RAG (База знаний)**\n\n"
            f"📄 Документов: {rag_count}\n\n"
            "**Команды:**\n"
            "`/rag_add <текст>`\n"
            "`/rag_list`\n"
            "`/rag_clear`"
        )
        return

    # ===== FAQ =====
    if data == "faq_settings":
        faq = (user_data or {}).get("bot_settings", {}).get("faq", [])
        text = f"❓ **FAQ** ({len(faq)} вопросов)\n\n"
        for i, item in enumerate(faq[:5], 1):
            text += f"{i}. {item.get('question', '')[:40]}\n"
        text += "\n**Команды:**\n`/set_faq вопрос|ответ; ...`\n`/show_faq`\n`/remove_faq <номер>`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== ЛИДЫ =====
    if data == "lead_settings":
        ls = (user_data or {}).get("lead_settings", {})
        await query.message.edit_text(
            f"🎯 **Настройка лидов**\n\n"
            f"💰 Бюджет: {ls.get('budget_threshold', 0)} ₽\n"
            f"📅 Срок: {ls.get('deadline_days', 0)} дн.\n"
            f"⚡ Правило: {ls.get('rule', 'AND')}\n\n"
            "**Команды:**\n"
            "`/set_budget 50000`\n"
            "`/set_deadline 7`\n"
            "`/set_rule AND`\n"
            "`/show_lead_settings`"
        )
        return

    # ===== КАСТОМНЫЕ ПОЛЯ =====
    if data == "custom_fields_settings":
        fields = (user_data or {}).get("custom_fields", [])
        text = f"📝 **Кастомные поля** ({len(fields)})\n\n"
        for i, f in enumerate(fields, 1):
            text += f"{i}. {f.get('name', '')} ({f.get('type', 'text')})\n"
        text += "\n**Команды:**\n`/add_field <название>`\n`/remove_field <название>`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== ГОЛОС =====
    if data == "voice_settings":
        s = (user_data or {}).get("settings", {})
        enabled = s.get("voice_enabled", True)
        await query.message.edit_text(
            f"🎤 **Голосовые**\n\n"
            f"🔊 Статус: {'✅ Включены' if enabled else '❌ Выключены'}\n\n"
            "**Команды:**\n`/voice_on`\n`/voice_off`\n`/set_voice_error <текст>`"
        )
        return

    # ===== ШАБЛОНЫ =====
    if data == "template_settings":
        s = (user_data or {}).get("bot_settings", {})
        texts = s.get("auto_reply_texts", [])
        timeout = s.get("auto_reply_timeout", 300)
        await query.message.edit_text(
            f"💬 **Шаблоны**\n\n"
            f"📌 Шаблонов: {len(texts)}\n"
            f"⏱️ Таймаут: {timeout} сек.\n\n"
            "**Команды:**\n`/set_auto_reply текст1; текст2`\n`/set_timeout 300`"
        )
        return

    # ===== ЯЗЫКИ =====
    if data == "language_settings":
        current = (user_data or {}).get("language", "ru")
        keyboard = [[InlineKeyboardButton(
            f"{'✅ ' if code == current else ''}{name}",
            callback_data=f"set_lang_{code}"
        )] for code, name in LANGUAGES.items()]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="client_panel")])
        await query.message.edit_text(
            "🌍 **Выберите язык:**",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("set_lang_"):
        lang = data.replace("set_lang_", "")
        if lang in LANGUAGES:
            db_users.update(user_id, language=lang, language_chosen=True)
            await query.message.edit_text(
                f"✅ Язык: {LANGUAGES[lang]}",
                reply_markup=client_panel_keyboard()
            )
        return

    # ===== МЕНЕДЖЕРЫ =====
    if data == "manager_settings":
        managers = (user_data or {}).get("managers", [])
        text = f"👥 **Менеджеры** ({len(managers)})\n\n"
        for m in managers:
            emoji = "⭐" if m.get("level") == "senior" else "🔹"
            text += f"{emoji} `{m.get('id')}` — {m.get('level', 'junior')}\n"
        text += "\n**Команды:**\n`/add_manager <id> junior/senior`\n`/remove_manager <id>`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== ГЕЙМИФИКАЦИЯ =====
    if data == "gamification_settings":
        stats = (user_data or {}).get("managers_stats", {})
        if stats:
            sorted_m = sorted(stats.items(), key=lambda x: x[1].get("xp", 0), reverse=True)
            text = "🏆 **ТОП МЕНЕДЖЕРОВ**\n\n"
            for i, (m_id, s) in enumerate(sorted_m[:10]):
                emoji = ["🥇", "🥈", "🥉"][i] if i < 3 else f"#{i+1}"
                text += f"{emoji} `{m_id}` — {s.get('xp', 0)} XP\n"
        else:
            text = "🏆 Пока нет данных."
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== CRM =====
    if data == "crm_settings":
        crm = (user_data or {}).get("settings", {}).get("crm_integration", {})
        status = "🟢 Включена" if crm.get("enabled") else "🔴 Отключена"
        await query.message.edit_text(
            f"📊 **CRM Интеграция**\n\n"
            f"Статус: {status}\n"
            f"Система: {crm.get('type', 'Не выбрана')}\n\n"
            "**Команды:**\n`/crm_setup`\n`/crm_status`\n`/crm_disable`"
        )
        return

    # ===== МУЛЬТИ-БОТ =====
    if data == "multi_bot_settings":
        bots = (user_data or {}).get("bots", [])
        tariff = (user_data or {}).get("tariff", "trial")
        limit = BOT_LIMITS.get(tariff, 1)
        text = f"🤖 **Мульти-бот** ({len(bots)}/{limit})\n\n"
        for b in bots:
            text += f"• {b.get('name', '—')} — {'✅' if b.get('is_active') else '❌'}\n"
        text += "\n**Команды:**\n`/add_bot <название> <токен>`\n`/remove_bot <название>`\n`/bots_list`"
        await query.message.edit_text(text, parse_mode="Markdown")
        return

    # ===== УПРАВЛЕНИЕ =====
    if data == "bot_toggle":
        new_status = not (user_data or {}).get("is_bot_active", True)
        db_users.update(user_id, is_bot_active=new_status)
        await query.message.edit_text(
            f"🔄 Бот {'включён ✅' if new_status else 'выключен ❌'}"
        )
        return

    if data == "schedule_settings":
        schedule = (user_data or {}).get("schedules", {})
        await query.message.edit_text(
            f"🕐 **Расписание**\n\n"
            f"Статус: {'✅ Включено' if schedule.get('enabled') else '❌ Выключено'}\n"
            f"Время: {schedule.get('start_time', '09:00')} - {schedule.get('end_time', '21:00')}\n\n"
            "**Команды:**\n`/schedule_on`\n`/schedule_off`\n`/set_schedule 09:00-21:00`"
        )
        return

    if data == "export_stats":
        await query.message.edit_text(
            "📊 **Экспорт статистики**\n\n"
            "**Команды:**\n"
            "`/export day excel`\n"
            "`/export week csv`\n"
            "`/export month csv`"
        )
        return

    # ===== СТАТИСТИКА =====
    if data == "stats":
        if not user_data:
            await query.message.edit_text("❌ Сначала /start")
            return
        total_leads = db_leads.count_by_client(user_id)
        hot_leads = db_leads.count_hot_by_client(user_id)
        await query.message.edit_text(
            f"📊 **Статистика**\n\n"
            f"🤖 Бот: {'✅' if user_data.get('bot_token') else '❌'}\n"
            f"📦 Тариф: {user_data.get('tariff', 'trial')}\n"
            f"👥 Менеджеров: {len(user_data.get('managers', []))}\n"
            f"📈 Лидов: {total_leads}\n"
            f"🔥 Горячих: {hot_leads}\n"
            f"📚 RAG: {len(user_data.get('rag_data', []))}",
            parse_mode="Markdown"
        )
        return

    if data == "support":
        await query.message.edit_text(
            "💬 **Поддержка**\n\n"
            "Напишите `/support_chat` чтобы связаться с разработчиком."
        )
        return

    if data == "suggest_idea":
        context.user_data["awaiting_idea"] = True
        await query.message.edit_text("💡 Напишите вашу идею:")
        return

    if data == "help":
        await help_command(update, context)
        return

    if data == "back":
        context.user_data.clear()
        await query.message.edit_text(
            "Главное меню:",
            reply_markup=main_menu_keyboard(user_id)
        )
        return

    await query.message.edit_text("⚠️ Команда не найдена.")

# ========================================================================
# HANDLE_MESSAGE
# ========================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if not text:
        return

    user_data = db_users.get(user_id)
    if not user_data:
        db_users.create_default(
            user_id,
            update.effective_user.username or "Unknown",
            update.effective_user.first_name or "User"
        )
        user_data = db_users.get(user_id)

    if user_data.get("is_banned"):
        await update.message.reply_text("⛔ Вы забанены.")
        return

    # ===== АКТИВАЦИЯ ТОКЕНА =====
    if context.user_data.get("awaiting_token"):
        context.user_data["awaiting_token"] = False
        if ":" in text and len(text) > 20:
            db_users.update(user_id, bot_token=text)
            if activate_client_bot(user_id, text):
                await update.message.reply_text(
                    "✅ **Бот активирован!**\n\nAI-менеджер запущен."
                )
            else:
                await update.message.reply_text(
                    "❌ Не удалось запустить агента. Проверьте логи."
                )
        else:
            await update.message.reply_text("❌ Неверный формат токена.")
        return

    # ===== ПОДДЕРЖКА =====
    if context.user_data.get("awaiting_support"):
        context.user_data["awaiting_support"] = False
        reqs = user_data.get("support_requests", [])
        reqs.append({
            "message": text,
            "status": "open",
            "created_at": datetime.now().isoformat(),
            "user_id": user_id
        })
        db_users.update(user_id, support_requests=reqs)
        await update.message.reply_text("✅ Отправлено в поддержку!")
        return

    # ===== ИДЕИ =====
    if context.user_data.get("awaiting_idea"):
        context.user_data["awaiting_idea"] = False
        ideas = user_data.get("ideas", [])
        ideas.append({
            "text": text,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "user_id": user_id
        })
        db_users.update(user_id, ideas=ideas)
        await update.message.reply_text("✅ Спасибо за идею!")
        return

    # ===== СКРИПТ =====
    if text.startswith("/set_script"):
        script = text.replace("/set_script", "").strip()
        if script:
            s = user_data.get("bot_settings", {})
            s["script"] = script
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text("✅ Скрипт обновлён!")
        return

    if text == "/show_script":
        s = user_data.get("bot_settings", {})
        await update.message.reply_text(f"📝 {s.get('script', 'Не установлен')}")
        return

    if text == "/reset_script":
        s = user_data.get("bot_settings", {})
        s["script"] = "Ты — профессиональный менеджер по продажам."
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Скрипт сброшен.")
        return

    # ===== ТОВАРЫ =====
    if text.startswith("/set_products"):
        products = [
            p.strip() for p in text.replace("/set_products", "").split(",") if p.strip()
        ]
        if products:
            s = user_data.get("bot_settings", {})
            s["products"] = products
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Товары: {', '.join(products)}")
        return

    if text.startswith("/add_product"):
        product = text.replace("/add_product", "").strip()
        if product:
            s = user_data.get("bot_settings", {})
            prods = s.get("products", [])
            prods.append(product)
            s["products"] = prods
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Добавлен: {product}")
        return

    if text == "/show_products":
        products = user_data.get("bot_settings", {}).get("products", [])
        if products:
            out = "📦 **Товары:**\n\n" + "\n".join([f"{i+1}. {p}" for i, p in enumerate(products)])
        else:
            out = "📦 Список пуст."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if text.startswith("/remove_product"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            s = user_data.get("bot_settings", {})
            products = s.get("products", [])
            idx = int(parts[1]) - 1
            if 0 <= idx < len(products):
                removed = products.pop(idx)
                s["products"] = products
                db_users.update(user_id, bot_settings=s)
                await update.message.reply_text(f"✅ Удалён: {removed}")
            else:
                await update.message.reply_text("❌ Неверный номер.")
        return

    if text == "/clear_products":
        s = user_data.get("bot_settings", {})
        s["products"] = []
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Все товары удалены.")
        return

    # ===== RAG =====
    if text.startswith("/rag_add"):
        rag_text = text.replace("/rag_add", "").strip()
        if rag_text:
            rag_data = user_data.get("rag_data", [])
            rag_data.append(rag_text)
            db_users.update(user_id, rag_data=rag_data)
            await update.message.reply_text(f"✅ Добавлено ({len(rag_data)} документов).")
        return

    if text == "/rag_list":
        rag_data = user_data.get("rag_data", [])
        await update.message.reply_text(f"📚 В RAG: {len(rag_data)} документов.")
        return

    if text == "/rag_clear":
        db_users.update(user_id, rag_data=[])
        await update.message.reply_text("✅ RAG очищена.")
        return

    # ===== FAQ =====
    if text.startswith("/set_faq"):
        pairs = text.replace("/set_faq", "").split(";")
        faq = []
        for pair in pairs:
            if "|" in pair:
                q, a = pair.split("|", 1)
                faq.append({"question": q.strip(), "answer": a.strip()})
        if faq:
            s = user_data.get("bot_settings", {})
            s["faq"] = faq
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ FAQ: {len(faq)} вопросов.")
        return

    if text == "/show_faq":
        faq = user_data.get("bot_settings", {}).get("faq", [])
        if faq:
            out = "❓ **FAQ:**\n\n" + "\n".join(
                [f"{i+1}. {f['question']}\n   💬 {f['answer']}" for i, f in enumerate(faq)]
            )
        else:
            out = "❓ FAQ пуст."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if text.startswith("/remove_faq"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            s = user_data.get("bot_settings", {})
            faq = s.get("faq", [])
            idx = int(parts[1]) - 1
            if 0 <= idx < len(faq):
                removed = faq.pop(idx)
                s["faq"] = faq
                db_users.update(user_id, bot_settings=s)
                await update.message.reply_text(f"✅ Удалён: {removed['question']}")
        return

    # ===== ЛИДЫ =====
    if text.startswith("/set_budget"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            ls = user_data.get("lead_settings", {})
            ls["budget_threshold"] = int(parts[1])
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Бюджет: {parts[1]} ₽")
        return

    if text.startswith("/set_deadline"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            ls = user_data.get("lead_settings", {})
            ls["deadline_days"] = int(parts[1])
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Срок: {parts[1]} дней")
        return

    if text.startswith("/set_rule"):
        parts = text.split()
        if len(parts) == 2 and parts[1].upper() in ("AND", "OR"):
            ls = user_data.get("lead_settings", {})
            ls["rule"] = parts[1].upper()
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Правило: {parts[1].upper()}")
        return

    if text == "/show_lead_settings":
        ls = user_data.get("lead_settings", {})
        await update.message.reply_text(
            f"🎯 **Настройки лидов**\n\n"
            f"💰 Бюджет: {ls.get('budget_threshold', 0)} ₽\n"
            f"📅 Срок: {ls.get('deadline_days', 0)} дн.\n"
            f"⚡ Правило: {ls.get('rule', 'AND')}",
            parse_mode="Markdown"
        )
        return

    # ===== КАСТОМНЫЕ ПОЛЯ =====
    if text.startswith("/add_field"):
        field_name = text.replace("/add_field", "").strip()
        if field_name:
            fields = user_data.get("custom_fields", [])
            if not any(f.get("name") == field_name for f in fields):
                fields.append({"name": field_name, "type": "text", "required": False})
                db_users.update(user_id, custom_fields=fields)
                await update.message.reply_text(f"✅ Поле добавлено: {field_name}")
            else:
                await update.message.reply_text("❌ Поле уже существует.")
        return

    if text.startswith("/remove_field"):
        field_name = text.replace("/remove_field", "").strip()
        if field_name:
            fields = [
                f for f in user_data.get("custom_fields", [])
                if f.get("name") != field_name
            ]
            db_users.update(user_id, custom_fields=fields)
            await update.message.reply_text(f"✅ Поле удалено: {field_name}")
        return

    # ===== МЕНЕДЖЕРЫ =====
    if text.startswith("/add_manager"):
        parts = text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            manager_id = int(parts[1])
            level = parts[2] if len(parts) > 2 and parts[2] in ("junior", "senior") else "junior"
            managers = user_data.get("managers", [])
            if not any(m.get("id") == manager_id for m in managers):
                managers.append({"id": manager_id, "level": level})
                db_users.update(user_id, managers=managers)
                await update.message.reply_text(f"✅ Менеджер {manager_id} добавлен ({level})")
            else:
                await update.message.reply_text("❌ Уже добавлен.")
        return

    if text.startswith("/remove_manager"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            manager_id = int(parts[1])
            managers = [
                m for m in user_data.get("managers", [])
                if m.get("id") != manager_id
            ]
            db_users.update(user_id, managers=managers)
            await update.message.reply_text("✅ Удалён.")
        return

    if text == "/managers_list":
        managers = user_data.get("managers", [])
        if managers:
            out = "👥 **Менеджеры:**\n\n" + "\n".join(
                [f"• {m.get('id')} — {m.get('level', 'junior')}" for m in managers]
            )
        else:
            out = "👥 Нет менеджеров."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    # ===== ГОЛОС =====
    if text == "/voice_on":
        s = user_data.get("settings", {})
        s["voice_enabled"] = True
        db_users.update(user_id, settings=s)
        await update.message.reply_text("✅ Голосовые включены.")
        return

    if text == "/voice_off":
        s = user_data.get("settings", {})
        s["voice_enabled"] = False
        db_users.update(user_id, settings=s)
        await update.message.reply_text("❌ Голосовые выключены.")
        return

    if text.startswith("/set_voice_error"):
        error_text = text.replace("/set_voice_error", "").strip()
        if error_text:
            s = user_data.get("bot_settings", {})
            s["voice_error_text"] = error_text
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text("✅ Текст ошибки обновлён.")
        return

    # ===== ШАБЛОНЫ =====
    if text.startswith("/set_auto_reply"):
        texts = [
            t.strip() for t in text.replace("/set_auto_reply", "").split(";") if t.strip()
        ]
        if texts:
            s = user_data.get("bot_settings", {})
            s["auto_reply_texts"] = texts
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Установлено {len(texts)} шаблонов.")
        return

    if text.startswith("/set_timeout"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            timeout = int(parts[1])
            if timeout >= 10:
                s = user_data.get("bot_settings", {})
                s["auto_reply_timeout"] = timeout
                db_users.update(user_id, bot_settings=s)
                await update.message.reply_text(f"✅ Таймаут: {timeout} сек.")
        return

    # ===== МУЛЬТИ-БОТ =====
    if text.startswith("/add_bot"):
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            bots = user_data.get("bots", [])
            tariff = user_data.get("tariff", "trial")
            limit = BOT_LIMITS.get(tariff, 1)
            if len(bots) < limit:
                bots.append({"name": parts[1], "token": parts[2], "is_active": True})
                db_users.update(user_id, bots=bots)
                await update.message.reply_text(f"✅ Бот {parts[1]} добавлен.")
            else:
                await update.message.reply_text(
                    f"❌ Лимит: {limit} ботов для тарифа {tariff}."
                )
        return

    if text == "/bots_list":
        bots = user_data.get("bots", [])
        if bots:
            out = "🤖 **Боты:**\n\n" + "\n".join([f"• {b.get('name', '—')}" for b in bots])
        else:
            out = "🤖 Нет ботов."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if text.startswith("/remove_bot"):
        bot_name = text.replace("/remove_bot", "").strip()
        if bot_name:
            bots = [
                b for b in user_data.get("bots", [])
                if b.get("name") != bot_name
            ]
            db_users.update(user_id, bots=bots)
            await update.message.reply_text(f"✅ Удалён: {bot_name}")
        return

    # ===== CRM =====
    if text == "/crm_setup":
        await update.message.reply_text(
            "📊 **Настройка CRM**\n\n"
            "AmoCRM: `/crm_token <субдомен> <токен>`\n"
            "Bitrix24: `/crm_token <webhook> <user_id>`\n"
            "HubSpot: `/crm_token <api_key>`"
        )
        return

    if text.startswith("/crm_token"):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            s = user_data.get("settings", {})
            s["crm_integration"] = {
                "enabled": True,
                "type": "amocrm",
                "subdomain": parts[1],
                "access_token": parts[2]
            }
            db_users.update(user_id, settings=s)
            await update.message.reply_text("✅ CRM настроена!")
        return

    if text == "/crm_status":
        crm = user_data.get("settings", {}).get("crm_integration", {})
        status = "✅ Включена" if crm.get("enabled") else "❌ Отключена"
        await update.message.reply_text(
            f"📊 CRM: {status}\nСистема: {crm.get('type', 'Не выбрана')}"
        )
        return

    if text == "/crm_disable":
        s = user_data.get("settings", {})
        if "crm_integration" in s:
            s["crm_integration"]["enabled"] = False
            db_users.update(user_id, settings=s)
        await update.message.reply_text("✅ CRM отключена.")
        return

    # ===== РАСПИСАНИЕ =====
    if text == "/schedule_on":
        s = user_data.get("schedules", {})
        s["enabled"] = True
        db_users.update(user_id, schedules=s)
        await update.message.reply_text("✅ Расписание включено.")
        return

    if text == "/schedule_off":
        s = user_data.get("schedules", {})
        s["enabled"] = False
        db_users.update(user_id, schedules=s)
        await update.message.reply_text("❌ Расписание выключено.")
        return

    if text.startswith("/set_schedule"):
        parts = text.replace("/set_schedule", "").strip().split("-")
        if len(parts) == 2:
            s = user_data.get("schedules", {})
            s["start_time"] = parts[0].strip()
            s["end_time"] = parts[1].strip()
            db_users.update(user_id, schedules=s)
            await update.message.reply_text(f"✅ Расписание: {parts[0]} - {parts[1]}")
        return

    # ===== ПОДДЕРЖКА / ИДЕИ =====
    if text == "/support_chat":
        context.user_data["awaiting_support"] = True
        await update.message.reply_text("📨 Напишите сообщение в поддержку:")
        return

    if text == "/suggest_idea":
        context.user_data["awaiting_idea"] = True
        await update.message.reply_text("💡 Напишите вашу идею:")
        return

    # ===== ПОДТВЕРЖДЕНИЕ ОПЛАТЫ =====
    if text == "/confirm_payment":
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💰 Пользователь `{user_id}` подтвердил оплату.\nПроверьте поступление.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
        await update.message.reply_text(
            "📩 Заявка отправлена. Менеджер проверит поступление в течение 24 часов."
        )
        return

    # ===== ЭКСПОРТ =====
    if text.startswith("/export"):
        parts = text.split()
        if len(parts) >= 2:
            period = parts[1]
            fmt = parts[2] if len(parts) > 2 else "csv"
            if period not in ("day", "week", "month"):
                await update.message.reply_text("❌ Период: day, week, month")
                return
            if fmt not in ("excel", "csv", "pdf"):
                await update.message.reply_text("❌ Формат: excel, csv, pdf")
                return

            stats = db_stats.get_client_stats(user_id, period)
            if not stats:
                await update.message.reply_text("📊 Нет данных за этот период.")
                return

            csv_lines = ["date,leads,hot"]
            for s in stats:
                csv_lines.append(f"{s['date']},{s['leads_count']},{s['hot_leads_count']}")
            csv_data = "\n".join(csv_lines)

            await update.message.reply_document(
                document=csv_data.encode("utf-8"),
                filename=f"stats_{period}.csv",
                caption=f"📊 Статистика за {period}"
            )
        return

    await update.message.reply_text("Используйте меню или /help.")

# ========================================================================
# ФОНОВЫЕ ЗАДАЧИ
# ========================================================================

async def check_expired_tariffs(context: ContextTypes.DEFAULT_TYPE):
    """Отключает истёкшие тарифы и уведомляет клиентов."""
    expired = db_users.get_expired_tariffs()
    for user in expired:
        uid = user["user_id"]
        if is_admin(uid):
            continue
        if user.get("tariff") == "expired":
            continue

        db_users.update(uid, is_bot_active=False, tariff="expired")

        try:
            await context.bot.send_message(
                chat_id=uid,
                text="⏳ **Ваш тариф истек.** Оплатите: /tariffs",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления {uid}: {e}")

async def process_notifications(context: ContextTypes.DEFAULT_TYPE):
    """Рассылает уведомления из таблицы notifications."""
    notifs = db_notifications.get_unsent()
    for n in notifs:
        try:
            await context.bot.send_message(
                chat_id=n["recipient_id"],
                text=n["message"],
                parse_mode="Markdown"
            )
            db_notifications.mark_sent(n["id"])
        except Forbidden:
            # Пользователь заблокировал бота — помечаем, чтобы не спамить
            logger.warning(f"Уведомление {n['id']}: пользователь {n['recipient_id']} заблокировал бота")
            db_notifications.mark_sent(n["id"])
        except BadRequest as e:
            logger.error(f"Уведомление {n['id']}: BadRequest: {e}")
            db_notifications.mark_sent(n["id"])
        except Exception as e:
            # Сетевая ошибка — НЕ помечаем, попробуем позже
            logger.error(f"Уведомление {n['id']}: {e}")

async def cleanup_notifications(context: ContextTypes.DEFAULT_TYPE):
    db_notifications.cleanup_old(days=7)

# ========================================================================
# MAIN
# ========================================================================

def main():
    if not is_configured():
        logger.error("❌ Конфиг не заполнен! Проверь .env")
        return

    init_db()

    app = Application.builder().token(MAIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tariffs", tariffs))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    if app.job_queue:
        app.job_queue.run_repeating(check_expired_tariffs, interval=3600, first=60)
        app.job_queue.run_repeating(process_notifications, interval=5, first=10)
        app.job_queue.run_repeating(cleanup_notifications, interval=86400, first=300)

    # Восстанавливаем агентов при старте
    restore_agents()

    logger.info("🤖 Главный бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()