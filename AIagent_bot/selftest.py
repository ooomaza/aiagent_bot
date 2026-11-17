# selftest.py
"""
Комплексный selftest проекта AI Manager.

Проверяет всё, что можно проверить БЕЗ Telegram и без VPN:
- Синтаксис всех .py файлов
- secure_config (шифрование AES-GCM)
- database (CRUD, миграции, A/B-статистика, платежи, CRM-логи)
- bot_main (экспорт Excel/PDF/CSV, weekly report, инсайты, A/B блок)
- crm_client (обработка ошибок на невалидных данных)
- agent_runner (валидация токена, проверка PID)
- bot_agent (detect_language, is_within_schedule через subprocess)

Запуск:
    python selftest.py

Вывод:
    ✅ / ❌ по каждому пункту + итоговая сводка.
    Код возврата: 0 если всё зелёное, 1 если были падения.

Безопасность:
- Использует тестовый user_id = 999999999 (никогда не пересечётся с реальными)
- Все вставленные данные удаляются в конце
- Excel/PDF сохраняются в selftest_output/ (можно открыть глазами)
"""

import os
import sys
import subprocess
import traceback
import json
import sqlite3
from datetime import datetime, timedelta

# ========================================================================
# РАМКА ТЕСТОВ
# ========================================================================

TEST_USER_ID = 999999999
OUTPUT_DIR = "selftest_output"

results = []  # список (section, name, ok, error)

def section(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

def check(name: str, condition: bool, error: str = ""):
    """Регистрирует результат одного теста."""
    current_section = _get_current_section()
    ok = bool(condition)
    results.append((current_section, name, ok, error if not ok else ""))
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}"
    if not ok and error:
        line += f"  ← {error}"
    print(line)
    return ok

_current_section = ["init"]
def _get_current_section():
    return _current_section[0]

def set_section(name):
    _current_section[0] = name

def run_check(name: str, fn):
    """Запускает функцию, ловит исключения, регистрирует как тест."""
    try:
        result = fn()
        return check(name, result is not False, "" if result is not False else "вернул False")
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return False

def cleanup():
    """Удаляет все тестовые данные."""
    try:
        c = sqlite3.connect("fox_bot.db")
        c.execute("DELETE FROM users WHERE user_id = ?", (TEST_USER_ID,))
        c.execute("DELETE FROM leads WHERE client_id = ?", (TEST_USER_ID,))
        c.execute("DELETE FROM stats WHERE client_id = ?", (TEST_USER_ID,))
        c.execute("DELETE FROM notifications WHERE recipient_id = ?", (TEST_USER_ID,))
        c.execute("DELETE FROM payments WHERE user_id = ?", (TEST_USER_ID,))
        c.execute("DELETE FROM crm_log WHERE client_id = ?", (TEST_USER_ID,))
        c.commit()
        c.close()
    except Exception as e:
        print(f"  ⚠️ cleanup: {e}")

# ========================================================================
# 1. СИНТАКСИС ВСЕХ ФАЙЛОВ
# ========================================================================

def test_syntax():
    section("1. СИНТАКСИС ФАЙЛОВ")
    set_section("syntax")

    files = [
        "config.py",
        "secure_config.py",
        "database.py",
        "agent_runner.py",
        "crm_client.py",
        "bot_main.py",
        "bot_agent.py",
        "bot_admin.py",
    ]

    import ast
    for f in files:
        if not os.path.exists(f):
            check(f"{f} существует", False, "файл не найден")
            continue

        def _check(f=f):
            with open(f, encoding="utf-8") as fh:
                ast.parse(fh.read())
            return True

        run_check(f"{f}: синтаксис", _check)

# ========================================================================
# 2. SECURE_CONFIG — ШИФРОВАНИЕ
# ========================================================================

def test_secure_config():
    section("2. SECURE_CONFIG — ШИФРОВАНИЕ AES-256-GCM")
    set_section("secure_config")

    import secure_config as sc

    def _roundtrip():
        plaintext = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
        encrypted = sc.encrypt(plaintext)
        if not encrypted:
            return False
        decrypted = sc.decrypt(encrypted)
        return decrypted == plaintext

    run_check("encrypt → decrypt round-trip", _roundtrip)

    def _unique():
        a = sc.encrypt("same")
        b = sc.encrypt("same")
        return a != b  # nonce разный → шифротексты разные

    run_check("Два шифрования одного текста дают разные шифротексты (nonce)", _unique)

    def _empty():
        return sc.encrypt("") == "" and sc.decrypt("") == ""

    run_check("Пустая строка → пустая (не ошибка)", _empty)

    def _corrupt():
        bad = sc.decrypt("это не base64!!!")
        return bad is None

    run_check("Битый шифротекст → None (не пустая строка)", _corrupt)

    def _files_exist():
        # secret.key создаётся при первом encrypt(),
        # secret.salt — при первом hash_password().
        # Триггерим создание обоих, потом проверяем.
        sc.hash_password("trigger_salt")
        key_ok = os.path.exists("secret.key") and os.path.getsize("secret.key") == 32
        salt_ok = os.path.exists("secret.salt") and os.path.getsize("secret.salt") == 16
        return key_ok and salt_ok

    run_check("secret.key (32B) и secret.salt (16B) создаются", _files_exist)

    def _hash():
        h = sc.hash_password("test123")
        return sc.verify_password("test123", h) and not sc.verify_password("wrong", h)

    run_check("hash_password / verify_password", _hash)

# ========================================================================
# 3. DATABASE — CRUD И МИГРАЦИИ
# ========================================================================

def test_database():
    section("3. DATABASE — CRUD, МИГРАЦИИ, A/B")
    set_section("database")

    import database
    database.init_db()

    from database import db_users, db_leads, db_stats, db_notifications, db_payments, db_crm_log

    # --- Миграции ---
    def _migrated():
        c = sqlite3.connect("fox_bot.db")
        cols_leads = [r[1] for r in c.execute("PRAGMA table_info(leads)")]
        cols_users = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        c.close()
        return ("facts" in cols_leads
                and "ab_variant" in cols_leads
                and "trial_used" in cols_users)

    run_check("Миграции: facts, ab_variant, trial_used в схеме", _migrated)

    # --- Users CRUD ---
    def _user_create():
        db_users.create_default(TEST_USER_ID, "test_selftest", "Selftest User")
        u = db_users.get(TEST_USER_ID)
        return u is not None and u["username"] == "test_selftest"

    run_check("UserDB.create_default → UserDB.get", _user_create)

    def _user_update():
        db_users.update(TEST_USER_ID, tariff="pro", manual_discount=15)
        u = db_users.get(TEST_USER_ID)
        return u["tariff"] == "pro" and u["manual_discount"] == 15

    run_check("UserDB.update (tariff, manual_discount)", _user_update)

    def _user_token():
        db_users.update(TEST_USER_ID, bot_token="111:AAAA")
        u = db_users.get(TEST_USER_ID)
        return u["bot_token"] == "111:AAAA"  # decrypt прошёл

    run_check("bot_token шифруется при записи и расшифровывается при чтении", _user_token)

    def _user_json():
        db_users.update(TEST_USER_ID, managers=[{"id": 123, "level": "senior"}])
        u = db_users.get(TEST_USER_ID)
        return u["managers"] == [{"id": 123, "level": "senior"}]

    run_check("JSON-поле managers: dict → JSON → dict", _user_json)

    # --- Leads CRUD ---
    def _lead_create():
        lead_id = db_leads.create(TEST_USER_ID, {
            "telegram_id": 999,
            "username": "lead_test",
            "name": "Lead",
            "messages": [{"role": "user", "content": "привет"}],
            "summary": "тест",
            "score": 50,
            "is_hot": True,
            "tags": ["горячий"],
            "sentiment": "positive",
            "facts": {"budget": 100000, "deadline": 7, "problem": "сайт"},
            "ab_variant": "A"
        })
        leads = db_leads.get_by_client(TEST_USER_ID)
        return len(leads) > 0 and leads[0]["score"] == 50

    run_check("LeadDB.create (с facts и ab_variant)", _lead_create)

    def _lead_update():
        leads = db_leads.get_by_client(TEST_USER_ID)
        lead_id = leads[0]["id"]
        db_leads.update(lead_id, score=75, facts={"budget": 200000})
        leads = db_leads.get_by_client(TEST_USER_ID)
        return leads[0]["score"] == 75 and "200000" in leads[0]["facts"]

    run_check("LeadDB.update (score, facts)", _lead_update)

    # --- Stats ---
    def _stats():
        db_stats.log_lead(TEST_USER_ID, is_hot=False)
        db_stats.increment_hot(TEST_USER_ID)
        today = datetime.now().strftime("%Y-%m-%d")
        c = sqlite3.connect("fox_bot.db")
        row = c.execute(
            "SELECT leads_count, hot_leads_count FROM stats WHERE client_id = ? AND date = ?",
            (TEST_USER_ID, today)
        ).fetchone()
        c.close()
        return row is not None and row[0] >= 1 and row[1] >= 1

    run_check("StatsDB.log_lead + increment_hot (UPSERT)", _stats)

    # --- Notifications ---
    def _notif():
        db_notifications.add(TEST_USER_ID, "тест-уведомление")
        unsent = db_notifications.get_unsent()
        mine = [n for n in unsent if n["recipient_id"] == TEST_USER_ID]
        if not mine:
            return False
        db_notifications.mark_sent(mine[0]["id"])
        return True

    run_check("NotificationDB.add → get_unsent → mark_sent", _notif)

    # --- Payments ---
    def _payments():
        ok1 = db_payments.add(TEST_USER_ID, "basic", 900, "charge_test_1", "prov_1")
        ok2 = db_payments.add(TEST_USER_ID, "basic", 900, "charge_test_1", "prov_1")
        return ok1 and not ok2  # второй раз — дубль, должен вернуть False

    run_check("PaymentDB.add идемпотентен (charge_id UNIQUE)", _payments)

    def _payments_exists():
        return db_payments.exists_charge("charge_test_1")

    run_check("PaymentDB.exists_charge", _payments_exists)

    def _payments_totals():
        count, stars = db_payments.get_totals()
        return count >= 1 and stars >= 900

    run_check("PaymentDB.get_totals", _payments_totals)

    # --- CRM log ---
    def _crm_log():
        db_crm_log.add(TEST_USER_ID, 1, "amocrm", "success", "сделка #42")
        db_crm_log.add(TEST_USER_ID, 2, "amocrm", "error", "401")
        logs = db_crm_log.get_by_client(TEST_USER_ID, limit=10)
        return len(logs) == 2

    run_check("CrmLogDB.add → get_by_client", _crm_log)

    def _crm_stats():
        stats = db_crm_log.get_stats(TEST_USER_ID)
        return stats["success"] == 1 and stats["error"] == 1 and stats["total"] == 2

    run_check("CrmLogDB.get_stats", _crm_stats)

    # --- A/B stats ---
    def _ab_stats():
        for variant, is_hot in [("A", 1), ("A", 0), ("B", 1), ("B", 0), ("B", 0)]:
            c = sqlite3.connect("fox_bot.db")
            c.execute(
                "INSERT INTO leads (client_id, ab_variant, is_hot, created_at) VALUES (?, ?, ?, ?)",
                (TEST_USER_ID, variant, is_hot, datetime.now().isoformat())
            )
            c.commit()
            c.close()
        since = (datetime.now() - timedelta(days=1)).isoformat()
        stats = db_leads.get_ab_stats(TEST_USER_ID, since)
        # A: 2 (1 hot, 1 not), B: 3 (1 hot, 2 not) + уже было 1 lead с variant=A
        return stats["A"]["total"] >= 2 and stats["B"]["total"] == 3

    run_check("LeadDB.get_ab_stats (A/B по вариантам)", _ab_stats)

# ========================================================================
# 4. BOT_MAIN — ХЕЛПЕРЫ
# ========================================================================

def test_bot_main():
    section("4. BOT_MAIN — ЭКСПОРТ, ОТЧЁТ, ИНСАЙТЫ, A/B")
    set_section("bot_main")

    import bot_main

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- CSV ---
    def _csv():
        stats = [{"date": "2026-10-20", "leads_count": 5, "hot_leads_count": 2}]
        data = bot_main._build_csv(stats)
        return isinstance(data, bytes) and b"2026-10-20" in data

    run_check("_build_csv", _csv)

    # --- Excel ---
    def _excel():
        stats = [
            {"date": "2026-10-20", "leads_count": 5, "hot_leads_count": 2},
            {"date": "2026-10-21", "leads_count": 3, "hot_leads_count": 1},
        ]
        data = bot_main._build_excel(stats)
        path = os.path.join(OUTPUT_DIR, "test_stats.xlsx")
        with open(path, "wb") as f:
            f.write(data)
        # Проверяем, что это валидный xlsx (ZIP-сигнатура)
        return len(data) > 1000 and data[:2] == b"PK"

    run_check("_build_excel → валидный .xlsx (сохранён в selftest_output/)", _excel)

    # --- PDF ---
    def _pdf():
        stats = [
            {"date": "2026-10-20", "leads_count": 5, "hot_leads_count": 2},
            {"date": "2026-10-21", "leads_count": 3, "hot_leads_count": 1},
        ]
        data = bot_main._build_pdf(stats, "week")
        path = os.path.join(OUTPUT_DIR, "test_stats.pdf")
        with open(path, "wb") as f:
            f.write(data)
        return len(data) > 500 and data[:4] == b"%PDF"

    run_check("_build_pdf → валидный .pdf (сохранён в selftest_output/)", _pdf)

    # --- Weekly insights ---
    def _insights():
        leads = [
            {"is_hot": 1, "sentiment": "positive", "created_at": "2026-10-20T19:00:00",
             "facts": '{"budget": 50000, "deadline": 7}'},
            {"is_hot": 1, "sentiment": "neutral", "created_at": "2026-10-21T20:00:00",
             "facts": '{"budget": 80000, "deadline": 14}'},
            {"is_hot": 0, "sentiment": "negative", "created_at": "2026-10-22T22:00:00",
             "facts": '{}'},
            {"is_hot": 0, "sentiment": "neutral", "created_at": "2026-10-23T10:00:00",
             "facts": '{}'},
            {"is_hot": 0, "sentiment": "neutral", "created_at": "2026-10-23T11:00:00",
             "facts": '{}'},
        ]
        lines = bot_main._build_weekly_insights(leads)
        # Должны быть: средний бюджет, средние сроки, тональность, пик
        return len(lines) >= 3

    run_check("_build_weekly_insights: минимум 3 инсайта", _insights)

    # --- Recommendations ---
    def _recs():
        leads = [
            {"is_hot": 0, "sentiment": "negative", "created_at": "2026-10-22T22:00:00",
             "facts": '{"budget": 30000}'},
        ] * 5
        user_data = {
            "schedules": {"enabled": True, "start_time": "09:00", "end_time": "21:00"},
            "lead_settings": {"budget_threshold": 100000, "deadline_days": 30},
        }
        recs = bot_main._build_weekly_recommendations(leads, user_data)
        return len(recs) >= 1

    run_check("_build_weekly_recommendations: выдаёт хотя бы одну", _recs)

    # --- A/B block ---
    def _ab_block():
        # Уже есть данные из test_database
        user_data = {
            "tariff": "basic",
            "bot_settings": {"ab_enabled": True, "ab_variant_a": "A", "ab_variant_b": "B"},
        }
        block = bot_main._build_ab_block(TEST_USER_ID, user_data)
        return len(block) >= 3  # заголовок + A + B + вердикт

    run_check("_build_ab_block: 3+ строки", _ab_block)

    # --- A/B на trial не должно работать ---
    def _ab_trial():
        user_data = {
            "tariff": "trial",
            "bot_settings": {"ab_enabled": True, "ab_variant_a": "A", "ab_variant_b": "B"},
        }
        block = bot_main._build_ab_block(TEST_USER_ID, user_data)
        return block == []

    run_check("_build_ab_block: пусто на trial-тарифе", _ab_trial)

    # --- Weekly report полный ---
    def _weekly():
        report = bot_main._build_weekly_report(TEST_USER_ID)
        # Лиды есть (из test_database), значит отчёт должен быть
        return report is not None and "Еженедельный отчёт" in report

    run_check("_build_weekly_report: полный отчёт генерируется", _weekly)

    # --- Сохраняем пример отчёта в файл ---
    try:
        report = bot_main._build_weekly_report(TEST_USER_ID)
        if report:
            path = os.path.join(OUTPUT_DIR, "weekly_report_example.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
    except Exception:
        pass

    # --- _leads_to_sheet_rows ---
    def _sheet_rows():
        leads = [{
            "created_at": "2026-10-20T10:00:00",
            "lead_telegram_id": 123,
            "lead_name": "Тест",
            "lead_username": "test_user",
            "summary": "краткое резюме",
            "score": 75,
            "is_hot": 1,
            "tags": '["горячий", "срочно"]',
            "sentiment": "positive",
        }]
        rows = bot_main._leads_to_sheet_rows(leads)
        return len(rows) == 1 and len(rows[0]) == 9 and rows[0][6] == "да"

    run_check("_leads_to_sheet_rows: структура", _sheet_rows)

# ========================================================================
# 5. CRM_CLIENT — ОБРАБОТКА ОШИБОК
# ========================================================================

def test_crm_client():
    section("5. CRM_CLIENT — ОБРАБОТКА ОШИБОК")
    set_section("crm_client")

    import asyncio
    import crm_client

    def _unknown():
        result = asyncio.run(crm_client.test_connection({}))
        return not result[0] and "Неизвестная CRM" in result[1]

    run_check("test_connection на пустой dict: (False, причина)", _unknown)

    def _empty_amocrm():
        result = asyncio.run(crm_client.test_connection({
            "type": "amocrm", "subdomain": "", "access_token": ""
        }))
        return not result[0] and "subdomain" in result[1].lower()

    run_check("AmoCRM без ключей → False (без краша)", _empty_amocrm)

    def _empty_bitrix():
        result = asyncio.run(crm_client.test_connection({
            "type": "bitrix24", "webhook_url": ""
        }))
        return not result[0]

    run_check("Bitrix24 без webhook → False (без краша)", _empty_bitrix)

    def _push_unknown():
        result = asyncio.run(crm_client.push_lead({"type": "unknown"}, {}))
        return not result[0]

    run_check("push_lead с неизвестной CRM → False", _push_unknown)

# ========================================================================
# 6. AGENT_RUNNER
# ========================================================================

def test_agent_runner():
    section("6. AGENT_RUNNER — ВАЛИДАЦИЯ И PID")
    set_section("agent_runner")

    import asyncio
    import agent_runner

    def _pid_self():
        return agent_runner._pid_alive(os.getpid())

    run_check("_pid_alive на текущем процессе → True", _pid_self)

    def _pid_dead():
        return not agent_runner._pid_alive(9999999)

    run_check("_pid_alive на несуществующем → False", _pid_dead)

    def _validate_empty():
        ok, msg = asyncio.run(agent_runner.validate_bot_token(""))
        return not ok

    run_check("validate_bot_token('') → False", _validate_empty)

    def _validate_bad_format():
        ok, msg = asyncio.run(agent_runner.validate_bot_token("no_colon_here"))
        return not ok and "':' " in msg or ":" in msg

    run_check("validate_bot_token(без ':') → False", _validate_bad_format)

    def _agent_alive():
        return not agent_runner.is_agent_alive(TEST_USER_ID)  # нет pid-файла

    run_check("is_agent_alive для несуществующего → False", _agent_alive)

# ========================================================================
# 7. BOT_AGENT — БЕЗ ЗАПУСКА ЧЕРЕЗ SUBPROCESS
# ========================================================================

def test_bot_agent():
    section("7. BOT_AGENT — ПРОВЕРКА ЛОГИКИ")
    set_section("bot_agent")

    # bot_agent делает sys.exit при импорте, поэтому проверяем через subprocess
    def _compiles():
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", "bot_agent.py"],
            capture_output=True, timeout=10
        )
        return result.returncode == 0

    run_check("bot_agent.py компилируется (py_compile)", _compiles)

    def _exits_without_args():
        result = subprocess.run(
            [sys.executable, "bot_agent.py"],
            capture_output=True, timeout=10,
            input=b""
        )
        # Без argv должен выйти с кодом 1 и сообщением "Использование"
        return result.returncode == 1 and b"CLIENT_ID" in result.stdout

    run_check("bot_agent.py без аргументов: exit(1) с подсказкой", _exits_without_args)

    # --- langdetect через отдельный процесс ---
    def _langdetect():
        code = (
            "from langdetect import detect, DetectorFactory; "
            "DetectorFactory.seed = 0; "
            "r = detect('I want to buy your product now'); "
            "print(r)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout=10, text=True
        )
        return result.returncode == 0 and result.stdout.strip() == "en"

    run_check("langdetect: 'I want to buy...' → 'en'", _langdetect)

# ========================================================================
# 8. ИТОГ
# ========================================================================

def print_summary():
    section("ИТОГ")
    total = len(results)
    passed = sum(1 for r in results if r[2])
    failed = total - passed

    print(f"\n  Всего тестов: {total}")
    print(f"  ✅ Прошло:   {passed}")
    print(f"  ❌ Упало:    {failed}")

    if failed > 0:
        print(f"\n  ПРОВАЛЫ:")
        current_section = None
        for sec, name, ok, err in results:
            if ok:
                continue
            if sec != current_section:
                print(f"\n  [{sec}]")
                current_section = sec
            print(f"    ❌ {name}")
            if err:
                print(f"       {err}")

    print(f"\n  Файлы для просмотра: {OUTPUT_DIR}/")
    if os.path.exists(OUTPUT_DIR):
        for f in sorted(os.listdir(OUTPUT_DIR)):
            print(f"    📄 {f}")

    print()
    return failed == 0

# ========================================================================
# MAIN
# ========================================================================

def main():
    print("=" * 70)
    print("  AI MANAGER — SELFTEST")
    print(f"  Запуск: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Тестовый user_id: {TEST_USER_ID}")
    print("=" * 70)

    # Прогреваем
    try:
        import database
        database.init_db()
    except Exception as e:
        print(f"\n⚠️ Не удалось инициализировать БД: {e}")
        print("   Проверь, что config.py, secure_config.py, database.py на месте.")
        sys.exit(1)

    try:
        test_syntax()
        test_secure_config()
        test_database()
        test_bot_main()
        test_crm_client()
        test_agent_runner()
        test_bot_agent()
    except Exception as e:
        print(f"\n⚠️ Неожиданная ошибка: {e}")
        traceback.print_exc()
        cleanup()
        sys.exit(1)

    cleanup()
    ok = print_summary()
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()