# bot_main.py
"""
ГЛАВНЫЙ БОТ.
- Продажа тарифов (Telegram Stars + карта)
- Активация агентов, восстановление при старте
- Управление настройками клиента
- Очередь уведомлений от bot_agent
- Еженедельный отчёт с инсайтами и графиком
- CRM-интеграция
- Google Sheets + Excel/PDF/CSV экспорт
"""

import os
import sys
import json
import logging
import asyncio
import aiohttp
import matplotlib.pyplot as plt
from io import BytesIO
from collections import Counter
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputFile
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)
from telegram.error import Forbidden, BadRequest

from config import (
    MAIN_BOT_TOKEN, ADMIN_IDS, CHANNEL_URL, channel_is_configured,
    TARIFFS, TARIFFS_STARS, PAID_TARIFFS, BOT_LIMITS, LANGUAGES, is_configured
)
import crm_client
from database import (
    init_db, db_users, db_leads, db_stats, db_notifications,
    db_payments, db_crm_log
)
from agent_runner import (
    validate_bot_token, kill_existing_agent, activate_client_bot, restore_agents
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

def get_stars_price(tariff_key: str, user_id=None) -> int:
    base = TARIFFS_STARS.get(tariff_key, 0)
    if base == 0:
        return 0
    discount = 0.0
    if user_id:
        discount = get_referral_discount(user_id)
    discount = min(discount, 0.40)
    return max(1, int(round(base * (1 - discount))))

def _calc_new_expiry(current_expiry, days: int) -> str:
    now = datetime.now()
    start = now
    if current_expiry:
        try:
            current = datetime.fromisoformat(current_expiry)
            if current > now:
                start = current
        except (ValueError, TypeError):
            pass
    return (start + timedelta(days=days)).isoformat()

def _ab_available(user_data: dict) -> bool:
    return (user_data or {}).get("tariff") in PAID_TARIFFS

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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
# ХЕЛПЕРЫ РЕАНИМАТОРА
# ========================================================================

DEFAULT_FOLLOWUP_INTERVALS = [3600, 86400, 259200]

def _fmt_duration(seconds: int) -> str:
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return "?"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} ч" + (f" {m} мин" if m else "")
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d} дн" + (f" {h} ч" if h else "")

def _get_followup_intervals(user_data: dict) -> list:
    bs = (user_data or {}).get("bot_settings") or {}
    raw = bs.get("followup_intervals")
    if isinstance(raw, list) and raw:
        result = []
        for v in raw:
            try:
                n = int(v)
                if n >= 60:
                    result.append(n)
            except (ValueError, TypeError):
                continue
        if result:
            return result
    return list(DEFAULT_FOLLOWUP_INTERVALS)

# ========================================================================
# КЛАВИАТУРЫ
# ========================================================================

def language_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"lang_{code}")]
        for code, name in LANGUAGES.items()
    ])

def main_menu_keyboard(user_id=None):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Купить AI-менеджера", callback_data="buy")],
        [InlineKeyboardButton("🔑 Активировать бота", callback_data="add_token")],
        [InlineKeyboardButton("⚙️ Управление ботом", callback_data="client_panel")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🏆 Топ-лидов", callback_data="top_leads")],
        [InlineKeyboardButton("💳 Мои платежи", callback_data="my_payments")],
        [InlineKeyboardButton("❓ Инструкция", callback_data="help")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")],
        [InlineKeyboardButton("💡 Предложить идею", callback_data="suggest_idea")],
    ])

def client_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Настройка личности", callback_data="menu_personality")],
        [InlineKeyboardButton("🎤 Коммуникации", callback_data="menu_communication")],
        [InlineKeyboardButton("👥 Команда и геймификация", callback_data="menu_team")],
        [InlineKeyboardButton("📊 Интеграции (CRM, боты)", callback_data="menu_integrations")],
        [InlineKeyboardButton("⚙️ Управление ботом", callback_data="menu_management")],
        [InlineKeyboardButton("🔙 В главное меню", callback_data="back")]
    ])

def tariffs_keyboard(user_id=None):
    keyboard = []
    for key in PAID_TARIFFS:
        t = TARIFFS[key]
        price_rub = get_price_with_discount(key, user_id)
        price_stars = get_stars_price(key, user_id)
        keyboard.append([InlineKeyboardButton(
            f"{t['name']} — {price_rub} ₽ / {price_stars} ⭐",
            callback_data=f"tariff_{key}"
        )])
    keyboard.append([InlineKeyboardButton("🎁 Пробный период 2 дня", callback_data="tariff_trial")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)

def tariff_payment_keyboard(tariff_key: str, user_id):
    price_stars = get_stars_price(tariff_key, user_id)
    price_rub = get_price_with_discount(tariff_key, user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⭐ Оплатить {price_stars} звёздами",
            callback_data=f"pay_stars_{tariff_key}"
        )],
        [InlineKeyboardButton(
            f"💳 Картой ({price_rub} ₽)",
            callback_data=f"pay_card_{tariff_key}"
        )],
        [InlineKeyboardButton("🔙 Назад", callback_data="buy")],
    ])

# ========================================================================
# КОМАНДЫ
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
        price_rub = get_price_with_discount(key, user_id)
        price_stars = get_stars_price(key, user_id)
        text += f"• **{t['name']}** — {price_rub} ₽ или {price_stars} ⭐ ({t['leads']} лидов)\n"
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=tariffs_keyboard(user_id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Инструкция:**\n\n"
        "1. Купите тариф (/tariffs) — звёздами или картой\n"
        "2. Создайте бота у @BotFather\n"
        "3. Вставьте токен (кнопка «Активировать»)\n"
        "4. Настройте бота в панели\n\n"
        "**Команды:**\n"
        "/tariffs — тарифы\n"
        "/payments — история платежей\n"
        "/start — меню\n"
        "/help — справка"
    )

async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = db_payments.get_by_user(user_id, limit=20)
    if not history:
        await update.message.reply_text(
            "💳 У вас пока нет платежей.\n\nКупить тариф: /tariffs"
        )
        return

    total = sum(p["amount_stars"] for p in history)
    text = f"💳 **Ваши платежи** ({len(history)})\n\n"
    for p in history:
        date = (p.get("created_at") or "")[:10]
        tariff_name = TARIFFS.get(p["tariff"], {}).get("name", p["tariff"])
        text += f"• {date} — {tariff_name} — {p['amount_stars']} ⭐\n"
    text += f"\n💰 Всего: {total} ⭐"
    await update.message.reply_text(text, parse_mode="Markdown")

# ========================================================================
# TELEGRAM STARS
# ========================================================================

async def send_stars_invoice(context, user_id: int, tariff_key: str):
    t = TARIFFS[tariff_key]
    stars = get_stars_price(tariff_key, user_id)

    payload = json.dumps({"type": "tariff", "tariff": tariff_key, "user": user_id})

    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title=f"AI-менеджер — {t['name']}",
            description=(
                f"Тариф «{t['name']}» на {t['days']} дней. "
                f"Лимит: {t['leads']} лидов. "
                f"AI-агент для квалификации лидов в Telegram."
            ),
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"Тариф {t['name']}", amount=stars)],
        )
        logger.info(f"[{user_id}] Счёт Stars: {tariff_key} = {stars} ⭐")
    except Exception as e:
        logger.error(f"[{user_id}] Ошибка send_invoice: {e}", exc_info=True)
        raise

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query

    try:
        payload = json.loads(q.invoice_payload)
    except (json.JSONDecodeError, TypeError):
        await q.answer(ok=False, error_message="Некорректный запрос.")
        logger.warning(f"Bad pre_checkout payload: {q.invoice_payload}")
        return

    if payload.get("type") != "tariff":
        await q.answer(ok=False, error_message="Неизвестный товар.")
        return

    tariff_key = payload.get("tariff")
    if tariff_key not in PAID_TARIFFS:
        await q.answer(ok=False, error_message="Неизвестный тариф.")
        return

    buyer_id = q.from_user.id
    user_data = db_users.get(buyer_id)
    if not user_data:
        await q.answer(ok=False, error_message="Пользователь не найден.")
        return
    if user_data.get("is_banned"):
        await q.answer(ok=False, error_message="Доступ запрещён.")
        return

    expected_stars = get_stars_price(tariff_key, buyer_id)
    if q.total_amount != expected_stars:
        await q.answer(ok=False, error_message="Неверная сумма. Попробуйте снова.")
        return

    await q.answer(ok=True)

async def payment_success_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    user_id = update.effective_user.id

    try:
        payload = json.loads(sp.invoice_payload)
    except (json.JSONDecodeError, TypeError):
        logger.error(f"Bad successful_payment payload: {sp.invoice_payload}")
        return

    tariff_key = payload.get("tariff")
    if tariff_key not in PAID_TARIFFS:
        logger.error(f"Unknown tariff in successful_payment: {tariff_key}")
        return

    charge_id = sp.telegram_payment_charge_id

    if db_payments.exists_charge(charge_id):
        logger.info(f"Duplicate charge {charge_id}, already processed")
        return

    user_data = db_users.get(user_id) or {}
    days = TARIFFS[tariff_key]["days"]
    new_expiry = _calc_new_expiry(user_data.get("tariff_expires"), days)

    db_users.update(
        user_id,
        tariff=tariff_key,
        tariff_expires=new_expiry,
        is_bot_active=True
    )

    saved = db_payments.add(
        user_id=user_id,
        tariff=tariff_key,
        amount_stars=sp.total_amount,
        telegram_charge_id=charge_id,
        provider_charge_id=sp.provider_payment_charge_id or ""
    )
    
    if not saved:
        logger.warning(f"Payment {charge_id} already in DB (race)")
        try:
            await update.message.reply_text("✅ Оплата получена, но возникла ошибка при записи. Админ проверит вручную.")
        except Exception:
            pass
        return

    if user_data.get("bot_token"):
        try:
            kill_existing_agent(user_id)
            activate_client_bot(user_id, user_data["bot_token"])
        except Exception as e:
            logger.warning(f"[{user_id}] Не удалось перезапустить агента: {e}")

    try:
        await update.message.reply_text(
            f"✅ **Оплата получена!**\n\n"
            f"Тариф: **{TARIFFS[tariff_key]['name']}**\n"
            f"Оплачено: {sp.total_amount} ⭐\n"
            f"Действует до: {new_expiry[:10]}\n\n"
            f"Теперь активируйте бота (если ещё не сделали) и настройте его в панели.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id)
        )
    except Exception:
        pass

    admin_text = (
        f"💰 **Новая оплата звёздами!**\n\n"
        f"Клиент: `{user_id}` @{user_data.get('username') or '—'}\n"
        f"Тариф: **{tariff_key}**\n"
        f"Сумма: {sp.total_amount} ⭐\n"
        f"Charge ID: `{charge_id}`"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=admin_text, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

    logger.info(
        f"✅ Payment: user={user_id} tariff={tariff_key} "
        f"stars={sp.total_amount} charge={charge_id}"
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

    # ===== ПРОБНЫЙ ПЕРИОД =====
    if data == "tariff_trial":
        if user_data and user_data.get("tariff") in PAID_TARIFFS:
            await query.message.edit_text(
                "❌ У вас уже есть активный платный тариф.",
                reply_markup=main_menu_keyboard(user_id)
            )
            return

        if user_data and user_data.get("trial_used"):
            await query.message.edit_text(
                "❌ Пробный период уже использован.",
                reply_markup=main_menu_keyboard(user_id)
            )
            return

        db_users.update(
            user_id,
            tariff="trial",
            tariff_expires=(datetime.now() + timedelta(days=2)).isoformat(),
            is_bot_active=True,
            trial_used=True
        )
        await query.message.edit_text(
            "🎁 **Пробный период активирован!**\n\n2 дня бесплатно.",
            reply_markup=main_menu_keyboard(user_id)
        )
        return

    # ===== СПИСОК ТАРИФОВ =====
    if data == "buy":
        await query.message.edit_text("Выберите тариф:", reply_markup=tariffs_keyboard(user_id))
        return

    # ===== КАРТОЧКА ТАРИФА =====
    if data.startswith("tariff_"):
        tariff_key = data.replace("tariff_", "")
        if tariff_key not in PAID_TARIFFS:
            await query.message.edit_text("❌ Неверный тариф.", reply_markup=main_menu_keyboard(user_id))
            return

        t = TARIFFS[tariff_key]
        price_rub = get_price_with_discount(tariff_key, user_id)
        price_stars = get_stars_price(tariff_key, user_id)

        text = (
            f"📦 **Тариф «{t['name']}»**\n\n"
            f"• Лидов: {t['leads']}\n"
            f"• Период: {t['days']} дней\n"
            f"• Цена: {price_rub} ₽ или {price_stars} ⭐\n\n"
            f"Выберите способ оплаты:"
        )
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=tariff_payment_keyboard(tariff_key, user_id)
        )
        return

    # ===== ОПЛАТА ЗВЁЗДАМИ =====
    if data.startswith("pay_stars_"):
        tariff_key = data.replace("pay_stars_", "")
        if tariff_key not in PAID_TARIFFS:
            await query.message.edit_text("❌ Неверный тариф.", reply_markup=main_menu_keyboard(user_id))
            return
        try:
            await send_stars_invoice(context, user_id, tariff_key)
        except Exception:
            await query.message.edit_text(
                "❌ Не удалось создать счёт. Попробуйте позже или оплатите картой.",
                reply_markup=main_menu_keyboard(user_id)
            )
        return

    # ===== ОПЛАТА КАРТОЙ =====
    if data.startswith("pay_card_"):
        tariff_key = data.replace("pay_card_", "")
        if tariff_key not in PAID_TARIFFS:
            await query.message.edit_text("❌ Неверный тариф.", reply_markup=main_menu_keyboard(user_id))
            return
        price = get_price_with_discount(tariff_key, user_id)
        await query.message.edit_text(
            f"💳 **Оплата картой (вручную)**\n\n"
            f"Тариф: **{TARIFFS[tariff_key]['name']}**\n"
            f"Сумма: **{price} ₽**\n\n"
            f"Переведите на карту:\n"
            f"`1234 5678 9012 3456`\n\n"
            f"После перевода напишите /confirm_payment — "
            f"админ проверит поступление и активирует тариф в течение 24 часов.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id)
        )
        return

    # ===== АКТИВАЦИЯ БОТА =====
    if data == "add_token":
        context.user_data["awaiting_token"] = True
        await query.message.edit_text("🔑 Отправьте токен вашего бота от @BotFather:")
        return

    # ===== ПАНЕЛЬ =====
    if data == "client_panel":
        if not await check_subscription(update, context):
            await query.message.edit_text("🔔 Подпишитесь на канал и нажмите /start.")
            return
        await query.message.edit_text(
            "⚙️ **Управление ботом**",
            reply_markup=client_panel_keyboard()
        )
        return

    # ===== ПОДМЕНЮ УПРАВЛЕНИЯ =====
    if data == "menu_personality":
        await query.message.edit_text(
            "🔥 **Настройка личности**\nВыберите параметр:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Скрипт и товары", callback_data="bot_settings")],
                [InlineKeyboardButton("📋 Товары", callback_data="show_products")],
                [InlineKeyboardButton("📚 RAG", callback_data="rag_settings")],
                [InlineKeyboardButton("❓ FAQ", callback_data="faq_settings")],
                [InlineKeyboardButton("🎯 Лиды", callback_data="lead_settings")],
                [InlineKeyboardButton("📝 Кастомные поля", callback_data="custom_fields_settings")],
                [InlineKeyboardButton("🅰️🅱️ A/B тест", callback_data="ab_settings")],
                [InlineKeyboardButton("🔙 Назад", callback_data="client_panel")]
            ])
        )
        return

    if data == "menu_communication":
        await query.message.edit_text(
            "🎤 **Коммуникации**\nВыберите параметр:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎤 Голос", callback_data="voice_settings")],
                [InlineKeyboardButton("💬 Шаблоны", callback_data="template_settings")],
                [InlineKeyboardButton("🌍 Языки", callback_data="language_settings")],
                [InlineKeyboardButton("🔙 Назад", callback_data="client_panel")]
            ])
        )
        return

    if data == "menu_team":
        await query.message.edit_text(
            "👥 **Команда**\nВыберите параметр:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Менеджеры", callback_data="manager_settings")],
                [InlineKeyboardButton("🏆 Геймификация", callback_data="gamification_settings")],
                [InlineKeyboardButton("🔙 Назад", callback_data="client_panel")]
            ])
        )
        return

    if data == "menu_integrations":
        await query.message.edit_text(
            "📊 **Интеграции**\nВыберите параметр:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 CRM", callback_data="crm_settings")],
                [InlineKeyboardButton("🤖 Мульти-бот", callback_data="multi_bot_settings")],
                [InlineKeyboardButton("🔙 Назад", callback_data="client_panel")]
            ])
        )
        return

    if data == "menu_management":
        await query.message.edit_text(
            "⚙️ **Управление**\nВыберите параметр:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Вкл/Выкл бота", callback_data="bot_toggle")],
                [InlineKeyboardButton("🕐 Расписание", callback_data="schedule_settings")],
                [InlineKeyboardButton("📊 Экспорт", callback_data="export_stats")],
                [InlineKeyboardButton("📈 Еженедельный отчёт", callback_data="weekly_report_settings")],
                [InlineKeyboardButton("🔄 Реаниматор", callback_data="followup_settings")],
                [InlineKeyboardButton("🛑 Остановить бота", callback_data="stop_bot")],
                [InlineKeyboardButton("🔙 Назад", callback_data="client_panel")]
            ])
        )
        return

    # ===== МОИ ПЛАТЕЖИ =====
    if data == "my_payments":
        history = db_payments.get_by_user(user_id, limit=20)
        if not history:
            await query.message.edit_text(
                "💳 У вас пока нет платежей.\n\nКупить тариф: /tariffs",
                reply_markup=main_menu_keyboard(user_id)
            )
            return
        total = sum(p["amount_stars"] for p in history)
        text = f"💳 **Ваши платежи** ({len(history)})\n\n"
        for p in history:
            date = (p.get("created_at") or "")[:10]
            tariff_name = TARIFFS.get(p["tariff"], {}).get("name", p["tariff"])
            text += f"• {date} — {tariff_name} — {p['amount_stars']} ⭐\n"
        text += f"\n💰 Всего: {total} ⭐"
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))
        return

    # ===== ТОП ЛИДОВ =====
    if data == "top_leads":
        leads = db_leads.get_top_leads(user_id, limit=5)
        
        if not leads:
            await query.message.edit_text(
                "🏆 **Топ-лидов**\n\nПока нет горячих лидов. Как только лид заинтересуется, он появится здесь.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id)
            )
            return

        text = "🏆 **Топ-5 горячих лидов:**\n\n"
        
        for i, l in enumerate(leads, 1):
            name = l.get("lead_name") or l.get("lead_username") or "Без имени"
            score = l.get("score", 0)
            
            try:
                facts = json.loads(l.get("facts") or "{}")
            except:
                facts = {}
                
            budget = facts.get("budget")
            deadline = facts.get("deadline")
            
            budget_str = f"{budget:,} ₽".replace(",", " ") if budget else "—"
            deadline_str = f"{deadline} дн." if deadline else "—"
            
            name = name.replace("_", "\\_").replace("*", "\\*")
            
            text += f"{i}. **{name}** (Оценка: {score}/100)\n"
            text += f"   💰 Бюджет: {budget_str} | 📅 Срок: {deadline_str}\n"
            
            problem = facts.get("problem")
            if problem:
                problem_clean = problem.replace("_", "\\_").replace("*", "\\*")
                text += f"   📝 Задача: {problem_clean[:50]}...\n"
            text += "\n"

        await query.message.edit_text(
            text, 
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="top_leads")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back")]
            ])
        )
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
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    if data == "show_products":
        products = (user_data or {}).get("bot_settings", {}).get("products", [])
        if products:
            text = "📦 **Товары:**\n\n" + "\n".join([f"{i+1}. {p}" for i, p in enumerate(products)])
            text += "\n\n`/add_product <название>`\n`/remove_product <номер>`\n`/clear_products`"
        else:
            text = "📦 Товаров нет.\n\nДобавьте: `/add_product <название>`"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    if data == "rag_settings":
        rag_count = len((user_data or {}).get("rag_data", []))
        await query.message.edit_text(
            f"📚 **RAG (База знаний)**\n\n"
            f"📄 Документов: {rag_count}\n\n"
            "**Команды:**\n`/rag_add <текст>`\n`/rag_list`\n`/rag_clear`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    if data == "faq_settings":
        faq = (user_data or {}).get("bot_settings", {}).get("faq", [])
        text = f"❓ **FAQ** ({len(faq)} вопросов)\n\n"
        for i, item in enumerate(faq[:5], 1):
            text += f"{i}. {item.get('question', '')[:40]}\n"
        text += "\n**Команды:**\n`/set_faq вопрос|ответ; ...`\n`/show_faq`\n`/remove_faq <номер>`"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    if data == "lead_settings":
        ls = (user_data or {}).get("lead_settings", {})
        await query.message.edit_text(
            f"🎯 **Настройка лидов**\n\n"
            f"💰 Бюджет: {ls.get('budget_threshold', 0)} ₽\n"
            f"📅 Срок: {ls.get('deadline_days', 0)} дн.\n"
            f"⚡ Правило: {ls.get('rule', 'AND')}\n\n"
            "**Команды:**\n"
            "`/set_budget 50000`\n`/set_deadline 7`\n`/set_rule AND`\n`/show_lead_settings`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    if data == "custom_fields_settings":
        fields = (user_data or {}).get("custom_fields", [])
        text = f"📝 **Кастомные поля** ({len(fields)})\n\n"
        for i, f in enumerate(fields, 1):
            text += f"{i}. {f.get('name', '')} ({f.get('type', 'text')})\n"
        text += "\n**Команды:**\n`/add_field <название>`\n`/remove_field <название>`"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    # ===== A/B ТЕСТ =====
    if data == "ab_settings":
        if not _ab_available(user_data):
            await query.message.edit_text(
                "🅰️🅱️ **A/B тест**\n\n"
                "❌ Доступен только на платных тарифах.\n\n"
                "Оформите подписку: /tariffs",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
                ])
            )
            return

        bs = (user_data or {}).get("bot_settings", {})
        enabled = bs.get("ab_enabled", False)
        a = (bs.get("ab_variant_a") or "").strip()
        b = (bs.get("ab_variant_b") or "").strip()

        text = "🅰️🅱️ **A/B тест скриптов**\n\n"
        text += f"Статус: {'✅ Включён' if enabled else '❌ Отключён'}\n\n"
        text += f"**Вариант A:**\n«{a[:120] if a else '— не задан —'}»\n\n"
        text += f"**Вариант B:**\n«{b[:120] if b else '— не задан —'}»\n\n"
        text += (
            "**Команды:**\n"
            "`/ab_on` — включить\n"
            "`/ab_off` — выключить\n"
            "`/ab_set 1 <текст>` — вариант A\n"
            "`/ab_set 2 <текст>` — вариант B\n"
            "`/ab_stats` — статистика\n"
            "`/ab_reset` — очистить оба варианта"
        )
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_personality")]
            ])
        )
        return

    # ===== КОММУНИКАЦИИ =====
    if data == "voice_settings":
        s = (user_data or {}).get("settings", {})
        enabled = s.get("voice_enabled", True)
        await query.message.edit_text(
            f"🎤 **Голосовые**\n\n"
            f"🔊 Статус: {'✅ Включены' if enabled else '❌ Выключены'}\n\n"
            "**Команды:**\n`/voice_on`\n`/voice_off`\n`/set_voice_error <текст>`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_communication")]
            ])
        )
        return

    if data == "template_settings":
        s = (user_data or {}).get("bot_settings", {})
        texts = s.get("auto_reply_texts", [])
        timeout = s.get("auto_reply_timeout", 300)
        await query.message.edit_text(
            f"💬 **Шаблоны**\n\n"
            f"📌 Шаблонов: {len(texts)}\n"
            f"⏱️ Таймаут: {timeout} сек.\n\n"
            "**Команды:**\n`/set_auto_reply текст1; текст2`\n`/set_timeout 300`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_communication")]
            ])
        )
        return

    if data == "language_settings":
        current = (user_data or {}).get("language", "ru")
        keyboard = [[InlineKeyboardButton(
            f"{'✅ ' if code == current else ''}{name}",
            callback_data=f"set_lang_{code}"
        )] for code, name in LANGUAGES.items()]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_communication")])
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

    # ===== КОМАНДА =====
    if data == "manager_settings":
        managers = (user_data or {}).get("managers", [])
        text = f"👥 **Менеджеры** ({len(managers)})\n\n"
        for m in managers:
            emoji = "⭐" if m.get("level") == "senior" else "🔹"
            text += f"{emoji} `{m.get('id')}` — {m.get('level', 'junior')}\n"
        text += "\n**Команды:**\n`/add_manager <id> junior/senior`\n`/remove_manager <id>`"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_team")]
            ])
        )
        return

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
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_team")]
            ])
        )
        return

    # ===== ИНТЕГРАЦИИ =====
    if data == "crm_settings":
        crm = (user_data or {}).get("settings", {}).get("crm_integration", {})
        enabled = crm.get("enabled")
        crm_type = crm.get("type") or "—"
        stats = db_crm_log.get_stats(user_id)

        status_line = "🟢 Включена" if enabled else "🔴 Отключена"
        text = (
            f"📊 **CRM Интеграция**\n\n"
            f"Статус: {status_line}\n"
            f"Система: {crm_type}\n"
        )
        if stats["total"] > 0:
            text += (
                f"📤 Отправлено: {stats['success']}\n"
                f"❌ Ошибок: {stats['error']}\n"
            )
        text += "\nВыберите систему:"

        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📘 AmoCRM", callback_data="crm_select_amocrm")],
                [InlineKeyboardButton("🅱️ Bitrix24", callback_data="crm_select_bitrix24")],
                [InlineKeyboardButton("🟠 HubSpot", callback_data="crm_select_hubspot")],
                [InlineKeyboardButton("🔍 Проверить подключение", callback_data="crm_test_run")],
                [InlineKeyboardButton("📋 История отправок", callback_data="crm_log_view")],
                [InlineKeyboardButton("🛑 Отключить CRM", callback_data="crm_disable_confirm")],
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_integrations")],
            ])
        )
        return

    if data == "crm_select_amocrm":
        await query.message.edit_text(
            "📘 **AmoCRM**\n\n"
            "1. AmoCRM → Настройки → Интеграции → Создать интеграцию.\n"
            "2. Тип: «Внешняя интеграция».\n"
            "3. Права: сделки, контакты.\n"
            "4. Долгосрочный токен (Long-lived token).\n"
            "5. Subdomain — часть до `.amocrm.ru`.\n\n"
            "Затем:\n"
            "`/crm_amocrm <subdomain> <token>`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
            ])
        )
        return

    if data == "crm_select_bitrix24":
        await query.message.edit_text(
            "🅱️ **Bitrix24**\n\n"
            "1. Bitrix24 → Разработчикам → Входящий webhook.\n"
            "2. Права: CRM (crm).\n"
            "3. URL: `https://mycompany.bitrix24.ru/rest/1/abc123/`\n\n"
            "Затем:\n"
            "`/crm_bitrix <webhook_url>`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
            ])
        )
        return

    if data == "crm_select_hubspot":
        await query.message.edit_text(
            "🟠 **HubSpot**\n\n"
            "1. Settings → Integrations → Private Apps.\n"
            "2. Создайте Private App.\n"
            "3. Права: crm.objects.contacts.\n"
            "4. Access Token (начинается с `pat-`).\n\n"
            "Затем:\n"
            "`/crm_hubspot <api_key>`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
            ])
        )
        return

    if data == "crm_test_run":
        crm = (user_data or {}).get("settings", {}).get("crm_integration", {})
        if not crm.get("enabled"):
            await query.message.edit_text(
                "❌ CRM не настроена.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
                ])
            )
            return
        await query.message.edit_text("🔍 Проверяю подключение...")
        ok, msg = await crm_client.test_connection(crm)
        await query.message.edit_text(
            f"{msg}\n\nСистема: `{crm.get('type')}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
            ])
        )
        return

    if data == "crm_log_view":
        logs = db_crm_log.get_by_client(user_id, limit=15)
        if not logs:
            await query.message.edit_text(
                "📋 Пока нет отправок в CRM.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
                ])
            )
            return
        text = "📋 **Последние отправки:**\n\n"
        for log in logs:
            date = (log.get("created_at") or "")[:16].replace("T", " ")
            icon = "✅" if log.get("status") == "success" else "❌"
            text += f"{icon} {date} — {log.get('message', '')[:60]}\n"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="crm_settings")]
            ])
        )
        return

    if data == "crm_disable_confirm":
        s = (user_data or {}).get("settings", {})
        crm = s.get("crm_integration", {})
        if crm:
            crm["enabled"] = False
            s["crm_integration"] = crm
            db_users.update(user_id, settings=s)
        await query.message.edit_text(
            "🛑 CRM отключена. Ключи сохранены.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 К CRM", callback_data="crm_settings")]
            ])
        )
        return

    if data == "multi_bot_settings":
        bots = (user_data or {}).get("bots", [])
        tariff = (user_data or {}).get("tariff", "trial")
        limit = BOT_LIMITS.get(tariff, 1)
        text = f"🤖 **Мульти-бот** ({len(bots)}/{limit})\n\n"
        for b in bots:
            text += f"• {b.get('name', '—')} — {'✅' if b.get('is_active') else '❌'}\n"
        text += "\n**Команды:**\n`/add_bot <название> <токен>`\n`/remove_bot <название>`\n`/bots_list`"
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_integrations")]
            ])
        )
        return

    # ===== УПРАВЛЕНИЕ =====
    if data == "bot_toggle":
        new_status = not (user_data or {}).get("is_bot_active", True)
        db_users.update(user_id, is_bot_active=new_status)
        await query.message.edit_text(
            f"🔄 Бот {'включён ✅' if new_status else 'выключен ❌'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    if data == "stop_bot":
        kill_existing_agent(user_id)
        db_users.update(user_id, bot_token=None, is_bot_active=False)
        await query.message.edit_text(
            "🛑 Бот остановлен, токен удалён из базы.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    if data == "schedule_settings":
        schedule = (user_data or {}).get("schedules", {})
        await query.message.edit_text(
            f"🕐 **Расписание**\n\n"
            f"Статус: {'✅ Включено' if schedule.get('enabled') else '❌ Выключено'}\n"
            f"Время: {schedule.get('start_time', '09:00')} - {schedule.get('end_time', '21:00')}\n\n"
            "**Команды:**\n`/schedule_on`\n`/schedule_off`\n`/set_schedule 09:00-21:00`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    if data == "export_stats":
        bs = (user_data or {}).get("bot_settings", {})
        sheets_url = bs.get("sheets_webhook_url") or "—"
        sheets_ok = "✅" if sheets_url != "—" else "❌"

        text = (
            "📊 **Экспорт данных**\n\n"
            "**Файлы:**\n"
            "`/export day csv` — CSV\n"
            "`/export week excel` — Excel (.xlsx)\n"
            "`/export month pdf` — PDF\n\n"
            f"**Google Sheets:** {sheets_ok}\n"
            f"URL: `{sheets_url[:60]}{'…' if len(sheets_url) > 60 else ''}`\n\n"
            "`/export_sheets <url>` — подключить Google Sheets\n"
            "`/export_sheets` — выгрузить лидов в Sheets"
        )
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    if data == "weekly_report_settings":
        bs = (user_data or {}).get("bot_settings", {})
        enabled = bs.get("weekly_report_enabled", True)
        text = (
            f"📈 **Еженедельный отчёт**\n\n"
            f"Статус: {'✅ Включён' if enabled else '❌ Отключён'}\n"
            f"Отправка: каждый понедельник в 10:00\n\n"
            f"**Что в отчёте:**\n"
            f"• Лидов за неделю\n"
            f"• Горячих и конверсия\n"
            f"• Сравнение с прошлой неделей\n"
            f"• Топ-3 тега\n"
            f"• 💡 Инсайты\n"
            f"• 🎯 Рекомендации\n"
            f"• 📈 График динамики\n\n"
            f"**Команды:**\n"
            f"`/weekly_on` — включить\n"
            f"`/weekly_off` — отключить\n"
            f"`/weekly_report_now` — прислать сейчас"
        )
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    if data == "followup_settings":
        bs = (user_data or {}).get("bot_settings", {})
        enabled = bs.get("followup_enabled", True)
        intervals = _get_followup_intervals(user_data)
        texts = bs.get("auto_reply_texts") or []

        text = f"🔄 **Реаниматор (follow-up)**\n\n"
        text += f"Статус: {'✅ Включён' if enabled else '❌ Отключён'}\n"
        text += f"Попыток: {len(intervals)}\n\n"

        for i, sec in enumerate(intervals, 1):
            txt = texts[(i - 1) % len(texts)] if texts else "—"
            text += f"{i}. через {_fmt_duration(sec)} — «{txt[:50]}»\n"

        text += (
            "\n**Команды:**\n"
            "`/followup_on` / `/followup_off`\n"
            "`/followup_interval <1-3> <секунд>`\n"
            "`/followup_text <1-3> <текст>`\n"
            "`/set_auto_reply текст1; текст2`\n"
            "`/followup_reset`"
        )
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="menu_management")]
            ])
        )
        return

    # ===== СТАТИСТИКА =====
    if data == "stats":
        if not user_data:
            await query.message.edit_text("❌ Сначала /start", reply_markup=main_menu_keyboard(user_id))
            return
        total_leads = db_leads.count_by_client(user_id)
        hot_leads = db_leads.count_hot_by_client(user_id)
        
        conv = int((hot_leads / total_leads * 100)) if total_leads > 0 else 0
        bar_length = 10
        filled = int(bar_length * conv / 100)
        bar = "🟩" * filled + "⬜️" * (bar_length - filled)

        await query.message.edit_text(
            f"📊 **Статистика бота**\n\n"
            f"🤖 Бот: {'✅ Активен' if user_data.get('bot_token') else '❌ Не активирован'}\n"
            f"📦 Тариф: {user_data.get('tariff', 'trial').upper()}\n"
            f"👥 Менеджеров: {len(user_data.get('managers', []))}\n\n"
            f"📈 **Лиды:**\n"
            f"Всего: {total_leads} | 🔥 Горячих: {hot_leads}\n"
            f"Конверсия: {conv}%\n"
            f"{bar}\n\n"
            f"📚 RAG-документов: {len(user_data.get('rag_data', []))}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="stats")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back")]
            ])
        )
        return

    if data == "support":
        await query.message.edit_text(
            "💬 **Поддержка**\n\nНапишите `/support_chat` чтобы связаться с разработчиком.",
            reply_markup=main_menu_keyboard(user_id)
        )
        return

    if data == "suggest_idea":
        context.user_data["awaiting_idea"] = True
        await query.message.edit_text("💡 Напишите вашу идею:", reply_markup=main_menu_keyboard(user_id))
        return

    if data == "help":
        await help_command(update, context)
        return

    if data == "back":
        context.user_data.clear()
        await query.message.edit_text(
            "Главное меню:", reply_markup=main_menu_keyboard(user_id)
        )
        return

    await query.message.edit_text("⚠️ Команда не найдена.", reply_markup=main_menu_keyboard(user_id))

# ========================================================================
# GOOGLE SHEETS
# ========================================================================

async def _push_to_sheets(url: str, rows: list) -> tuple:
    if not url or not url.startswith("https://script.google.com/"):
        return False, "URL должен начинаться с https://script.google.com/"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.post(url, json={"rows": rows}) as r:
                if r.status == 200:
                    try:
                        data = await r.json()
                    except Exception:
                        return False, "Webhook вернул не-JSON"
                    if data.get("ok"):
                        return True, f"Записано строк: {data.get('added', len(rows))}"
                    return False, data.get("error", "Неизвестная ошибка Apps Script")
                text = await r.text()
                return False, f"HTTP {r.status}: {text[:200]}"
    except aiohttp.ClientError as e:
        return False, f"Сеть: {e}"
    except Exception as e:
        return False, f"{e}"

def _leads_to_sheet_rows(leads: list) -> list:
    rows = []
    for l in leads:
        try:
            tags = json.loads(l.get("tags") or "[]")
            tags_str = ", ".join(tags) if isinstance(tags, list) else ""
        except (json.JSONDecodeError, TypeError):
            tags_str = ""

        rows.append([
            (l.get("created_at") or "")[:16].replace("T", " "),
            l.get("lead_telegram_id") or "",
            l.get("lead_name") or "",
            l.get("lead_username") or "",
            (l.get("summary") or "").split("\n")[0] if l.get("summary") else "",
            l.get("score") or 0,
            "да" if l.get("is_hot") else "нет",
            tags_str,
            l.get("sentiment") or "neutral",
        ])
    return rows

# ========================================================================
# ЭКСПОРТ: ХЕЛПЕРЫ
# ========================================================================

def _build_csv(stats: list) -> bytes:
    lines = ["date,leads,hot"]
    for s in stats:
        lines.append(f"{s['date']},{s['leads_count']},{s['hot_leads_count']}")
    return "\n".join(lines).encode("utf-8")

def _build_excel(stats: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = "Stats"

    ws.append(["Дата", "Лидов", "Горячих"])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    total_leads = 0
    total_hot = 0
    for s in stats:
        ws.append([s["date"], s["leads_count"], s["hot_leads_count"]])
        total_leads += s["leads_count"]
        total_hot += s["hot_leads_count"]

    ws.append(["ИТОГО", total_leads, total_hot])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 10

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()

def _find_cyrillic_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("StatsFont", path))
                return "StatsFont"
            except Exception as e:
                logger.warning(f"Не удалось зарегистрировать шрифт {path}: {e}")
    return "Helvetica"

def _build_pdf(stats: list, period: str) -> bytes:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from io import BytesIO

    font_name = _find_cyrillic_font()

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    c.setFont(font_name, 16)
    c.drawString(50, height - 50, f"Статистика за {period}")

    c.setFont(font_name, 10)
    y = height - 90
    total_leads = 0
    total_hot = 0

    for s in stats:
        if y < 60:
            c.showPage()
            c.setFont(font_name, 10)
            y = height - 60

        c.drawString(50, y, f"{s['date']}: {s['leads_count']} лидов, {s['hot_leads_count']} горячих")
        y -= 18
        total_leads += s["leads_count"]
        total_hot += s["hot_leads_count"]

    if y < 80:
        c.showPage()
        c.setFont(font_name, 11)
        y = height - 60

    c.setFont(font_name, 12)
    c.drawString(50, y - 10, f"ИТОГО: {total_leads} лидов, {total_hot} горячих")

    c.save()
    buf.seek(0)
    return buf.read()

def _build_weekly_chart(client_id: int) -> Optional[BytesIO]:
    stats = db_stats.get_client_stats(client_id, period="week")
    if not stats:
        return None

    today = datetime.now()
    dates_map = {}
    for i in range(7):
        d = (today - timedelta(days=6-i)).strftime("%Y-%m-%d")
        dates_map[d] = {"leads": 0, "hot": 0}
    
    for s in stats:
        if s["date"] in dates_map:
            dates_map[s["date"]]["leads"] = s["leads_count"]
            dates_map[s["date"]]["hot"] = s["hot_leads_count"]
            
    sorted_dates = sorted(dates_map.keys())
    dates = [d[5:] for d in sorted_dates]
    leads = [dates_map[d]["leads"] for d in sorted_dates]
    hot = [dates_map[d]["hot"] for d in sorted_dates]

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(7, 4))
    
    ax.plot(dates, leads, marker='o', label='Всего лидов', color='#00A8FF', linewidth=2)
    ax.plot(dates, hot, marker='s', label='Горячие лиды', color='#FF4757', linewidth=2)

    ax.set_title('Динамика лидов за неделю', fontsize=14, color='white')
    ax.set_xlabel('Дата', color='gray')
    ax.set_ylabel('Количество', color='gray')
    ax.grid(True, linestyle='--', alpha=0.3)
    ax.legend()

    for spine in ['top', 'right']:
        ax.spines[spine].set_visible(False)

    buf = BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plt.close(fig)
    
    return buf

# ========================================================================
# ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ
# ========================================================================

def _parse_lead_facts(lead: dict) -> dict:
    raw = lead.get("facts")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}

def _build_weekly_insights(leads: list) -> list:
    if not leads:
        return []

    total = len(leads)

    budgets = []
    deadlines = []
    for l in leads:
        f = _parse_lead_facts(l)
        b = f.get("budget")
        d = f.get("deadline")
        try:
            if b is not None:
                budgets.append(int(b))
        except (ValueError, TypeError):
            pass
        try:
            if d is not None:
                deadlines.append(int(d))
        except (ValueError, TypeError):
            pass

    positive = sum(1 for l in leads if l.get("sentiment") == "positive")
    negative = sum(1 for l in leads if l.get("sentiment") == "negative")

    hour_counter: Counter = Counter()
    for l in leads:
        created = l.get("created_at") or ""
        if len(created) >= 13 and created[10] == "T":
            try:
                hour = int(created[11:13])
                hour_counter[hour] += 1
            except (ValueError, IndexError):
                pass

    lines = []

    if budgets:
        avg_b = sum(budgets) // len(budgets)
        avg_b_str = f"{avg_b:,}".replace(",", " ")
        lines.append(
            f"• 💰 Средний бюджет: **{avg_b_str} ₽** "
            f"(указали {len(budgets)} из {total})"
        )
    if deadlines:
        avg_d = sum(deadlines) // len(deadlines)
        lines.append(
            f"• 📅 Средние сроки: **{avg_d} дн.** "
            f"(указали {len(deadlines)} из {total})"
        )
    if positive or negative:
        pos_pct = int(round(positive / total * 100))
        neg_pct = int(round(negative / total * 100))
        lines.append(f"• 😊 Позитив: {pos_pct}% / 😠 Негатив: {neg_pct}%")

    if hour_counter:
        peak_hour, peak_count = hour_counter.most_common(1)[0]
        lines.append(
            f"• 🕐 Пик активности: **{peak_hour:02d}:00–{peak_hour+1:02d}:00** "
            f"({peak_count} лидов)"
        )

    return lines

def _build_weekly_recommendations(leads: list, user_data: dict) -> list:
    if not leads:
        return []

    total = len(leads)
    recs = []

    schedules = (user_data or {}).get("schedules") or {}
    if schedules.get("enabled"):
        try:
            start_h = int((schedules.get("start_time") or "09:00").split(":")[0])
            end_h = int((schedules.get("end_time") or "21:00").split(":")[0])
        except (ValueError, IndexError):
            start_h, end_h = 9, 21

        out_of_window = 0
        for l in leads:
            created = l.get("created_at") or ""
            if len(created) >= 13 and created[10] == "T":
                try:
                    h = int(created[11:13])
                    if start_h <= end_h:
                        if not (start_h <= h < end_h):
                            out_of_window += 1
                    else:
                        if end_h <= h < start_h:
                            out_of_window += 1
                except (ValueError, IndexError):
                    pass

        if out_of_window / total > 0.3:
            pct = int(round(out_of_window / total * 100))
            recs.append(
                f"• 🕐 {pct}% лидов пишут вне рабочего окна — "
                f"расширьте расписание или отключите его"
            )

    negative = sum(1 for l in leads if l.get("sentiment") == "negative")
    if negative / total > 0.2:
        pct = int(round(negative / total * 100))
        recs.append(
            f"• 😠 {pct}% лидов негативны — проверьте FAQ и скрипт, "
            f"добавьте ответы на частые возражения"
        )

    ls = (user_data or {}).get("lead_settings") or {}
    threshold = ls.get("budget_threshold", 0) or 0
    if threshold > 0:
        budgets = []
        for l in leads:
            f = _parse_lead_facts(l)
            try:
                if f.get("budget") is not None:
                    budgets.append(int(f["budget"]))
            except (ValueError, TypeError):
                pass
        if budgets:
            avg = sum(budgets) // len(budgets)
            if avg < threshold:
                avg_str = f"{avg:,}".replace(",", " ")
                threshold_str = f"{threshold:,}".replace(",", " ")
                recs.append(
                    f"• 💰 Средний бюджет ({avg_str} ₽) ниже порога "
                    f"({threshold_str} ₽) — возможно, порог завышен"
                )

    hot = sum(1 for l in leads if l.get("is_hot"))
    if total >= 5 and hot / total < 0.15:
        recs.append(
            "• 🔥 Мало горячих лидов (<15%) — проверьте скрипт, "
            "возможно, он слишком пассивный"
        )

    return recs[:3]

def _build_ab_block(client_id: int, user_data: dict) -> list:
    bs = (user_data or {}).get("bot_settings") or {}
    if not bs.get("ab_enabled", False):
        return []
    if not _ab_available(user_data):
        return []

    since = (datetime.now() - timedelta(days=7)).isoformat()
    stats = db_leads.get_ab_stats(client_id, since)

    a = stats.get("A", {"total": 0, "hot": 0})
    b = stats.get("B", {"total": 0, "hot": 0})

    if a["total"] == 0 and b["total"] == 0:
        return []

    def conv(s):
        return int(round(s["hot"] / s["total"] * 100)) if s["total"] else 0

    a_conv = conv(a)
    b_conv = conv(b)

    if a["total"] + b["total"] < 10:
        verdict = "мало данных для вывода (нужно ≥10 лидов)"
    elif abs(a_conv - b_conv) < 5:
        verdict = "варианты примерно равны"
    elif a_conv > b_conv:
        verdict = f"вариант A лучше на {a_conv - b_conv} п.п."
    else:
        verdict = f"вариант B лучше на {b_conv - a_conv} п.п."

    return [
        "🅰️🅱️ **A/B результаты:**",
        f"• A: {a['total']} лидов, {a['hot']} горячих ({a_conv}%)",
        f"• B: {b['total']} лидов, {b['hot']} горячих ({b_conv}%)",
        f"• Итог: {verdict}",
    ]

def _build_weekly_report(client_id: int) -> Optional[str]:
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).isoformat()
    two_weeks_ago = (now - timedelta(days=14)).isoformat()

    current = db_leads.get_leads_since(client_id, week_ago)
    previous = db_leads.get_leads_between(client_id, two_weeks_ago, week_ago)

    if not current and not previous:
        return None

    total = len(current)
    hot = sum(1 for l in current if l.get("is_hot"))
    conversion = int(round(hot / total * 100)) if total else 0

    tag_counter: Counter = Counter()
    for l in current:
        try:
            tags = json.loads(l.get("tags") or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        if isinstance(tags, list):
            for t in tags:
                tag_counter[t] += 1
    top_tags = tag_counter.most_common(3)

    prev_total = len(previous)
    trend = ""
    if prev_total > 0:
        diff = total - prev_total
        pct = int(round(diff / prev_total * 100))
        if diff > 0:
            trend = f"📈 +{diff} к прошлой неделе (+{pct}%)"
        elif diff < 0:
            trend = f"📉 {diff} к прошлой неделе ({pct}%)"
        else:
            trend = "➡️ Как на прошлой неделе"
    elif total > 0:
        trend = "🆕 Первая неделя с лидами"

    lines = [
        "📊 **Еженедельный отчёт AI-менеджера**",
        "",
        f"Период: последние 7 дней ({(now - timedelta(days=7)).strftime('%d.%m')} – {now.strftime('%d.%m')})",
        "",
        f"📈 Лидов: **{total}**",
        f"🔥 Горячих: **{hot}** ({conversion}%)",
    ]
    if trend:
        lines.append(trend)

    if top_tags:
        tags_str = ", ".join(f"{t} ({c})" for t, c in top_tags)
        lines.append("")
        lines.append(f"🏷️ Топ-теги: {tags_str}")

    insights = _build_weekly_insights(current)
    if insights:
        lines.append("")
        lines.append("💡 **Инсайты:**")
        lines.extend(insights)

    user_data = db_users.get(client_id) or {}
    recs = _build_weekly_recommendations(current, user_data)
    if recs:
        lines.append("")
        lines.append("🎯 **Рекомендации:**")
        lines.extend(recs)

    ab_block = _build_ab_block(client_id, user_data)
    if ab_block:
        lines.append("")
        lines.extend(ab_block)

    lines.append("")
    lines.append("📌 Детали: /stats")
    lines.append("🔕 Отключить: /weekly_off")

    return "\n".join(lines)

async def send_weekly_reports(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.weekday() != 0:
        return
    if now.hour != 10:
        return

    users = db_users.get_all()
    sent = 0
    skipped = 0

    for u in users:
        uid = u["user_id"]
        if is_admin(uid):
            continue
        if u.get("is_banned"):
            continue
        if u.get("tariff") in ("expired",):
            continue
        if not u.get("is_bot_active"):
            continue

        bs = u.get("bot_settings") or {}
        if not bs.get("weekly_report_enabled", True):
            skipped += 1
            continue

        report = _build_weekly_report(uid)
        if not report:
            skipped += 1
            continue

        try:
            await context.bot.send_message(
                chat_id=uid, text=report, parse_mode="Markdown"
            )
            
            chart_buf = _build_weekly_chart(uid)
            if chart_buf:
                try:
                    await context.bot.send_photo(
                        chat_id=uid, 
                        photo=InputFile(chart_buf, filename="chart.png"),
                        caption="📈 Визуализация лидов за неделю"
                    )
                finally:
                    chart_buf.close()
                    
            sent += 1
        except Forbidden:
            logger.warning(f"Отчёт {uid}: пользователь заблокировал бота")
            skipped += 1
        except Exception as e:
            logger.error(f"Отчёт {uid}: {e}")

    logger.info(f"📊 Еженедельные отчёты: отправлено {sent}, пропущено {skipped}")

# ========================================================================
# HANDLE_MESSAGE
# ========================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if not text:
        return

    parts = text.split(maxsplit=1)
    first_word = parts[0] if parts else ""

    # Пропускаем команды, у которых есть свои хендлеры
    if first_word in ("/start", "/tariffs", "/help", "/payments"):
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

        if ":" not in text or len(text) < 20:
            await update.message.reply_text(
                "❌ Неверный формат токена.\n\n"
                "Токен выглядит как `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id)
            )
            return

        msg = await update.message.reply_text("🔍 Проверяю токен...")
        ok, info = await validate_bot_token(text)
        if not ok:
            await msg.edit_text(
                f"❌ **Токен не прошёл проверку.**\n\n"
                f"Причина: `{info}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id)
            )
            return

        db_users.update(user_id, bot_token=text)
        if activate_client_bot(user_id, text):
            await msg.edit_text(
                f"✅ **Бот активирован!**\n\n"
                f"Ваш AI-менеджер запущен: `{info}`\n\n"
                f"Лиды будут квалифицироваться, а горячие — приходить сюда.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id)
            )
        else:
            await msg.edit_text(
                "❌ Токен валиден, но не удалось запустить агента. "
                "Проверьте `main_bot.log`.",
                reply_markup=main_menu_keyboard(user_id)
            )
        return

    # ===== ПОДДЕРЖКА =====
    if context.user_data.get("awaiting_support"):
        context.user_data["awaiting_support"] = False
        reqs = user_data.get("support_requests", [])
        reqs.append({
            "message": text, "status": "open",
            "created_at": datetime.now().isoformat(), "user_id": user_id
        })
        db_users.update(user_id, support_requests=reqs)
        await update.message.reply_text("✅ Отправлено в поддержку!", reply_markup=main_menu_keyboard(user_id))
        return

    # ===== ИДЕИ =====
    if context.user_data.get("awaiting_idea"):
        context.user_data["awaiting_idea"] = False
        ideas = user_data.get("ideas", [])
        ideas.append({
            "text": text, "status": "pending",
            "created_at": datetime.now().isoformat(), "user_id": user_id
        })
        db_users.update(user_id, ideas=ideas)
        await update.message.reply_text("✅ Спасибо за идею!", reply_markup=main_menu_keyboard(user_id))
        return

    # ===== СКРИПТ =====
    if first_word == "/set_script":
        script = text.replace("/set_script", "", 1).strip()
        if script:
            s = user_data.get("bot_settings", {})
            s["script"] = script
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text("✅ Скрипт обновлён!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="client_panel")]]))
        return

    if first_word == "/show_script":
        s = user_data.get("bot_settings", {})
        await update.message.reply_text(f"📝 {s.get('script', 'Не установлен')}")
        return

    if first_word == "/reset_script":
        s = user_data.get("bot_settings", {})
        s["script"] = "Ты — профессиональный менеджер по продажам."
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Скрипт сброшен.")
        return

    # ===== ТОВАРЫ =====
    if first_word == "/set_products":
        products = [p.strip() for p in text.replace("/set_products", "", 1).split(",") if p.strip()]
        if products:
            s = user_data.get("bot_settings", {})
            s["products"] = products
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Товары: {', '.join(products)}")
        return

    if first_word == "/add_product":
        product = text.replace("/add_product", "", 1).strip()
        if product:
            s = user_data.get("bot_settings", {})
            prods = s.get("products", [])
            prods.append(product)
            s["products"] = prods
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Добавлен: {product}")
        return

    if first_word == "/show_products":
        products = user_data.get("bot_settings", {}).get("products", [])
        if products:
            out = "📦 **Товары:**\n\n" + "\n".join([f"{i+1}. {p}" for i, p in enumerate(products)])
        else:
            out = "📦 Список пуст."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/remove_product":
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

    if first_word == "/clear_products":
        s = user_data.get("bot_settings", {})
        s["products"] = []
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Все товары удалены.")
        return

    # ===== RAG =====
    if first_word == "/rag_add":
        rag_text = text.replace("/rag_add", "", 1).strip()
        if rag_text:
            rag_data = user_data.get("rag_data", [])
            rag_data.append(rag_text)
            db_users.update(user_id, rag_data=rag_data)
            await update.message.reply_text(f"✅ Добавлено ({len(rag_data)} документов).")
        return

    if first_word == "/rag_list":
        rag_data = user_data.get("rag_data", [])
        await update.message.reply_text(f"📚 В RAG: {len(rag_data)} документов.")
        return

    if first_word == "/rag_clear":
        db_users.update(user_id, rag_data=[])
        await update.message.reply_text("✅ RAG очищена.")
        return

    # ===== FAQ =====
    if first_word == "/set_faq":
        pairs = text.replace("/set_faq", "", 1).split(";")
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

    if first_word == "/show_faq":
        faq = user_data.get("bot_settings", {}).get("faq", [])
        if faq:
            out = "❓ **FAQ:**\n\n" + "\n".join(
                [f"{i+1}. {f['question']}\n   💬 {f['answer']}" for i, f in enumerate(faq)]
            )
        else:
            out = "❓ FAQ пуст."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/remove_faq":
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
    if first_word == "/set_budget":
        if len(parts) == 2 and parts[1].isdigit():
            ls = user_data.get("lead_settings", {})
            ls["budget_threshold"] = int(parts[1])
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Бюджет: {parts[1]} ₽")
        return

    if first_word == "/set_deadline":
        if len(parts) == 2 and parts[1].isdigit():
            ls = user_data.get("lead_settings", {})
            ls["deadline_days"] = int(parts[1])
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Срок: {parts[1]} дней")
        return

    if first_word == "/set_rule":
        if len(parts) == 2 and parts[1].upper() in ("AND", "OR"):
            ls = user_data.get("lead_settings", {})
            ls["rule"] = parts[1].upper()
            db_users.update(user_id, lead_settings=ls)
            await update.message.reply_text(f"✅ Правило: {parts[1].upper()}")
        return

    if first_word == "/show_lead_settings":
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
    if first_word == "/add_field":
        field_name = text.replace("/add_field", "", 1).strip()
        if field_name:
            fields = user_data.get("custom_fields", [])
            if not any(f.get("name") == field_name for f in fields):
                fields.append({"name": field_name, "type": "text", "required": False})
                db_users.update(user_id, custom_fields=fields)
                await update.message.reply_text(f"✅ Поле добавлено: {field_name}")
            else:
                await update.message.reply_text("❌ Поле уже существует.")
        return

    if first_word == "/remove_field":
        field_name = text.replace("/remove_field", "", 1).strip()
        if field_name:
            fields = [f for f in user_data.get("custom_fields", []) if f.get("name") != field_name]
            db_users.update(user_id, custom_fields=fields)
            await update.message.reply_text(f"✅ Поле удалено: {field_name}")
        return

    # ===== МЕНЕДЖЕРЫ =====
    if first_word == "/add_manager":
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

    if first_word == "/remove_manager":
        if len(parts) == 2 and parts[1].isdigit():
            manager_id = int(parts[1])
            managers = [m for m in user_data.get("managers", []) if m.get("id") != manager_id]
            db_users.update(user_id, managers=managers)
            await update.message.reply_text("✅ Удалён.")
        return

    if first_word == "/managers_list":
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
    if first_word == "/voice_on":
        s = user_data.get("settings", {})
        s["voice_enabled"] = True
        db_users.update(user_id, settings=s)
        await update.message.reply_text("✅ Голосовые включены.")
        return

    if first_word == "/voice_off":
        s = user_data.get("settings", {})
        s["voice_enabled"] = False
        db_users.update(user_id, settings=s)
        await update.message.reply_text("❌ Голосовые выключены.")
        return

    if first_word == "/set_voice_error":
        error_text = text.replace("/set_voice_error", "", 1).strip()
        if error_text:
            s = user_data.get("bot_settings", {})
            s["voice_error_text"] = error_text
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text("✅ Текст ошибки обновлён.")
        return

    # ===== ШАБЛОНЫ =====
    if first_word == "/set_auto_reply":
        texts = [t.strip() for t in text.replace("/set_auto_reply", "", 1).split(";") if t.strip()]
        if texts:
            s = user_data.get("bot_settings", {})
            s["auto_reply_texts"] = texts
            db_users.update(user_id, bot_settings=s)
            await update.message.reply_text(f"✅ Установлено {len(texts)} шаблонов.")
        return

    if first_word == "/set_timeout":
        if len(parts) == 2 and parts[1].isdigit():
            timeout = int(parts[1])
            if timeout >= 10:
                s = user_data.get("bot_settings", {})
                s["auto_reply_timeout"] = timeout
                db_users.update(user_id, bot_settings=s)
                await update.message.reply_text(f"✅ Таймаут: {timeout} сек.")
        return

    # ===== A/B ТЕСТ =====
    if first_word == "/ab_on":
        if not _ab_available(user_data):
            await update.message.reply_text("❌ A/B доступен только на платных тарифах. /tariffs")
            return
        bs = user_data.get("bot_settings", {})
        a = (bs.get("ab_variant_a") or "").strip()
        b = (bs.get("ab_variant_b") or "").strip()
        if not a or not b:
            await update.message.reply_text(
                "❌ Сначала задайте оба варианта:\n"
                "`/ab_set 1 <текст>`\n`/ab_set 2 <текст>`",
                parse_mode="Markdown"
            )
            return
        bs["ab_enabled"] = True
        db_users.update(user_id, bot_settings=bs)
        await update.message.reply_text("✅ A/B включён.")
        return

    if first_word == "/ab_off":
        bs = user_data.get("bot_settings", {})
        bs["ab_enabled"] = False
        db_users.update(user_id, bot_settings=bs)
        await update.message.reply_text("🔕 A/B отключён.")
        return

    if first_word == "/ab_set":
        if not _ab_available(user_data):
            await update.message.reply_text("❌ A/B доступен только на платных тарифах. /tariffs")
            return
        if len(parts) < 3 or not parts[1] in ("1", "2"):
            await update.message.reply_text(
                "❌ Используйте: `/ab_set 1 <текст>` или `/ab_set 2 <текст>`",
                parse_mode="Markdown"
            )
            return
        variant = parts[1]
        new_text = parts[2].strip()
        if not new_text:
            await update.message.reply_text("❌ Текст не может быть пустым.")
            return
        bs = user_data.get("bot_settings", {})
        if variant == "1":
            bs["ab_variant_a"] = new_text
        else:
            bs["ab_variant_b"] = new_text
        db_users.update(user_id, bot_settings=bs)
        await update.message.reply_text(
            f"✅ Вариант {'A' if variant == '1' else 'B'} обновлён."
        )
        return

    if first_word == "/ab_status":
        if not _ab_available(user_data):
            await update.message.reply_text("❌ A/B доступен только на платных тарифах. /tariffs")
            return
        bs = user_data.get("bot_settings", {})
        enabled = bs.get("ab_enabled", False)
        a = (bs.get("ab_variant_a") or "").strip()
        b = (bs.get("ab_variant_b") or "").strip()
        out = f"🅰️🅱️ **A/B тест** — {'✅ включён' if enabled else '❌ отключён'}\n\n"
        out += f"**A:** «{a if a else '—'}»\n\n"
        out += f"**B:** «{b if b else '—'}»"
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/ab_stats":
        if not _ab_available(user_data):
            await update.message.reply_text("❌ A/B доступен только на платных тарифах. /tariffs")
            return
        since = (datetime.now() - timedelta(days=7)).isoformat()
        stats = db_leads.get_ab_stats(user_id, since)
        a = stats.get("A", {"total": 0, "hot": 0})
        b = stats.get("B", {"total": 0, "hot": 0})

        if a["total"] + b["total"] == 0:
            await update.message.reply_text(
                "📊 За последние 7 дней нет данных A/B.\n"
                "Включите A/B и подождите новых лидов."
            )
            return

        def conv(s):
            return int(round(s["hot"] / s["total"] * 100)) if s["total"] else 0

        out = "🅰️🅱️ **A/B статистика (7 дней)**\n\n"
        out += f"**A:** {a['total']} лидов, {a['hot']} горячих ({conv(a)}%)\n"
        out += f"**B:** {b['total']} лидов, {b['hot']} горячих ({conv(b)}%)\n"

        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/ab_reset":
        if not _ab_available(user_data):
            await update.message.reply_text("❌ A/B доступен только на платных тарифах. /tariffs")
            return
        bs = user_data.get("bot_settings", {})
        bs["ab_variant_a"] = ""
        bs["ab_variant_b"] = ""
        bs["ab_enabled"] = False
        db_users.update(user_id, bot_settings=bs)
        await update.message.reply_text("✅ A/B сброшен (оба варианта очищены).")
        return

    # ===== ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ =====
    if first_word == "/weekly_on":
        s = user_data.get("bot_settings", {})
        s["weekly_report_enabled"] = True
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Еженедельный отчёт включён.")
        return

    if first_word == "/weekly_off":
        s = user_data.get("bot_settings", {})
        s["weekly_report_enabled"] = False
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("🔕 Еженедельный отчёт отключён.")
        return

    if first_word == "/weekly_report_now":
        report = _build_weekly_report(user_id)
        if not report:
            await update.message.reply_text(
                "📊 За последние 7 дней данных нет.\n"
                "Как только лиды начнут приходить — отчёт появится."
            )
        else:
            await update.message.reply_text(report, parse_mode="Markdown")
            chart_buf = _build_weekly_chart(user_id)
            if chart_buf:
                try:
                    await update.message.reply_photo(
                        photo=InputFile(chart_buf, filename="chart.png"),
                        caption="📈 График лидов"
                    )
                finally:
                    chart_buf.close()
        return

    # ===== РЕАНИМАТОР =====
    if first_word == "/followup_on":
        s = user_data.get("bot_settings", {})
        s["followup_enabled"] = True
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("✅ Реаниматор включён.")
        return

    if first_word == "/followup_off":
        s = user_data.get("bot_settings", {})
        s["followup_enabled"] = False
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text("🔕 Реаниматор отключён.")
        return

    if first_word == "/followup_status":
        s = user_data.get("bot_settings", {})
        enabled = s.get("followup_enabled", True)
        intervals = _get_followup_intervals(user_data)
        texts = s.get("auto_reply_texts") or []

        out = f"🔄 **Реаниматор** — {'✅ включён' if enabled else '❌ отключён'}\n\n"
        for i, sec in enumerate(intervals, 1):
            txt = texts[(i - 1) % len(texts)] if texts else "—"
            out += f"{i}. через {_fmt_duration(sec)} — «{txt[:50]}»\n"
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/followup_interval":
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await update.message.reply_text(
                "❌ Используйте: `/followup_interval <1-3> <секунд>`\n"
                "Пример: `/followup_interval 1 3600` (1 час)",
                parse_mode="Markdown"
            )
            return
        num = int(parts[1])
        sec = int(parts[2])
        if num < 1 or num > 3:
            await update.message.reply_text("❌ Номер попытки: 1, 2 или 3.")
            return
        if sec < 60:
            await update.message.reply_text("❌ Минимум 60 секунд.")
            return
        s = user_data.get("bot_settings", {})
        intervals = _get_followup_intervals(user_data)
        while len(intervals) < 3:
            intervals.append(DEFAULT_FOLLOWUP_INTERVALS[len(intervals)])
        intervals[num - 1] = sec
        s["followup_intervals"] = intervals
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text(
            f"✅ Попытка {num}: через {_fmt_duration(sec)}"
        )
        return

    if first_word == "/followup_text":
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text(
                "❌ Используйте: `/followup_text <1-3> <текст>`",
                parse_mode="Markdown"
            )
            return
        num = int(parts[1])
        new_text = parts[2].strip()
        if num < 1 or num > 3 or not new_text:
            await update.message.reply_text("❌ Номер 1-3 и непустой текст.")
            return
        s = user_data.get("bot_settings", {})
        texts = list(s.get("auto_reply_texts") or [])
        while len(texts) < 3:
            texts.append("📌 Ещё думаете? Я готов помочь!")
        texts[num - 1] = new_text
        s["auto_reply_texts"] = texts
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text(
            f"✅ Текст попытки {num} обновлён:\n«{new_text}»"
        )
        return

    if first_word == "/followup_reset":
        s = user_data.get("bot_settings", {})
        s.pop("followup_intervals", None)
        s["followup_enabled"] = True
        db_users.update(user_id, bot_settings=s)
        await update.message.reply_text(
            "✅ Реаниматор сброшен к дефолту (1 ч / 24 ч / 72 ч)."
        )
        return

    # ===== МУЛЬТИ-БОТ =====
    if first_word == "/add_bot":
        if len(parts) == 3:
            bots = user_data.get("bots", [])
            tariff = user_data.get("tariff", "trial")
            limit = BOT_LIMITS.get(tariff, 1)
            if len(bots) < limit:
                bots.append({"name": parts[1], "token": parts[2], "is_active": True})
                db_users.update(user_id, bots=bots)
                await update.message.reply_text(f"✅ Бот {parts[1]} добавлен.")
            else:
                await update.message.reply_text(f"❌ Лимит: {limit} ботов для тарифа {tariff}.")
        return

    if first_word == "/bots_list":
        bots = user_data.get("bots", [])
        if bots:
            out = "🤖 **Боты:**\n\n" + "\n".join([f"• {b.get('name', '—')}" for b in bots])
        else:
            out = "🤖 Нет ботов."
        await update.message.reply_text(out, parse_mode="Markdown")
        return

    if first_word == "/remove_bot":
        bot_name = text.replace("/remove_bot", "", 1).strip()
        if bot_name:
            bots = [b for b in user_data.get("bots", []) if b.get("name") != bot_name]
            db_users.update(user_id, bots=bots)
            await update.message.reply_text(f"✅ Удалён: {bot_name}")
        return

    # ===== CRM =====
    if first_word == "/crm_setup":
        await update.message.reply_text(
            "📊 **Настройка CRM**\n\n"
            "Откройте панель: Управление ботом → Интеграции → CRM.\n\n"
            "Или команды:\n"
            "`/crm_amocrm <subdomain> <token>`\n"
            "`/crm_bitrix <webhook_url>`\n"
            "`/crm_hubspot <api_key>`\n"
            "`/crm_test` — проверить\n"
            "`/crm_status` — статус\n"
            "`/crm_disable` — отключить",
            parse_mode="Markdown"
        )
        return

    if first_word == "/crm_amocrm":
        if len(parts) < 3:
            await update.message.reply_text(
                "❌ Используйте: `/crm_amocrm <subdomain> <token>`",
                parse_mode="Markdown"
            )
            return
        subdomain = parts[1].strip().replace("https://", "").replace(".amocrm.ru", "").strip("/")
        token = parts[2].strip()
        crm = {
            "enabled": True, "type": "amocrm",
            "subdomain": subdomain, "access_token": token,
            "webhook_url": "", "api_key": "",
        }
        msg = await update.message.reply_text("🔍 Проверяю подключение к AmoCRM...")
        ok, info = await crm_client.test_connection(crm)
        if not ok:
            await msg.edit_text(f"{info}\n\nПроверьте subdomain и токен.")
            return
        s = user_data.get("settings", {})
        s["crm_integration"] = crm
        db_users.update(user_id, settings=s)
        await msg.edit_text(
            f"{info}\n\n✅ **AmoCRM подключена.** Лиды будут уходить в CRM."
        )
        return

    if first_word == "/crm_bitrix":
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Используйте: `/crm_bitrix <webhook_url>`",
                parse_mode="Markdown"
            )
            return
        webhook = parts[1].strip()
        if not webhook.endswith("/"):
            webhook += "/"
        crm = {
            "enabled": True, "type": "bitrix24",
            "subdomain": "", "access_token": "",
            "webhook_url": webhook, "api_key": "",
        }
        msg = await update.message.reply_text("🔍 Проверяю подключение к Bitrix24...")
        ok, info = await crm_client.test_connection(crm)
        if not ok:
            await msg.edit_text(f"{info}\n\nПроверьте webhook URL.")
            return
        s = user_data.get("settings", {})
        s["crm_integration"] = crm
        db_users.update(user_id, settings=s)
        await msg.edit_text(
            f"{info}\n\n✅ **Bitrix24 подключён.** Лиды будут уходить в CRM."
        )
        return

    if first_word == "/crm_hubspot":
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Используйте: `/crm_hubspot <api_key>`",
                parse_mode="Markdown"
            )
            return
        token = parts[1].strip()
        crm = {
            "enabled": True, "type": "hubspot",
            "subdomain": "", "access_token": "",
            "webhook_url": "", "api_key": token,
        }
        msg = await update.message.reply_text("🔍 Проверяю подключение к HubSpot...")
        ok, info = await crm_client.test_connection(crm)
        if not ok:
            await msg.edit_text(f"{info}\n\nПроверьте API-ключ.")
            return
        s = user_data.get("settings", {})
        s["crm_integration"] = crm
        db_users.update(user_id, settings=s)
        await msg.edit_text(
            f"{info}\n\n✅ **HubSpot подключён.** Лиды будут уходить в CRM."
        )
        return

    if first_word == "/crm_test":
        crm = user_data.get("settings", {}).get("crm_integration", {})
        if not crm.get("enabled"):
            await update.message.reply_text("❌ CRM не настроена.")
            return
        msg = await update.message.reply_text("🔍 Проверяю подключение...")
        ok, info = await crm_client.test_connection(crm)
        await msg.edit_text(
            f"{info}\n\nСистема: `{crm.get('type')}`",
            parse_mode="Markdown"
        )
        return

    if first_word == "/crm_status":
        crm = user_data.get("settings", {}).get("crm_integration", {})
        enabled = crm.get("enabled")
        status = "✅ Включена" if enabled else "❌ Отключена"
        stats = db_crm_log.get_stats(user_id)
        await update.message.reply_text(
            f"📊 **CRM**\n\n"
            f"Статус: {status}\n"
            f"Система: {crm.get('type', 'Не выбрана')}\n"
            f"📤 Отправлено: {stats['success']}\n"
            f"❌ Ошибок: {stats['error']}",
            parse_mode="Markdown"
        )
        return

    if first_word == "/crm_disable":
        s = user_data.get("settings", {})
        crm = s.get("crm_integration", {})
        if crm:
            crm["enabled"] = False
            s["crm_integration"] = crm
            db_users.update(user_id, settings=s)
        await update.message.reply_text("✅ CRM отключена. Ключи сохранены.")
        return

    # ===== РАСПИСАНИЕ =====
    if first_word == "/schedule_on":
        s = user_data.get("schedules", {})
        s["enabled"] = True
        db_users.update(user_id, schedules=s)
        await update.message.reply_text("✅ Расписание включено.")
        return

    if first_word == "/schedule_off":
        s = user_data.get("schedules", {})
        s["enabled"] = False
        db_users.update(user_id, schedules=s)
        await update.message.reply_text("❌ Расписание выключено.")
        return

    if first_word == "/set_schedule":
        parts2 = text.replace("/set_schedule", "", 1).strip().split("-")
        if len(parts2) == 2:
            s = user_data.get("schedules", {})
            s["start_time"] = parts2[0].strip()
            s["end_time"] = parts2[1].strip()
            db_users.update(user_id, schedules=s)
            await update.message.reply_text(f"✅ Расписание: {parts2[0]} - {parts2[1]}")
        return

    # ===== ПОДДЕРЖКА / ИДЕИ =====
    if first_word == "/support_chat":
        context.user_data["awaiting_support"] = True
        await update.message.reply_text("📨 Напишите сообщение в поддержку:")
        return

    if first_word == "/suggest_idea":
        context.user_data["awaiting_idea"] = True
        await update.message.reply_text("💡 Напишите вашу идею:")
        return

    # ===== GOOGLE SHEETS =====
    if first_word == "/export_sheets":
        if len(parts) == 2:
            url = parts[1].strip()
            if not url.startswith("https://script.google.com/"):
                await update.message.reply_text(
                    "❌ URL должен начинаться с `https://script.google.com/`",
                    parse_mode="Markdown"
                )
                return

            msg = await update.message.reply_text("🔍 Проверяю webhook...")
            ok, info = await _push_to_sheets(url, [["test", "test"]])
            if not ok:
                await msg.edit_text(f"❌ Webhook не отвечает: `{info}`", parse_mode="Markdown")
                return

            s = user_data.get("bot_settings", {})
            s["sheets_webhook_url"] = url
            db_users.update(user_id, bot_settings=s)
            await msg.edit_text(
                f"✅ **Google Sheets подключён.**\n\n"
                f"Тестовая строка добавлена. Удали её из таблицы вручную.\n\n"
                f"Теперь: `/export_sheets` выгрузит всех лидов.",
                parse_mode="Markdown"
            )
            return

        s = user_data.get("bot_settings", {})
        url = s.get("sheets_webhook_url")
        if not url:
            await update.message.reply_text(
                "❌ Сначала подключи Google Sheets:\n"
                "`/export_sheets <webhook_url>`",
                parse_mode="Markdown"
            )
            return

        leads = db_leads.get_by_client(user_id, limit=500)
        if not leads:
            await update.message.reply_text("📊 Пока нет лидов для выгрузки.")
            return

        msg = await update.message.reply_text(f"📤 Выгружаю {len(leads)} лидов в Google Sheets...")
        rows = _leads_to_sheet_rows(leads)
        ok, info = await _push_to_sheets(url, rows)

        if ok:
            await msg.edit_text(
                f"✅ **Готово!** {info}\n\n"
                f"Проверь таблицу — лиды добавились в лист «Leads».",
                parse_mode="Markdown"
            )
        else:
            await msg.edit_text(f"❌ Ошибка: `{info}`", parse_mode="Markdown")
        return

    # ===== РУЧНАЯ ОПЛАТА КАРТОЙ =====
    if first_word == "/confirm_payment":
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💰 Пользователь `{user_id}` подтвердил оплату картой.\n"
                         f"Проверьте поступление и активируйте тариф через /set_tariff.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
        await update.message.reply_text(
            "📩 Заявка отправлена. Админ проверит поступление в течение 24 часов."
        )
        return

    # ===== ЭКСПОРТ =====
    if first_word == "/export":
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Используйте: `/export <day|week|month> [csv|excel|pdf]`",
                parse_mode="Markdown"
            )
            return

        period_parts = parts[1].split()
        period = period_parts[0].lower() if period_parts else ""
        fmt = (period_parts[1] if len(period_parts) > 1 else "csv").lower()

        if period not in ("day", "week", "month"):
            await update.message.reply_text("❌ Период: day, week, month")
            return
        if fmt not in ("csv", "excel", "pdf"):
            await update.message.reply_text("❌ Формат: csv, excel, pdf")
            return

        stats = db_stats.get_client_stats(user_id, period)
        if not stats:
            await update.message.reply_text("📊 Нет данных за этот период.")
            return

        try:
            if fmt == "csv":
                data = _build_csv(stats)
                await update.message.reply_document(
                    document=data, filename=f"stats_{period}.csv",
                    caption=f"📊 Статистика за {period} (CSV)"
                )
            elif fmt == "excel":
                data = _build_excel(stats)
                await update.message.reply_document(
                    document=data, filename=f"stats_{period}.xlsx",
                    caption=f"📊 Статистика за {period} (Excel)"
                )
            elif fmt == "pdf":
                data = _build_pdf(stats, period)
                await update.message.reply_document(
                    document=data, filename=f"stats_{period}.pdf",
                    caption=f"📊 Статистика за {period} (PDF)"
                )
        except Exception as e:
            logger.error(f"Ошибка экспорта ({fmt}): {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Не удалось сформировать {fmt.upper()}. Попробуйте CSV."
            )
        return

    await update.message.reply_text("Используйте меню или /help.", reply_markup=main_menu_keyboard(user_id))

# ========================================================================
# ФОНОВЫЕ ЗАДАЧИ
# ========================================================================

async def check_expired_tariffs(context: ContextTypes.DEFAULT_TYPE):
    expired = db_users.get_expired_tariffs()
    for user in expired:
        uid = user["user_id"]
        if is_admin(uid):
            continue
        if user.get("tariff") == "expired":
            continue

        db_users.update(uid, is_bot_active=False, tariff="expired")

        try:
            kill_existing_agent(uid)
        except Exception as e:
            logger.warning(f"[{uid}] Не удалось убить агента при истечении тарифа: {e}")

        try:
            await context.bot.send_message(
                chat_id=uid,
                text="⏳ **Ваш тариф истек.** Оплатите: /tariffs",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления {uid}: {e}")

async def process_notifications(context: ContextTypes.DEFAULT_TYPE):
    notifs = db_notifications.get_unsent()
    for n in notifs:
        try:
            await context.bot.send_message(
                chat_id=n["recipient_id"], text=n["message"], parse_mode="Markdown"
            )
            db_notifications.mark_sent(n["id"])
        except Forbidden:
            logger.warning(f"Уведомление {n['id']}: пользователь {n['recipient_id']} заблокировал бота")
            db_notifications.mark_sent(n["id"])
        except BadRequest as e:
            logger.error(f"Уведомление {n['id']}: BadRequest: {e}")
            db_notifications.mark_sent(n["id"])
        except Exception as e:
            logger.error(f"Уведомление {n['id']}: {e}")
            db_notifications.mark_sent(n["id"])

async def cleanup_notifications(context: ContextTypes.DEFAULT_TYPE):
    db_notifications.cleanup_old(days=7)

# ========================================================================
# MAIN
# ========================================================================

from andro_cfw import CFWSession

def main():
    # ... init_db() ...

    # Загружаем сессию Cloudflare Worker
    session = CFWSession.load()

    # Создаём приложение с прокси
    app = Application.builder().token(MAIN_BOT_TOKEN) \
        .base_url(session.ptb_base_url()) \
        .base_file_url(session.ptb_base_file_url()) \
        .build()

    # ... добавляем хендлеры ...
    app.run_polling()