# bot_admin.py
"""
АДМИН-БОТ.

Функции:
- Список клиентов с пагинацией
- Общая статистика
- Тикеты и идеи (с пагинацией)
- Смена тарифов, скидки, баны
- Ответы в поддержку и на идеи
"""

import os
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

from config import (
    ADMIN_BOT_TOKEN, ADMIN_IDS, TARIFFS, PAID_TARIFFS, is_configured
)
from database import (
    init_db, db_users, db_leads, db_stats, db_notifications
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("admin_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========================================================================
# ХЕЛПЕРЫ
# ========================================================================

PAGE_SIZE = 10

def is_admin(user_id) -> bool:
    """Безопасная проверка. Не падает на нечисловых ID."""
    try:
        return str(user_id) in [str(a) for a in ADMIN_IDS]
    except (ValueError, TypeError):
        return False

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Все клиенты", callback_data="users_0")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📨 Тикеты", callback_data="tickets_0")],
        [InlineKeyboardButton("💡 Идеи", callback_data="ideas_0")],
        [InlineKeyboardButton("💳 Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton("🚫 Баны", callback_data="bans")]
    ])

def back_to_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Меню", callback_data="menu")]
    ])

def nav_keyboard(prefix: str, page: int, total_pages: int):
    """Строит навигацию с границами."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{page-1}"))

    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{page+1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(keyboard)

# ========================================================================
# CALLBACK
# ========================================================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if not is_admin(uid):
        return

    # ===== СПИСОК КЛИЕНТОВ =====
    if data.startswith("users_"):
        try:
            page = int(data.replace("users_", ""))
        except ValueError:
            page = 0

        total = db_users.count()
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        page_users = db_users.get_paginated(page=page, size=PAGE_SIZE)

        text = f"📋 **Клиенты** ({total}) — стр. {page + 1}/{total_pages}\n\n"
        for u in page_users:
            tariff = u.get("tariff", "trial")
            ban = "🚫" if u.get("is_banned") else "✅"
            bot = "🤖" if u.get("bot_token") else "🔴"
            uname = u.get("username") or "—"
            text += f"{ban}{bot} `{u['user_id']}` @{uname} — {tariff}\n"

        if not page_users:
            text += "Пусто."

        await q.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=nav_keyboard("users", page, total_pages)
        )
        return

    # ===== ОБЩАЯ СТАТИСТИКА =====
    if data == "stats":
        total_users = db_users.count()
        users = db_users.get_all()

        active = sum(1 for u in users if u.get("tariff") not in ("trial", "expired"))
        trial = sum(1 for u in users if u.get("tariff") == "trial")
        banned = sum(1 for u in users if u.get("is_banned"))
        with_bot = sum(1 for u in users if u.get("bot_token"))

        total_leads, total_hot = db_leads.get_global_stats()

        text = (
            f"📊 **Статистика:**\n\n"
            f"👥 Всего клиентов: {total_users}\n"
            f"✅ Активных: {active}\n"
            f"🔄 На пробном: {trial}\n"
            f"🚫 Забанено: {banned}\n"
            f"🤖 С ботом: {with_bot}\n\n"
            f"📈 Лидов всего: {total_leads}\n"
            f"🔥 Горячих: {total_hot}"
        )
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu())
        return

    # ===== ТИКЕТЫ =====
    if data.startswith("tickets_"):
        try:
            page = int(data.replace("tickets_", ""))
        except ValueError:
            page = 0

        users = db_users.get_all()
        all_tickets = []
        for u in users:
            for r in (u.get("support_requests") or []):
                if r.get("status") == "open":
                    all_tickets.append({
                        "user_id": u["user_id"],
                        "message": r.get("message", ""),
                        "created_at": r.get("created_at", "")
                    })

        total = len(all_tickets)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start_idx = page * PAGE_SIZE
        page_tickets = all_tickets[start_idx:start_idx + PAGE_SIZE]

        text = f"📨 **Открытые тикеты** ({total}) — стр. {page + 1}/{total_pages}\n\n"
        if page_tickets:
            for t in page_tickets:
                text += f"• `{t['user_id']}`: {t['message'][:60]}...\n"
            text += "\n📌 Для ответа: `/reply_support <user_id> <текст>`"
        else:
            text += "Нет открытых тикетов."

        await q.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=nav_keyboard("tickets", page, total_pages)
        )
        return

    # ===== ИДЕИ =====
    if data.startswith("ideas_"):
        try:
            page = int(data.replace("ideas_", ""))
        except ValueError:
            page = 0

        users = db_users.get_all()
        all_ideas = []
        for u in users:
            for i in (u.get("ideas") or []):
                if i.get("status") == "pending":
                    all_ideas.append({
                        "user_id": u["user_id"],
                        "text": i.get("text", ""),
                        "created_at": i.get("created_at", "")
                    })

        total = len(all_ideas)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start_idx = page * PAGE_SIZE
        page_ideas = all_ideas[start_idx:start_idx + PAGE_SIZE]

        text = f"💡 **Новые идеи** ({total}) — стр. {page + 1}/{total_pages}\n\n"
        if page_ideas:
            for i in page_ideas:
                text += f"• `{i['user_id']}`: {i['text'][:60]}...\n"
            text += "\n📌 Для ответа: `/reply_idea <user_id> <текст>`"
        else:
            text += "Нет новых идей."

        await q.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=nav_keyboard("ideas", page, total_pages)
        )
        return

    # ===== ТАРИФЫ =====
    if data == "tariffs":
        text = (
            "💳 **Управление тарифами:**\n\n"
            "`/set_tariff <user_id> <basic|pro|unlimited>`\n"
            "`/set_trial <user_id> <дни>`\n"
            "`/give_discount <user_id> <0-40>`\n\n"
            "`trial` выдаётся только через `/set_trial`."
        )
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu())
        return

    # ===== БАНЫ =====
    if data == "bans":
        text = (
            "🚫 **Управление банами:**\n\n"
            "`/ban <user_id>`\n"
            "`/unban <user_id>`"
        )
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu())
        return

    # ===== МЕНЮ =====
    if data == "menu":
        await q.message.edit_text("⚙️ **Админ-панель**", reply_markup=admin_menu())
        return

    if data == "noop":
        return

    await q.message.edit_text("⚠️ Команда не найдена.", reply_markup=back_to_menu())

# ========================================================================
# /start
# ========================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text("⚙️ **Админ-панель**", reply_markup=admin_menu())

# ========================================================================
# КОМАНДЫ
# ========================================================================

async def commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    text = (update.message.text or "").strip()

    # ===== /set_tariff =====
    if text.startswith("/set_tariff"):
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/set_tariff 123456 basic`")
            return

        target = int(parts[1])
        tariff = parts[2].lower()

        if tariff not in PAID_TARIFFS:
            await update.message.reply_text(
                f"❌ Неверный тариф. Доступны: {', '.join(PAID_TARIFFS)}"
            )
            return

        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(
            target,
            tariff=tariff,
            tariff_expires=(datetime.now() + timedelta(days=30)).isoformat(),
            is_bot_active=True
        )
        await update.message.reply_text(
            f"✅ Тариф пользователя `{target}` → **{tariff}**",
            parse_mode="Markdown"
        )
        return

    # ===== /set_trial =====
    if text.startswith("/set_trial"):
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await update.message.reply_text("❌ Используйте: `/set_trial 123456 3`")
            return

        target = int(parts[1])
        days = int(parts[2])

        if days < 1 or days > 365:
            await update.message.reply_text("❌ Дней: от 1 до 365.")
            return

        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(
            target,
            tariff="trial",
            tariff_expires=(datetime.now() + timedelta(days=days)).isoformat(),
            is_bot_active=True
        )
        await update.message.reply_text(
            f"✅ Пробный период для `{target}` на {days} дней.",
            parse_mode="Markdown"
        )
        return

    # ===== /give_discount =====
    if text.startswith("/give_discount"):
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await update.message.reply_text("❌ Используйте: `/give_discount 123456 20`")
            return

        target = int(parts[1])
        percent = int(parts[2])

        if percent < 0 or percent > 40:
            await update.message.reply_text("❌ Скидка: от 0 до 40%.")
            return

        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(target, manual_discount=percent)
        await update.message.reply_text(
            f"✅ Пользователю `{target}` установлена скидка {percent}%",
            parse_mode="Markdown"
        )
        return

    # ===== /ban =====
    if text.startswith("/ban"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/ban 123456`")
            return

        target = int(parts[1])
        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(target, is_banned=True, is_bot_active=False)
        await update.message.reply_text(
            f"🚫 Пользователь `{target}` забанен.",
            parse_mode="Markdown"
        )
        return

    # ===== /unban =====
    if text.startswith("/unban"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/unban 123456`")
            return

        target = int(parts[1])
        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(target, is_banned=False, is_bot_active=True)
        await update.message.reply_text(
            f"✅ Пользователь `{target}` разбанен.",
            parse_mode="Markdown"
        )
        return

    # ===== /user =====
    if text.startswith("/user"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/user 123456`")
            return

        target = int(parts[1])
        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        total_leads = db_leads.count_by_client(target)
        hot_leads = db_leads.count_hot_by_client(target)
        expires = (u.get("tariff_expires") or "")[:10]

        info = (
            f"👤 **Клиент** `{u['user_id']}`\n\n"
            f"Username: @{u.get('username') or '—'}\n"
            f"Имя: {u.get('first_name') or '—'}\n"
            f"Тариф: **{u.get('tariff', 'trial')}**\n"
            f"До: {expires or '—'}\n"
            f"Бот: {'✅' if u.get('bot_token') else '❌'}\n"
            f"Забанен: {'🚫' if u.get('is_banned') else '✅'}\n\n"
            f"📈 Лидов: {total_leads}\n"
            f"🔥 Горячих: {hot_leads}\n"
            f"👥 Менеджеров: {len(u.get('managers', []))}\n"
            f"📚 RAG: {len(u.get('rag_data', []))}\n"
            f"🎁 Manual discount: {u.get('manual_discount', 0)}%"
        )
        await update.message.reply_text(info, parse_mode="Markdown")
        return

    # ===== /reply_support =====
    if text.startswith("/reply_support"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text(
                "❌ Используйте: `/reply_support 123456 текст`"
            )
            return

        target = int(parts[1])
        reply = parts[2]

        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        # Закрываем все открытые тикеты
        reqs = u.get("support_requests") or []
        closed = 0
        for r in reqs:
            if r.get("status") == "open":
                r["status"] = "closed"
                r["closed_at"] = datetime.now().isoformat()
                closed += 1
        if closed:
            db_users.update(target, support_requests=reqs)

        # Уведомление через БД (bot_main разошлёт)
        db_notifications.add(target, f"💬 **Ответ поддержки:**\n\n{reply}")

        await update.message.reply_text(
            f"✅ Ответ отправлен в очередь для `{target}`. Закрыто тикетов: {closed}",
            parse_mode="Markdown"
        )
        return

    # ===== /reply_idea =====
    if text.startswith("/reply_idea"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text(
                "❌ Используйте: `/reply_idea 123456 текст`"
            )
            return

        target = int(parts[1])
        reply = parts[2]

        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        # Помечаем последнюю pending-идею как reviewed
        ideas = u.get("ideas") or []
        for i in reversed(ideas):
            if i.get("status") == "pending":
                i["status"] = "reviewed"
                i["reviewed_at"] = datetime.now().isoformat()
                break
        db_users.update(target, ideas=ideas)

        # Уведомление через БД
        db_notifications.add(target, f"💡 **Ответ на вашу идею:**\n\n{reply}")

        await update.message.reply_text(
            f"✅ Ответ отправлен в очередь для `{target}`.",
            parse_mode="Markdown"
        )
        return

    # ===== Справка =====
    await update.message.reply_text(
        "⚙️ **Команды админа:**\n\n"
        "`/user <id>` — информация о клиенте\n"
        "`/set_tariff <id> <tariff>` — сменить тариф\n"
        "`/set_trial <id> <дни>` — пробный\n"
        "`/give_discount <id> <процент>` — скидка\n"
        "`/ban <id>` — бан\n"
        "`/unban <id>` — разбан\n"
        "`/reply_support <id> <текст>` — ответ в поддержку\n"
        "`/reply_idea <id> <текст>` — ответ на идею"
    )

# ========================================================================
# MAIN
# ========================================================================

def main():
    if not is_configured():
        logger.error("❌ Конфиг не заполнен! Проверь .env")
        return

    if not ADMIN_BOT_TOKEN or ADMIN_BOT_TOKEN == "YOUR_ADMIN_BOT_TOKEN":
        logger.error("❌ ADMIN_BOT_TOKEN не задан!")
        return

    init_db()

    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, commands))

    logger.info("🕵️ Админ-бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()