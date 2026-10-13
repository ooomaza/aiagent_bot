<div align="center">

# 🤖 AI Manager

### SaaS-платформа для продажи AI-менеджеров по продажам через Telegram

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![SQLite](https://img.shields.io/badge/SQLite-3-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://sqlite.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot_API-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-AI-6E56CF?style=for-the-badge&logo=openai&logoColor=white)](https://deepseek.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

**Продавай AI-менеджеров. Автоматизируй продажи. Зарабатывай на подписках.**

[Возможности](#-возможности) · [Архитектура](#-архитектура) · [Установка](#-установка) · [Команды](#-команды) · [FAQ](#-faq)

---

</div>

## 📸 Скриншоты

<div align="center">

| Главное меню | Управление ботом | AI-диалог |
|:---:|:---:|:---:|
| ![Главное меню](docs/screenshots/main-menu.png) | ![Управление](docs/screenshots/control-panel.png) | ![Диалог](docs/screenshots/ai-dialog.png) |

</div>

> 💡 Скриншоты добавь в `docs/screenshots/` после первого запуска.

---

## 📖 О проекте

**AI Manager** — это готовая платформа, которая позволяет продавать **AI-менеджеров по продажам** как SaaS-сервис.

**Как это работает:**

```mermaid
graph LR
    A[Клиент] -->|Покупает тариф| B[Главный бот]
    B -->|Вставляет токен| C[AI-агент запущен]
    D[Лид бизнеса клиента] -->|Пишет| C
    C -->|Квалификация| E[Горячий лид]
    E -->|Уведомление| A
    style A fill:#E26D5C,color:#fff
    style B fill:#3A6EA5,color:#fff
    style C fill:#6E56CF,color:#fff
    style E fill:#5BA678,color:#fff
```

1. Клиент покупает подписку в твоём боте
2. Создаёт своего бота у @BotFather
3. Вставляет токен — AI-агент **запускается автоматически**
4. Агент общается с лидами клиента, квалифицирует их
5. Горячие лиды приходят клиенту **прямо в Telegram**

---

## ✨ Возможности

<table>
<tr>
<td width="50%" valign="top">

### 🎯 Для клиента

- 💳 **Тарифы** — Basic/Pro/Unlimited
- 🎁 **Пробный период** — 2 дня бесплатно
- 👥 **Реферальная система** — до 40% скидки
- ⚙️ **Настройка бота** — скрипт, товары, FAQ
- 📚 **RAG** — база знаний для точных ответов
- 🎯 **Фильтрация лидов** — бюджет, сроки, правила
- 🎤 **Голосовые** — распознавание речи
- 🌍 **5 языков** — RU/EN/ES/AR/ZH
- 🤵 **Менеджеры** — команда с уровнями
- 📊 **CRM-интеграции** — AmoCRM, Bitrix24, HubSpot
- 🤖 **Мульти-бот** — управление несколькими ботами
- 📈 **Экспорт** — Excel, CSV, PDF

</td>
<td width="50%" valign="top">

### 🤖 AI-агент

- 💬 **DeepSeek AI** — свободный диалог
- 🏷️ **Авто-теги** — злой, горячий, срочно
- 📊 **Lead Scoring** — оценка 0–100
- 📱 **Сбор контактов** — телефон, email
- 🔄 **Fallback** — работает без AI
- 🕐 **Hot-reload** — настройки обновляются на лету
- 🎯 **Умная передача** — менеджеру с историей

### 👑 Для админа

- 📋 **Все клиенты** — с пагинацией
- 📊 **Общая статистика**
- 💳 **Управление тарифами**
- 🚫 **Баны и разбаны**
- 📨 **Ответы в поддержку**
- 💡 **Управление идеями**

</td>
</tr>
</table>

---

## 🏗️ Архитектура

```text
┌─────────────────────────────────────────────────────────┐
│                    ГЛАВНЫЙ БОТ                          │
│         Продажи • Управление • Активация                │
│                                                         │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │
│   │   Клиент    │  │  Тарифы     │  │  Рефералы   │   │
│   └─────────────┘  └─────────────┘  └─────────────┘   │
└───────────────────────┬─────────────────────────────────┘
                        │
                        │ subprocess.Popen
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     AI-АГЕНТ                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ DeepSeek │  │   RAG    │  │ Scoring  │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │   FAQ    │  │  Теги    │  │ Языки    │              │
│  └──────────┘  └──────────┘  └──────────┘              │
└───────────────────────┬─────────────────────────────────┘
                        │
                        │ db_notifications
                        ▼
┌─────────────────────────────────────────────────────────┐
│                   АДМИН-БОТ                             │
│          Клиенты • Тикеты • Идеи • Баны                 │
└─────────────────────────────────────────────────────────┘
```

### 📦 Стек технологий

| Слой | Технологии |
|:---|:---|
| **Язык** | ![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white) |
| **Telegram** | ![ptb](https://img.shields.io/badge/python--telegram--bot-21.7-26A5E4?logo=telegram) |
| **AI** | ![DeepSeek](https://img.shields.io/badge/DeepSeek-API-6E56CF) |
| **БД** | ![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite) |
| **Шифрование** | ![AES](https://img.shields.io/badge/AES--256--GCM-cryptography-red) |
| **HTTP** | ![aiohttp](https://img.shields.io/badge/aiohttp-async-blue) |

---

## 📁 Структура проекта

```text
ai_agent/
│
├── 📄 .env                      # Токены (НЕ коммитить!)
├── 📄 .env.encrypted            # Зашифрованные токены
├── 📄 .gitignore
├── 📄 requirements.txt
│
├── 🔐 secure_config.py          # AES-256-GCM
├── ⚙️ config.py                 # Настройки
├── 💾 database.py               # SQLite
│
├── 🤖 bot_main.py               # Главный бот
├── 🧠 bot_agent.py              # AI-агент
├── 👑 bot_admin.py              # Админ-бот
│
├── 🔑 secret.key                # Мастер-ключ (авто)
├── 🔑 secret.salt               # Соль (авто)
├── 💽 fox_bot.db                # БД (авто)
│
└── 📋 *.log                     # Логи
```

---

## 🚀 Установка

### Требования

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![pip](https://img.shields.io/badge/pip-latest-3776AB?style=flat-square&logo=pypi&logoColor=white)

### Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/your-username/ai-agent.git
cd ai-agent

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Создать .env
cp .env.example .env
# Заполнить токенами

# 4. Запустить
python bot_main.py    # Терминал 1
python bot_admin.py   # Терминал 2
```

### Пошаговая инструкция

<details>
<summary><b>📌 Шаг 1. Создать ботов у @BotFather</b></summary>

**Главный бот:**
```
/newbot
Name: AI Manager
Username: ai_manager_bot
```

**Админ-бот:**
```
/newbot
Name: AI Admin
Username: ai_admin_bot
```

Сохрани оба токена.
</details>

<details>
<summary><b>📌 Шаг 2. Получить DeepSeek API-ключ</b></summary>

1. Открыть [platform.deepseek.com](https://platform.deepseek.com/)
2. Зарегистрироваться
3. API Keys → Create new key
4. Скопировать
</details>

<details>
<summary><b>📌 Шаг 3. Заполнить .env</b></summary>

```env
MAIN_BOT_TOKEN=1234567890:ABC...
ADMIN_BOT_TOKEN=0987654321:XYZ...
DEEPSEEK_API_KEY=sk-xxxxx
```
</details>

<details>
<summary><b>📌 Шаг 4. Настроить config.py</b></summary>

```python
ADMIN_IDS = [123456789]  # Твой Telegram ID
CHANNEL_URL = ""         # Ссылка на канал (опционально)
```

Узнать ID: @userinfobot
</details>

---

## 📖 Команды

### 🎮 Для клиента

| Команда | Описание |
|:---|:---|
| `/start` | 🏠 Главное меню |
| `/tariffs` | 💳 Тарифы |
| `/help` | 📖 Справка |
| `/set_script <текст>` | 📝 Скрипт продаж |
| `/set_products <товар1, товар2>` | 📦 Товары |
| `/set_faq вопрос\|ответ; ...` | ❓ FAQ |
| `/rag_add <текст>` | 📚 Добавить в RAG |
| `/set_budget 50000` | 💰 Минимальный бюджет |
| `/set_deadline 7` | 📅 Максимальный срок |
| `/add_manager <id> junior` | 👥 Добавить менеджера |
| `/crm_setup` | 🔗 Настройка CRM |
| `/export day excel` | 📊 Экспорт статистики |
| `/support_chat` | 💬 Поддержка |
| `/suggest_idea` | 💡 Предложить идею |

### 👑 Для админа

| Команда | Описание |
|:---|:---|
| `/user <id>` | 👤 Инфо о клиенте |
| `/set_tariff <id> basic` | 💳 Сменить тариф |
| `/set_trial <id> 3` | 🎁 Выдать пробный |
| `/give_discount <id> 20` | 💰 Скидка |
| `/ban <id>` | 🚫 Забанить |
| `/unban <id>` | ✅ Разбанить |
| `/reply_support <id> <текст>` | 💬 Ответ в поддержку |
| `/reply_idea <id> <текст>` | 💡 Ответ на идею |

---

## 💰 Тарифы

<div align="center">

| 🎁 Пробный | 🥉 Базовый | 🥈 Про | 🥇 Безлимит |
|:---:|:---:|:---:|:---:|
| **0 ₽** | **1,500 ₽** | **4,500 ₽** | **12,000 ₽** |
| 2 дня | 30 дней | 30 дней | 30 дней |
| 10 лидов | 30 лидов | 100 лидов | ∞ лидов |

</div>

### 🎉 Реферальная система

```
3 друга → 20% скидки
6 друзей → 40% скидки
```

---

## 🖼️ Скриншоты интерфейса

<details>
<summary><b>Посмотреть скриншоты</b></summary>

### Главное меню
![Main Menu](docs/screenshots/main-menu.png)

### Управление ботом
![Control Panel](docs/screenshots/control-panel.png)

### AI-диалог с лидом
![AI Dialog](docs/screenshots/ai-dialog.png)

### Статистика
![Stats](docs/screenshots/stats.png)

</details>

---

## ❓ FAQ

<details>
<summary><b>Как активировать бота клиента?</b></summary>

1. Клиент пишет `/start`
2. Нажимает «🔑 Активировать бота»
3. Создаёт бота у @BotFather
4. Вставляет токен
5. Агент запускается автоматически через `subprocess.Popen`
</details>

<details>
<summary><b>Что если DeepSeek не настроен?</b></summary>

Агент работает в **fallback-режиме** — простой скрипт (3 вопроса → уведомление владельцу).
</details>

<details>
<summary><b>Где хранятся данные?</b></summary>

В SQLite (`fox_bot.db`). Токены клиентов зашифрованы **AES-256-GCM**.
</details>

<details>
<summary><b>Можно ли использовать PostgreSQL?</b></summary>

Да. Замени в `config.py`:
```python
DATABASE_URL = "postgresql+asyncpg://user:pass@host/db"
```
</details>

<details>
<summary><b>Как обновить бота?</b></summary>

```bash
git pull
pip install -r requirements.txt
# Перезапустить боты
```
</details>

<details>
<summary><b>Как добавить новую игру/функцию?</b></summary>

Через админку в боте или через `/suggest_idea` от клиентов.
</details>

---

## 🗺️ Roadmap

- [x] Продажа тарифов
- [x] AI-агент с DeepSeek
- [x] RAG-база знаний
- [x] Lead Scoring
- [x] Мультиязычность (5 языков)
- [x] CRM-интеграции
- [x] Мульти-бот (холдинг)
- [x] Геймификация
- [x] Автоматическая оплата (ЮKassa)
- [ ] Web-админка (FastAPI)
- [ ] Мобильное приложение
- [ ] White Label
- [ ] API для разработчиков

---

## 🤝 Вклад в проект

Pull requests приветствуются!

1. Форкнуть проект
2. Создать ветку: `git checkout -b feature/amazing-feature`
3. Коммит: `git commit -m 'Add amazing feature'`
4. Push: `git push origin feature/amazing-feature`
5. Открыть Pull Request

---

## 📊 Статистика репозитория

<div align="center">

![Stars](https://img.shields.io/github/stars/your-username/ai-agent?style=social)
![Forks](https://img.shields.io/github/forks/your-username/ai-agent?style=social)
![Watchers](https://img.shields.io/github/watchers/your-username/ai-agent?style=social)

![Issues](https://img.shields.io/github/issues/your-username/ai-agent)
![Pull Requests](https://img.shields.io/github/issues-pr/your-username/ai-agent)
![Last Commit](https://img.shields.io/github/last-commit/your-username/ai-agent)
![Code Size](https://img.shields.io/github/languages/code-size/your-username/ai-agent)

</div>

---

## 📄 Лицензия

Этот проект распространяется под лицензией **MIT**. Подробности в файле [LICENSE](LICENSE).

```text
MIT License

Copyright (c) 2026 Your Name

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 📞 Контакты

<div align="center">

**Есть вопросы? Свяжись со мной:**

[![Telegram](https://img.shields.io/badge/Telegram-@YourSupportBot-26A5E4?style=for-the-badge&logo=telegram)](https://t.me/YourSupportBot)
[![Email](https://img.shields.io/badge/Email-support@example.com-D14836?style=for-the-badge&logo=gmail&logoColor=white)](mailto:support@example.com)
[![GitHub](https://img.shields.io/badge/GitHub-your--username-181717?style=for-the-badge&logo=github)](https://github.com/your-username)

---

### ⭐ Если проект был полезен — поставь звезду!

**Сделано с ❤️ для бизнеса и разработчиков.**

</div>