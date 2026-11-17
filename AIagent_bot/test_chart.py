import sqlite3
from datetime import datetime, timedelta
from bot_main import _build_weekly_chart
import database

# 1. Инициализируем БД
database.init_db()

# 2. Добавим фейковые данные для теста
c = sqlite3.connect("fox_bot.db")
today = datetime.now()
for i in range(7):
    date_str = (today - timedelta(days=6-i)).strftime("%Y-%m-%d")
    c.execute("INSERT OR REPLACE INTO stats (client_id, date, leads_count, hot_leads_count) VALUES (?, ?, ?, ?)", 
              (999888, date_str, 10 + i*2, 2 + i))
c.commit()
c.close()

# 3. Генерируем график
buf = _build_weekly_chart(999888)
if buf:
    with open("test_chart.png", "wb") as f:
        f.write(buf.read())
    print("✅ График сохранен в test_chart.png")
else:
    print("❌ Нет данных")