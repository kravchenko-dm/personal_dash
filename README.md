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
| Мониторинг | Агент на каждом VPS (python3 + systemd, без Docker и pip): нагрузка, память, диски, systemd-юниты, контейнеры, процессы, версии кода, локальные health-URL. Внешние HTTP-проверки с дашборда. Инциденты с алертами в пуш и Telegram, перезапуск и логи сервисов из приложения |
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
                    # projects, monitoring (agent reports, HTTP checks), alerts (incidents),
                    # telegram_bot, calendar (Google), icloud (CalDAV), agenda, yougile, scheduler
  services/llm/     # pluggable LLM backends: anthropic_client, openai_compat (kimi, deepseek, ...)
  integrations/     # провайдеры балансов (base, registry, providers/*)
frontend/src
  App.tsx, pages/*  # Home, Tasks, Balances, Calendar, YouGile, Summaries, Settings
  sw.ts             # service worker: precache + push + клик по уведомлению
  lib/api.ts        # типизированный клиент API
agent/dash_agent.py  # агент для VPS (stdlib only), раздаётся бэкендом по /api/agent/script
agent/install.sh.tmpl
deploy/bootstrap.sh  # первичная настройка VPS
deploy/deploy.sh     # обновление на сервере (вызывается GitHub Actions)
deploy/Caddyfile
.github/workflows/deploy.yml
docker-compose.yml
```

## Деплой на VPS

Нужен домен, указывающий на VPS (A-запись). Telegram webhook и Web Push требуют HTTPS.

### 1. Первичная настройка сервера (один раз, от root)

```bash
curl -fsSL https://raw.githubusercontent.com/kravchenko-dm/personal_dash/main/deploy/bootstrap.sh | bash -s -- dash.example.com
```

Скрипт `deploy/bootstrap.sh` ставит Docker, ufw (22/80/443), fail2ban и автообновления, создаёт
пользователя `dash` (группа docker, без sudo) и копирует ему ваши SSH-ключи, запрещает вход по паролю,
добавляет swap, клонирует репозиторий в `/opt/personal_dash`, создаёт `.env` со сгенерированными
секретами и VAPID-ключами.

### 2. Заполнить `.env`

`nano /opt/personal_dash/.env`:

1. `APP_DOMAIN` (если не передали скрипту), `DASH_PASSWORD`.
2. `TELEGRAM_BOT_TOKEN` от @BotFather. В @BotFather выполните `/setprivacy` → Disable, иначе бот не видит сообщения в группах. `TELEGRAM_OWNER_ID` — ваш числовой id (бот подскажет в ответ на `/start`).
3. Нейросеть для саммари: по умолчанию Claude, нужен `ANTHROPIC_API_KEY`. Другие провайдеры — см. раздел «Выбор нейросети».
4. Google: в Google Cloud Console создайте OAuth Client (Web), redirect URI `https://<домен>/api/calendar/oauth/callback`, включите Calendar API.
5. `YOUGILE_API_KEY` — в YouGile: Компания → Настройки → API.
6. iCloud (семейный календарь Apple): `ICLOUD_USERNAME` — ваш Apple ID, `ICLOUD_APP_PASSWORD` — пароль приложения
   (appleid.apple.com → Вход и безопасность → Пароли приложений). Обычный пароль Apple ID не подойдёт.

Всё, кроме домена и пароля, можно заполнить позже: модули без ключей просто неактивны.

### 3. Запуск

```bash
sudo -u dash docker compose -f /opt/personal_dash/docker-compose.yml up -d --build
docker compose -f /opt/personal_dash/docker-compose.yml logs -f backend   # ждём "webhook set to https://..."
```

Откройте `https://<домен>`, введите пароль. На телефоне: «Поделиться → На экран Домой» (iOS) или
«Установить приложение» (Android), затем в разделе «Ещё» включите уведомления.

### 4. Автодеплой из GitHub (рекомендуется)

Каждый push в `main` прогоняет тесты и сборку, затем по SSH выполняет на сервере `deploy/deploy.sh`
(`git reset --hard origin/main` + `docker compose up -d --build`). Настройка:

```bash
# на своём компьютере: отдельный ключ только для деплоя
ssh-keygen -t ed25519 -f ~/.ssh/dash_deploy -C deploy -N ""
ssh-copy-id -i ~/.ssh/dash_deploy.pub dash@<ip-сервера>
```

В GitHub → репозиторий → Settings → Secrets and variables → Actions добавьте:

| Секрет | Значение |
|---|---|
| `DEPLOY_HOST` | IP или домен сервера |
| `DEPLOY_USER` | `dash` |
| `DEPLOY_SSH_KEY` | содержимое `~/.ssh/dash_deploy` (приватный ключ) |
| `DEPLOY_PORT` | порт SSH, если не 22 (необязательно) |

Проверить: Actions → Deploy → Run workflow. Откатиться: `git revert` нужного коммита и push.

### Связь Mac ↔ сервер (шпаргалка)

Один раз на Mac:

```bash
# ключ для входа на сервер (если ~/.ssh/id_ed25519 ещё нет)
ssh-keygen -t ed25519 -C "mac" -N "" -f ~/.ssh/id_ed25519

# положить ключ на сервер (пароль root спросит один раз, его выдал хостер)
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<ip-сервера>

# алиас, чтобы не помнить IP
cat >> ~/.ssh/config <<'CFG'
Host dash
    HostName <ip-сервера>
    User dash
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
Host dash-root
    HostName <ip-сервера>
    User root
    IdentityFile ~/.ssh/id_ed25519
CFG
```

Затем первичная настройка (bootstrap скопирует ключ root пользователю `dash`):

```bash
ssh dash-root 'curl -fsSL https://raw.githubusercontent.com/kravchenko-dm/personal_dash/main/deploy/bootstrap.sh | bash -s -- dash.example.com'
```

Повседневные команды:

```bash
ssh dash                                              # зайти на сервер
ssh dash 'nano /opt/personal_dash/.env'               # или: ssh dash, потом nano
ssh dash '/opt/personal_dash/deploy/deploy.sh'        # обновить до последнего main
ssh dash 'cd /opt/personal_dash && docker compose ps'
ssh dash 'cd /opt/personal_dash && docker compose logs -f --tail=100 backend'
ssh dash 'cd /opt/personal_dash && docker compose up -d backend'   # применить изменения .env
scp dash:/opt/personal_dash/.env ~/Desktop/dash.env  # забрать копию .env

`docker compose restart` НЕ перечитывает `.env`: после правки переменных нужен `docker compose up -d`,
он пересоздаст только те контейнеры, у которых изменилось окружение.
```

Claude Code прямо на сервере (как на других VPS): `ssh dash`, затем `npm install -g @anthropic-ai/claude-code`
и `claude` в `/opt/personal_dash`, вход по ссылке из терминала. Пользователь `dash` состоит в группе
docker, поэтому Claude на сервере сможет перезапускать контейнеры и читать логи.

### Обновление вручную

```bash
sudo -u dash /opt/personal_dash/deploy/deploy.sh
```

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


## Мониторинг серверов

### Агент

Один файл на стандартной библиотеке Python, работает на любом Linux с python3 и systemd.
На странице проекта → «Мониторинг» → «+ Сервер»: дашборд выпустит токен и команду установки:

```bash
curl -fsSL "https://<домен>/api/agent/install?token=dash_..." | sudo bash
```

Скрипт кладёт агента в `/opt/dash-agent`, конфиг в `/etc/dash-agent/config.json` и запускает
сервис `dash-agent`. Токен показывается один раз, на сервере дашборда хранится только его хеш.
Агент ходит к дашборду сам (исходящий HTTPS), входящих портов не нужно, работает от root,
потому что перезапускает юниты и читает журналы.

Опишите сервисы в `config.json`, агент перечитывает его при каждом отчёте:

```json
{
  "url": "https://dash.example.com",
  "token": "dash_...",
  "interval": 20,
  "disks": ["/", "/var/lib/postgresql"],
  "discover_docker": true,
  "discover_failed_units": true,
  "services": [
    {"name": "api", "kind": "systemd", "unit": "myapi.service", "path": "/opt/myapi", "url": "http://127.0.0.1:8000/health"},
    {"name": "web", "kind": "docker", "container": "web", "version_file": "/opt/web/VERSION"},
    {"name": "worker", "kind": "process", "match": "celery worker"}
  ]
}
```

| Поле | Смысл |
|---|---|
| `kind` | `systemd` (поле `unit`), `docker` (поле `container`), `process` (подстрока командной строки `match`) |
| `path` | каталог кода: если это git-репозиторий, передаётся коммит и ветка; если нет, время последнего изменения файлов как метка деплоя |
| `version_file` | файл с версией для проектов без git |
| `url` | локальный health-URL, проверяется с самого сервера |
| `discover_docker` | показывать все контейнеры, даже не описанные в конфиге |
| `discover_failed_units` | сообщать о любых systemd-юнитах в состоянии failed |
| `max_age_hours` | для задач по расписанию: инцидент, если успешного запуска не было дольше этого срока |
| `scheduled` | принудительно считать сервис задачей по расписанию (обычно определяется само по `Type=oneshot` или таймеру) |
| `auto_update` | агент сам обновляется до версии, которую раздаёт дашборд (по умолчанию включено) |

Задачи по расписанию (oneshot-сервисы и сервисы с таймерами) между запусками показываются как
«по расписанию» с временем последнего и следующего запуска, а не как упавшие. Инцидент открывается,
если последний запуск завершился ошибкой, таймер выключен или задача давно не запускалась
(`max_age_hours`). Кнопка ▶ запускает задачу вручную.

Что присылает агент каждые 20 секунд: load average, память, swap, диски, аптайм, статус каждого
сервиса (состояние, число рестартов, время запуска, версия, ответ health-URL), список failed-юнитов.

Команды из дашборда (кнопки на карточке сервера): ping, перезапуск, стоп/старт, последние строки
журнала (`journalctl -u` или `docker logs`). Произвольные команды агент не выполняет; скрипты можно
класть в `/etc/dash-agent/scripts/` и вызывать по имени, это задел под бэкапы.

Проверить, что агент видит на сервере, без отправки: `python3 /opt/dash-agent/dash_agent.py report`.

### Внешние проверки и инциденты

«+ URL» на вкладке «Мониторинг»: дашборд сам запрашивает адрес раз в минуту (интервал настраивается),
сравнивает код ответа и при желании ищет текст в теле. Хранится история и аптайм за 24 часа.

Инциденты открываются и закрываются автоматически, алерт уходит пушем и в Telegram при открытии и при восстановлении:

| Инцидент | Условие |
|---|---|
| сайт не отвечает | две неудачные внешние проверки подряд |
| агент молчит | нет отчётов дольше порога (по умолчанию 5 минут), обычно означает недоступный сервер |
| сервис упал | юнит failed/inactive, контейнер exited/unhealthy/restarting, локальный health-URL не отвечает |
| юнит failed | любой systemd-юнит на хосте в состоянии failed |
| диск заполнен | любой из дисков выше порога (по умолчанию 85 %) |
| цикл перезапусков | счётчик рестартов сервиса вырос больше чем на 2 между отчётами |

Открытые инциденты видны на главной, в списке проектов (цвет точки) и в шапке проекта.
Образцы метрик и проверок хранятся 30 дней.

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

## Миграции схемы (Alembic)

Схема управляется Alembic, при старте backend сам выполняет `alembic upgrade head`. База, созданная
до появления миграций простым `create_all`, при первом запуске помечается ревизией `0001` и дальше
обновляется как обычно.

Изменили модели в `backend/app/models.py`:

```bash
cd backend
DATABASE_URL=sqlite+aiosqlite:////tmp/mig.db alembic upgrade head          # свежая база на текущей схеме
DATABASE_URL=sqlite+aiosqlite:////tmp/mig.db alembic revision --autogenerate -m "add foo"
# проверить файл в alembic/versions/, затем:
pytest tests/test_migrations.py                                           # миграции == модели
```

Тест `test_migrations_match_models` падает, если миграции и модели разошлись.

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
| Внешние HTTP-проверки | каждые 30 с (каждый монитор по своему интервалу) |
| Проверка молчащих агентов | каждую минуту |
| Очистка старых метрик | 04:30 |
| Вечерний дайджест (Claude → Telegram + push) | 21:00 по `TZ` |
| Утренний бриф (задачи + календарь) | 08:00 по `TZ` |
