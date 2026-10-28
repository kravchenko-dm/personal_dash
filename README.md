# Personal Dash

Личный дашборд в виде PWA (ставится на iPhone и Android) с задачами, балансами сервисов,
push-уведомлениями, Telegram-ботом, саммари от Claude, Google Calendar и YouGile.

## Что умеет (MVP)

| Модуль | Что есть |
|---|---|
| Задачи | CRUD, приоритеты, сроки, напоминания (push + Telegram) за 15 минут до срока |
| Балансы | Источники: ручной, универсальный HTTP JSON (URL + заголовки + JSONPath), OpenRouter, DeepSeek. Обновление каждые 30 минут, история, пуш при падении ниже порога |
| Push | Web Push (VAPID). На iOS работает после «На экран Домой» (iOS 16.4+) |
| Telegram | Бот собирает сообщения из чатов, куда его добавили. Команды `/summary`, `/digest`, `/tasks`, `/add`, `/done`. Предложенные задачи добавляются одной кнопкой |
| Саммари | Нейросеть делает выжимку по чатам + предлагает задачи. По умолчанию Claude, одной настройкой переключается на Kimi, DeepSeek, OpenRouter, OpenAI, Qwen или локальную Ollama. Вечерний дайджест в 21:00, утренний бриф в 08:00 |
| Календарь | Google (OAuth) и iCloud через CalDAV, включая семейный календарь Apple. Выбор календарей, единая агенда на 1–30 дней |
| YouGile | Доска (kanban), список, дедлайны, по людям. Отметка «готово», создание задач |
| Проекты | Карточка на каждый сервис: стадия, ссылки, строка «что происходит сейчас». Доска фич (идеи → запланировано → в работе → тестирование → выпущено), заметки в markdown, привязка Telegram-чатов. Саммари по чатам проекта получает описание и доску и предлагает фичи, которые добавляются на доску одной кнопкой в Telegram или в UI |
| Напоминания Apple | Снимок открытых напоминаний приходит с iPhone по расписанию (Команда/Shortcuts), показывается в задачах и на главной, переносится в задачи одной кнопкой, попадает в утренний бриф и саммари |

## Стек

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async) + PostgreSQL, APScheduler, aiogram 3, pywebpush, anthropic SDK, google-api-python-client.
- **Frontend:** React 18 + Vite + TypeScript, vite-plugin-pwa (service worker с push-обработчиком).
- **Инфра:** docker compose: Postgres, backend, frontend (nginx), Caddy (автоматический HTTPS).

## Структура

```
backend/app
  main.py           # FastAPI, lifespan (БД, Telegram webhook, планировщик)
  config.py         # настройки из .env
  models.py         # таблицы
  routers/          # /api/* эндпоинты
  services/         # tasks, balances, push, summaries, summarizer (prompt + schema), apple_reminders,
                    # telegram_bot, calendar (Google), icloud (CalDAV), agenda, yougile, scheduler
  services/llm/     # pluggable LLM backends: anthropic_client, openai_compat (kimi, deepseek, ...)
  integrations/     # провайдеры балансов (base, registry, providers/*)
frontend/src
  App.tsx, pages/*  # Home, Tasks, Balances, Calendar, YouGile, Summaries, Settings
  sw.ts             # service worker: precache + push + клик по уведомлению
  lib/api.ts        # типизированный клиент API
deploy/Caddyfile
docker-compose.yml
```

## Деплой на VPS

Нужен домен, указывающий на VPS (A-запись). Telegram webhook и Web Push требуют HTTPS.

```bash
# на сервере
apt install -y docker.io docker-compose-v2 git
git clone https://github.com/kravchenko-dm/personal_dash.git && cd personal_dash
cp .env.example .env
```

Заполните `.env`:

1. `APP_DOMAIN`, `APP_SECRET` (длинная случайная строка), `DASH_PASSWORD`.
2. VAPID-ключи: `docker compose run --rm backend python -m app.tools.vapid` и вставьте вывод.
3. `TELEGRAM_BOT_TOKEN` от @BotFather. В @BotFather выполните `/setprivacy` → Disable, иначе бот не видит сообщения в группах. `TELEGRAM_OWNER_ID` — ваш числовой id (бот подскажет в ответ на `/start`).
4. Нейросеть для саммари: по умолчанию Claude, нужен `ANTHROPIC_API_KEY`. Другие провайдеры — см. раздел «Выбор нейросети».
5. Google: в Google Cloud Console создайте OAuth Client (Web), redirect URI `https://<домен>/api/calendar/oauth/callback`, включите Calendar API.
6. `YOUGILE_API_KEY` — в YouGile: Компания → Настройки → API.
7. iCloud (семейный календарь Apple): `ICLOUD_USERNAME` — ваш Apple ID, `ICLOUD_APP_PASSWORD` — пароль приложения
   (appleid.apple.com → Вход и безопасность → Пароли приложений). Обычный пароль Apple ID не подойдёт.
   После запуска в разделе «Календарь → Календари» отметьте «Семья»; по умолчанию показываются все календари iCloud.

Запуск:

```bash
docker compose up -d --build
docker compose logs -f backend   # должно быть "webhook set to https://..."
```

Откройте `https://<домен>`, введите пароль. На телефоне: «Поделиться → На экран Домой» (iOS) или
«Установить приложение» (Android), затем в разделе «Ещё» включите уведомления.

## Локальная разработка

```bash
docker compose -f docker-compose.dev.yml up -d          # только Postgres на :5432

cd backend && python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example .env   # APP_DOMAIN=localhost, DATABASE_URL=...@localhost:5432/dash
uvicorn app.main:app --reload                            # http://localhost:8000/docs

cd ../frontend && npm install && npm run dev             # http://localhost:5173 (прокси /api → 8000)
```

Тесты и линтер backend:

```bash
cd backend && pytest && ruff check app
```

Telegram webhook локально не работает без публичного URL (нужен туннель вроде `cloudflared`),
остальное работает.

## Проекты и доски фич

Раздел «Проекты» — основа для мониторинга и бэкапов, которые появятся следующими этапами.

- **Проект:** название, описание, стадия (идея / разработка / бета / в проде / на паузе), цвет,
  ссылки на репозиторий, сайт и доску YouGile, архив.
- **«Сейчас»:** короткая строка о текущем состоянии с отметкой времени, видна на главной.
- **Доска фич:** пять колонок, карточка с приоритетом, ориентиром по сроку, описанием и ссылкой.
  На телефоне удобнее режим «Список», перенос между колонками кнопками ‹ ›.
- **Заметки:** markdown, закрепление сверху. Для окружений, адресов, контактов и чеклистов.
  Секреты в заметках не храните, только где они лежат.
- **Чаты:** Telegram-чаты привязываются к проекту на странице проекта или на странице «Саммари».
  Саммари по проекту (`POST /api/summaries?project_id=`) собирает сообщения только из его чатов,
  а в промпт добавляются описание проекта и открытые фичи. Предложения принимаются как задачи
  или как карточки на доску: кнопка «📋 на доску» в Telegram и в UI. Команда `/summary` в чате,
  привязанном к проекту, автоматически работает в контексте проекта.

API: `/api/projects`, `/api/projects/{id}` (детали одним запросом), `/features`, `/features/{id}/move`
(`{"status", "index"}`), `/notes`, `/chats/{chat_row_id}`.

Схема БД создаётся через `create_all` при старте. Это нормально до первого деплоя; после него для
изменений схемы добавим Alembic.

## Напоминания Apple

Apple не отдаёт «Напоминания» по сети: после обновления формата в iOS 13 они исчезли из iCloud
CalDAV и доступны только приложениям на устройствах Apple. Поэтому их присылает сам iPhone
через «Команды» (Shortcuts). Настройка занимает несколько минут:

1. Войдите в дашборд в Safari на iPhone, откройте «Ещё» и скопируйте токен (кнопка «Токен для Команд»).
2. Откройте «Команды» → «+» и соберите команду:
   - **Найти напоминания**, фильтр «Не завершено» (можно ограничить списком, например «Семья»).
   - **Повторять с каждым** по результату. Внутри: **Словарь** с ключами
     `title` = Название, `list` = Список, `due` = Дата выполнения, `priority` = Приоритет,
     `notes` = Заметки, `completed` = Завершено, `url` = URL (значения берите из переменной «Элемент повтора»).
   - После цикла: **Получить содержимое URL**, адрес `https://<домен>/api/apple/reminders/sync?token=<токен>`,
     метод POST, тело запроса JSON, поле `items` = «Результаты повтора».
3. Запустите вручную, проверьте вкладку «Напоминания» на странице «Задачи».
4. «Автоматизация» → «Время суток» (или несколько раз в день) → выберите эту команду, выключите
   «Спрашивать перед запуском». С iOS 17 такие автоматизации работают без подтверждения.

Каждый запуск присылает полный снимок открытых напоминаний: что исчезло на iPhone, исчезает
и в дашборде. Даты принимаются в ISO 8601 и в формате «28.10.2026 18:00».

Для старых списков, которые никогда не «обновлялись», бэкенд дополнительно раз в час читает
задачи (VTODO) по CalDAV, если задан `ICLOUD_USERNAME`. На большинстве аккаунтов таких списков нет.

## Выбор нейросети для саммари

Саммари и предложения задач не привязаны к одному вендору. В `.env`:

```
LLM_PROVIDER=kimi          # anthropic | kimi | deepseek | openrouter | openai | qwen | ollama | custom
LLM_API_KEY=sk-...         # ключ выбранного провайдера
LLM_MODEL=                 # пусто = модель по умолчанию (для kimi это kimi-k2-0905-preview)
LLM_BASE_URL=              # пусто = адрес по умолчанию; для ollama/custom укажите свой
```

| Провайдер | Адрес по умолчанию | Модель по умолчанию |
|---|---|---|
| anthropic | SDK Anthropic | `CLAUDE_MODEL` (claude-opus-5-5) |
| kimi | https://api.moonshot.ai/v1 | kimi-k2-0905-preview |
| deepseek | https://api.deepseek.com/v1 | deepseek-chat |
| openrouter | https://openrouter.ai/api/v1 | moonshotai/kimi-k2 |
| openai | https://api.openai.com/v1 | gpt-5 |
| qwen | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| ollama | http://ollama:11434/v1 | qwen2.5:14b (ключ не нужен) |
| custom | `LLM_BASE_URL` | `LLM_MODEL` |

Названия моделей у вендоров меняются, при ошибке 404 задайте актуальное через `LLM_MODEL`.
Все провайдеры кроме Anthropic работают через OpenAI-совместимый Chat Completions API: схема ответа
вкладывается в промпт, включается JSON-режим (с автоматическим откатом, если провайдер его не
поддерживает), ответ валидируется по схеме. Через OpenRouter доступны сотни моделей по одному ключу.

Код: `backend/app/services/llm/`. Новый вендор с другим протоколом — класс с методом
`complete_json(system, user, schema)` и ветка в `get_llm()`.

## Добавить свой источник баланса

Создайте файл в `backend/app/integrations/providers/`, унаследуйте `BalanceProvider`,
заполните `key`, `title`, `fields` и реализуйте `fetch(config) -> BalanceResult`, затем добавьте
класс в `registry.py`. Для большинства API достаточно универсального провайдера «HTTP JSON» из UI.

## Планировщик (backend/app/services/scheduler.py)

| Задача | Расписание |
|---|---|
| Обновление балансов | каждые 30 мин |
| Напоминания по задачам | каждые 5 мин |
| Напоминания Apple по CalDAV (старые списки) | каждый час |
| Вечерний дайджест (Claude → Telegram + push) | 21:00 по `TZ` |
| Утренний бриф (задачи + календарь) | 08:00 по `TZ` |
