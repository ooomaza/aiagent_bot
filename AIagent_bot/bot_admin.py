# bot_admin.py
"""
АДМИН-БОТ.
- Иерархическое меню (Аналитика, Клиенты, Инструменты)
- Список клиентов с пагинацией
- Общая статистика
- Платежи (статистика по звёздам)
- Тикеты и идеи (с пагинацией)
- Смена тарифов, скидки, баны
- Ответы в поддержку и на идеи
- /restart_agent <id> — перезапуск упавшего агента
- /agents — сводка по всем агентам
- check_agent_health — фоновый авто-рестарт и алерты
"""

import os
import sys
import logging
import subprocess
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
    init_db, db_users, db_leads, db_stats, db_notifications, db_payments
)
from agent_runner import (
    kill_existing_agent, activate_client_bot,
    is_agent_alive
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
    try:
        return str(user_id) in [str(a) for a in ADMIN_IDS]
    except (ValueError, TypeError):
        return False

def _agent_status(client_id: int) -> str:
    """'running' | 'dead' | 'no_pid_file'."""
    pid_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"agent_{client_id}.pid"
    )
    if not os.path.exists(pid_file):
        return "no_pid_file"

    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return "no_pid_file"

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            alive = ("No tasks are running" not in result.stdout
                     and str(pid) in result.stdout)
        else:
            os.kill(pid, 0)
            alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    except Exception:
        alive = False

    return "running" if alive else "dead"

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Аналитика (Статистика, Платежи)", callback_data="admin_analytics")],
        [InlineKeyboardButton("👥 Клиенты (Список, Тарифы, Баны)", callback_data="admin_clients")],
        [InlineKeyboardButton("🛠 Инструменты (Агенты, Тикеты, Идеи)", callback_data="admin_tools")],
    ])

def back_to_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])

def nav_keyboard(prefix: str, page: int, total_pages: int, back_target: str = "menu"):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{page+1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=back_target)])
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

    try:
        # ===== ПОДМЕНЮ =====
        if data == "admin_analytics":
            await q.message.edit_text(
                "📊 **Аналитика**\nВыберите раздел:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📈 Общая статистика", callback_data="stats")],
                    [InlineKeyboardButton("💳 Платежи и выручка", callback_data="payments")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
                ])
            )
            return

        if data == "admin_clients":
            await q.message.edit_text(
                "👥 **Клиенты**\nВыберите раздел:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Список клиентов", callback_data="users_0")],
                    [InlineKeyboardButton("💳 Управление тарифами", callback_data="tariffs")],
                    [InlineKeyboardButton("🚫 Баны", callback_data="bans")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
                ])
            )
            return

        if data == "admin_tools":
            await q.message.edit_text(
                "🛠 **Инструменты**\nВыберите раздел:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 Состояние агентов", callback_data="agents")],
                    [InlineKeyboardButton("📨 Тикеты", callback_data="tickets_0")],
                    [InlineKeyboardButton("💡 Идеи", callback_data="ideas_0")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
                ])
            )
            return

        # ===== КЛИЕНТЫ =====
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
                reply_markup=nav_keyboard("users", page, total_pages, back_target="admin_clients")
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
            pay_count, pay_stars = db_payments.get_totals()
            net_rub = int(pay_stars * 1.17)

            text = (
                f"📊 **Статистика:**\n\n"
                f"👥 Всего клиентов: {total_users}\n"
                f"✅ Активных: {active}\n"
                f"🔄 На пробном: {trial}\n"
                f"🚫 Забанено: {banned}\n"
                f"🤖 С ботом: {with_bot}\n\n"
                f"📈 Лидов всего: {total_leads}\n"
                f"🔥 Горячих: {total_hot}\n\n"
                f"💳 Платежей: {pay_count}\n"
                f"⭐ Звёзд: {pay_stars}\n"
                f"💰 ~Выручка: {net_rub} ₽ (net)"
            )
            await q.message.edit_text(
                text, parse_mode="Markdown", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_analytics")]])
            )
            return

        # ===== АГЕНТЫ =====
        if data == "agents":
            users = db_users.get_all()
            lines = ["🤖 **Агенты клиентов:**\n"]
            running = dead = inactive = 0

            for u in users:
                if not u.get("bot_token"):
                    continue

                if u.get("is_banned"):
                    lines.append(f"🚫 `{u['user_id']}` — забанен")
                    inactive += 1
                    continue

                if not u.get("is_bot_active"):
                    lines.append(f"⏸ `{u['user_id']}` — выключен")
                    inactive += 1
                    continue

                if u.get("tariff") in ("expired",):
                    lines.append(f"⏳ `{u['user_id']}` — тариф истёк")
                    inactive += 1
                    continue

                status = _agent_status(u["user_id"])
                if status == "running":
                    lines.append(f"✅ `{u['user_id']}` — работает")
                    running += 1
                elif status == "dead":
                    lines.append(f"❌ `{u['user_id']}` — упал")
                    dead += 1
                else:
                    lines.append(f"🔴 `{u['user_id']}` — нет PID-файла")
                    dead += 1

            if running + dead + inactive == 0:
                lines.append("Нет клиентов с ботом.")
            else:
                lines.append(f"\n📊 Работают: {running} / Упали: {dead} / Выкл: {inactive}")
                if dead:
                    lines.append("📌 Перезапуск: `/restart_agent <id>`")

            await q.message.edit_text(
                "\n".join(lines), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_tools")]])
            )
            return

        # ===== ПЛАТЕЖИ =====
        if data == "payments":
            total_count, total_stars = db_payments.get_totals()
            if total_count == 0:
                await q.message.edit_text(
                    "💳 Платежей пока нет.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_analytics")]])
                )
                return
            net_rub = int(total_stars * 1.17)
            text = (
                f"💳 **Платежи звёздами:**\n\n"
                f"Количество: {total_count}\n"
                f"Всего звёзд: {total_stars} ⭐\n"
                f"Примерная выручка: ~{net_rub} ₽ (net)\n\n"
                f"📌 Детализация: `/payments <user_id>`"
            )
            await q.message.edit_text(
                text, parse_mode="Markdown", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_analytics")]])
            )
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
            page_tickets = all_tickets[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

            text = f"📨 **Открытые тикеты** ({total}) — стр. {page + 1}/{total_pages}\n\n"
            if page_tickets:
                for t in page_tickets:
                    text += f"• `{t['user_id']}`: {t['message'][:60]}...\n"
                text += "\n📌 Для ответа: `/reply_support <user_id> <текст>`"
            else:
                text += "Нет открытых тикетов."

            await q.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=nav_keyboard("tickets", page, total_pages, back_target="admin_tools")
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
            page_ideas = all_ideas[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

            text = f"💡 **Новые идеи** ({total}) — стр. {page + 1}/{total_pages}\n\n"
            if page_ideas:
                for i in page_ideas:
                    text += f"• `{i['user_id']}`: {i['text'][:60]}...\n"
                text += "\n📌 Для ответа: `/reply_idea <user_id> <текст>`"
            else:
                text += "Нет новых идей."

            await q.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=nav_keyboard("ideas", page, total_pages, back_target="admin_tools")
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
            await q.message.edit_text(
                text, parse_mode="Markdown", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_clients")]])
            )
            return

        # ===== БАНЫ =====
        if data == "bans":
            text = (
                "🚫 **Управление банами:**\n\n"
                "`/ban <user_id>` — бан + остановка агента\n"
                "`/unban <user_id>` — разбан + перезапуск агента\n"
                "`/restart_agent <user_id>` — перезапуск вручную"
            )
            await q.message.edit_text(
                text, parse_mode="Markdown", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_clients")]])
            )
            return

        if data == "menu":
            await q.message.edit_text("⚙️ **Админ-панель**", reply_markup=admin_menu())
            return

        if data == "noop":
            return

        await q.message.edit_text("⚠️ Команда не найдена.", reply_markup=back_to_menu())

    except Exception as e:
        logger.error(f"Ошибка в callback: {e}")

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
    parts = text.split(maxsplit=2)
    first_word = parts[0] if parts else ""

    if first_word == "/start":
        return

    # ===== /payments =====
    if first_word == "/payments":
        if len(parts) == 1:
            total_count, total_stars = db_payments.get_totals()
            if total_count == 0:
                await update.message.reply_text("💳 Платежей пока нет.")
                return
            net_rub = int(total_stars * 1.17)
            out = (
                f"💳 **Платежи звёздами:**\n\n"
                f"Количество: {total_count}\n"
                f"Всего звёзд: {total_stars} ⭐\n"
                f"Примерная выручка: ~{net_rub} ₽ (net)\n\n"
                f"📌 Детализация: `/payments <user_id>`"
            )
            await update.message.reply_text(out, parse_mode="Markdown")
            return

        if len(parts) == 2 and parts[1].isdigit():
            target = int(parts[1])
            history = db_payments.get_by_user(target, limit=20)
            if not history:
                await update.message.reply_text(f"💳 У `{target}` платежей нет.")
                return
            total = sum(p["amount_stars"] for p in history)
            out = f"💳 **Платежи клиента** `{target}`:\n\n"
            for p in history:
                date = (p.get("created_at") or "")[:10]
                tariff_name = TARIFFS.get(p["tariff"], {}).get("name", p["tariff"])
                out += f"• {date} — {tariff_name} — {p['amount_stars']} ⭐\n"
            out += f"\n💰 Всего: {total} ⭐"
            await update.message.reply_text(out, parse_mode="Markdown")
            return

    # ===== /agents =====
    if first_word == "/agents" and len(parts) == 1:
        users = db_users.get_all()
        lines = ["🤖 **Агенты:**\n"]
        running = dead = inactive = 0

        for u in users:
            if not u.get("bot_token"):
                continue
            if u.get("is_banned"):
                lines.append(f"🚫 `{u['user_id']}` — забанен")
                inactive += 1
                continue
            if not u.get("is_bot_active"):
                lines.append(f"⏸ `{u['user_id']}` — выключен")
                inactive += 1
                continue
            if u.get("tariff") in ("expired",):
                lines.append(f"⏳ `{u['user_id']}` — истёк")
                inactive += 1
                continue

            status = _agent_status(u["user_id"])
            if status == "running":
                lines.append(f"✅ `{u['user_id']}` — работает")
                running += 1
            elif status == "dead":
                lines.append(f"❌ `{u['user_id']}` — упал")
                dead += 1
            else:
                lines.append(f"🔴 `{u['user_id']}` — нет PID")
                dead += 1

        if running + dead + inactive == 0:
            lines.append("Нет клиентов с ботом.")
        else:
            lines.append(f"\n📊 Работают: {running} / Упали: {dead} / Выкл: {inactive}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # ===== /restart_agent =====
    if first_word == "/restart_agent":
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/restart_agent 123456`")
            return

        target = int(parts[1])
        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        if not u.get("bot_token"):
            await update.message.reply_text("❌ У клиента нет токена бота.")
            return

        if u.get("is_banned"):
            await update.message.reply_text("❌ Клиент забанен. Сначала /unban.")
            return

        msg = await update.message.reply_text("🔄 Перезапускаю агента...")
        ok = activate_client_bot(target, u["bot_token"])
        if ok:
            await msg.edit_text(f"✅ Агент `{target}` перезапущен.", parse_mode="Markdown")
        else:
            await msg.edit_text("❌ Не удалось перезапустить. Смотри `agent_<id>.log`.")
        return

    # ===== /set_tariff =====
    if first_word == "/set_tariff":
        if len(parts) != 3 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/set_tariff 123456 basic`")
            return

        target = int(parts[1])
        tariff = parts[2].lower()

        if tariff not in PAID_TARIFFS:
            await update.message.reply_text(f"❌ Неверный тариф. Доступны: {', '.join(PAID_TARIFFS)}")
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

        u = db_users.get(target)
        if u and u.get("bot_token"):
            activate_client_bot(target, u["bot_token"])

        await update.message.reply_text(f"✅ Тариф пользователя `{target}` → **{tariff}**", parse_mode="Markdown")
        return

    # ===== /set_trial =====
    if first_word == "/set_trial":
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
        await update.message.reply_text(f"✅ Пробный период для `{target}` на {days} дней.", parse_mode="Markdown")
        return

    # ===== /give_discount =====
    if first_word == "/give_discount":
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
        await update.message.reply_text(f"✅ Пользователю `{target}` установлена скидка {percent}%", parse_mode="Markdown")
        return

    # ===== /ban =====
    if first_word == "/ban":
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/ban 123456`")
            return

        target = int(parts[1])
        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(target, is_banned=True, is_bot_active=False)
        kill_existing_agent(target)

        await update.message.reply_text(f"🚫 Пользователь `{target}` забанен. Агент остановлен.", parse_mode="Markdown")
        return

    # ===== /unban =====
    if first_word == "/unban":
        if len(parts) != 2 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/unban 123456`")
            return

        target = int(parts[1])
        if not db_users.get(target):
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        db_users.update(target, is_banned=False, is_bot_active=True)

        u = db_users.get(target)
        restarted = False
        if u and u.get("bot_token"):
            restarted = activate_client_bot(target, u["bot_token"])

        status = "\n🤖 Агент перезапущен." if restarted else "\n⚠️ Токен не задан."
        await update.message.reply_text(f"✅ Пользователь `{target}` разбанен.{status}", parse_mode="Markdown")
        return

    # ===== /user =====
    if first_word == "/user":
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
        pay_history = db_payments.get_by_user(target, limit=1)
        last_payment = "—"
        if pay_history:
            p = pay_history[0]
            last_payment = f"{p['amount_stars']} ⭐ ({(p.get('created_at') or '')[:10]})"

        agent_status = _agent_status(target)

        info = (
            f"👤 **Клиент** `{u['user_id']}`\n\n"
            f"Username: @{u.get('username') or '—'}\n"
            f"Имя: {u.get('first_name') or '—'}\n"
            f"Тариф: **{u.get('tariff', 'trial')}**\n"
            f"До: {expires or '—'}\n"
            f"Бот: {'✅' if u.get('bot_token') else '❌'}\n"
            f"Агент: {agent_status}\n"
            f"Забанен: {'🚫' if u.get('is_banned') else '✅'}\n\n"
            f"📈 Лидов: {total_leads}\n"
            f"🔥 Горячих: {hot_leads}\n"
            f"👥 Менеджеров: {len(u.get('managers', []))}\n"
            f"📚 RAG: {len(u.get('rag_data', []))}\n"
            f"🎁 Manual discount: {u.get('manual_discount', 0)}%\n"
            f"💳 Последний платёж: {last_payment}"
        )
        await update.message.reply_text(info, parse_mode="Markdown")
        return

    # ===== /reply_support =====
    if first_word == "/reply_support":
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/reply_support 123456 текст`")
            return

        target = int(parts[1])
        reply = parts[2]

        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        reqs = u.get("support_requests") or []
        closed = 0
        for r in reqs:
            if r.get("status") == "open":
                r["status"] = "closed"
                r["closed_at"] = datetime.now().isoformat()
                closed += 1
        if closed:
            db_users.update(target, support_requests=reqs)

        db_notifications.add(target, f"💬 **Ответ поддержки:**\n\n{reply}")

        await update.message.reply_text(f"✅ Ответ отправлен в очередь для `{target}`. Закрыто тикетов: {closed}", parse_mode="Markdown")
        return

    # ===== /reply_idea =====
    if first_word == "/reply_idea":
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text("❌ Используйте: `/reply_idea 123456 текст`")
            return

        target = int(parts[1])
        reply = parts[2]

        u = db_users.get(target)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден.")
            return

        ideas = u.get("ideas") or []
        for i in reversed(ideas):
            if i.get("status") == "pending":
                i["status"] = "reviewed"
                i["reviewed_at"] = datetime.now().isoformat()
                break
        db_users.update(target, ideas=ideas)

        db_notifications.add(target, f"💡 **Ответ на вашу идею:**\n\n{reply}")

        await update.message.reply_text(f"✅ Ответ отправлен в очередь для `{target}`.", parse_mode="Markdown")
        return

    # ===== Справка =====
    await update.message.reply_text(
        "⚙️ **Команды админа:**\n\n"
        "`/user <id>` — информация о клиенте\n"
        "`/agents` — сводка по агентам\n"
        "`/restart_agent <id>` — перезапуск агента\n"
        "`/payments` — статистика по звёздам\n"
        "`/payments <id>` — платежи клиента\n"
        "`/set_tariff <id> <tariff>` — сменить тариф\n"
        "`/set_trial <id> <дни>` — пробный\n"
        "`/give_discount <id> <процент>` — скидка\n"
        "`/ban <id>` — бан (агент останавливается)\n"
        "`/unban <id>` — разбан (агент перезапускается)\n"
        "`/reply_support <id> <текст>` — ответ в поддержку\n"
        "`/reply_idea <id> <текст>` — ответ на идею"
    )

# ========================================================================
# ФОНОВАЯ ПРОВЕРКА АГЕНТОВ
# ========================================================================

async def check_agent_health(context: ContextTypes.DEFAULT_TYPE):
    """
    Раз в 5 минут проверяет агентов. Если упал — пытается перезапустить.
    Если перезапустить не удалось — пишет админу.
    """
    users = db_users.get_all()
    dead = []

    for u in users:
        if u.get("is_banned"):
            continue
        if not u.get("is_bot_active"):
            continue
        if not u.get("bot_token"):
            continue
        if u.get("tariff") in ("expired",):
            continue

        status = _agent_status(u["user_id"])
        if status != "running":
            # АВТО-РЕСТАРТ
            success = activate_client_bot(u["user_id"], u["bot_token"])
            if success:
                logger.info(f"Авто-рестарт агента {u['user_id']} прошел успешно.")
            else:
                dead.append((u["user_id"], u.get("username"), status))

    if not dead:
        return

    # Если даже авто-рестарт не помог - пишем админу
    text = f"⚠️ **Агенты упали и не поднялись ({len(dead)}):**\n\n"
    for uid, uname, status in dead[:10]:
        text += f"❌ `{uid}` @{uname or '—'} — {status}\n"
    
    text += "\n\n📌 Ручной перезапуск: `/restart_agent <id>`"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

    logger.warning(f"Обнаружено {len(dead)} неработающих агентов")

# ========================================================================
# MAIN
# ========================================================================

from andro_cfw import CFWSession

def main():
    # ... init_db() ...
    session = CFWSession.load()
    app = Application.builder().token(ADMIN_BOT_TOKEN) \
        .base_url(session.ptb_base_url()) \
        .base_file_url(session.ptb_base_file_url()) \
        .build()
    # ...