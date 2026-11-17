# Personal AI Assistant

Персональный ИИ-ассистент с самодельным ядром агента (без агентских фреймворков), единым LLM-шлюзом, гибридной
долговременной памятью, изолированной Docker-песочницей, поддержкой MCP-серверов, наблюдаемостью и двумя общими
интерфейсами (**CLI** и **Telegram-бот**).

---

## Содержание

1. [Архитектура и сервисы](#архитектура-и-сервисы)
2. [Ключевые возможности и скиллы](#ключевые-возможности-и-скиллы)
3. [Структура проекта](#структура-проекта)
4. [Быстрый старт](#быстрый-старт)
5. [Настройка интеграций (переменные `.env`)](#настройка-интеграций-переменные-env)
6. [Использование интерфейсов (CLI и Telegram)](#использование-интерфейсов-cli-и-telegram)
7. [Расширение: `AGENTS.md`, `SKILL.md` и MCP-серверы](#расширение-agentsmd-skillmd-и-mcp-серверы)
8. [Наблюдаемость и контроль расхода](#наблюдаемость-и-контроль-расхода)
9. [Повседневное управление (запуск, остановка, бэкапы)](#повседневное-управление-запуск-остановка-бэкапы)
10. [Безопасность](#безопасность)

---

## Архитектура и сервисы

Вся система поднимается через `docker-compose.yml` и разделена на изолированные сервисы:

```text
  [ CLI (host) ] ──HTTP──┐
                         ▼
  [ Telegram (aiogram) ] ─► [ assistant-daemon (:8080) ]
                               │    ├── AgentLoop (нативный tool calling + compaction)
                               │    ├── MemoryStore (SQLite сессии + FTS5 + Embeddings)
                               │    ├── TaskQueue & ReminderLoop
                               │    └── Integrations & MCP Client
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
  [ LiteLLM Proxy ]    [ Arize Phoenix ]   [ Docker Sandbox ]
   (:4000 + Postgres)   (:6006 OTLP/UI)     (bash, pwsh 7.6, python3)
```

| Сервис      | Контейнер                 | Порт                     | Назначение                                                                                                      |
|:------------|:--------------------------|:-------------------------|:----------------------------------------------------------------------------------------------------------------|
| **daemon**  | `assistant-daemon`        | `127.0.0.1:8080`         | Единый FastAPI-бэкенд: ядро агента, очередь задач, планировщик напоминаний, Telegram-бот                        |
| **litellm** | `assistant-litellm-proxy` | `127.0.0.1:4000`         | Единый LLM-шлюз: маршрутизация, пул моделей, fallback-цепочки, бюджеты и учёт токенов                           |
| **db**      | `assistant-litellm-db`    | —                        | PostgreSQL 16 для хранения ключей, бюджетов и логов LiteLLM                                                     |
| **phoenix** | `assistant-phoenix`       | `127.0.0.1:6006`, `4317` | Arize Phoenix — трассировка вызовов агента, LLM и тулов (OpenTelemetry)                                         |
| **sandbox** | `assistant-sandbox`       | —                        | Изолированный контейнер для выполнения кода и shell (`bash`, `pwsh 7.6`, `python3`), видит только `./workspace` |

---

## Ключевые возможности и скиллы

### Ядро агента и память

* **Самодельный цикл (`AgentLoop`):** нативный `tool calling` без LangChain/LlamaIndex, параллельный вызов инструментов,
  самовосстановление истории при сбоях.
* **Управление контекстом (`ContextManager`):** подсчёт токенов и автоматический `compaction` (сжатие старой истории
  через быструю модель без разрыва цепочек `tool_calls` ↔ `tool`).
* **Память (`MemoryStore`):**
    * *Краткосрочная:* персистентные сессии в SQLite (`data/assistant.db`), общие для CLI и Telegram.
    * *Долговременная:* гибридный поиск (векторный по эмбеддингам `gemini-embedding-001` через шлюз + полнотекстовый
      SQLite `FTS5` BM25).
* **Очередь задач и уведомления (`TaskQueue`):** синхронный чат и асинхронные фоновые задачи (`/task`), общие для обоих
  интерфейсов.

### Инструменты и интеграции

* **Файловая система и стандарты (`workspace/`):**
    * иерархическое дерево каталогов (`list_directory`), чтение с нумерацией строк (`read_file`), запись (`write_file`),
      точечное редактирование (`edit_file`);
    * автоматическая подгрузка правил из `AGENTS.md` и динамическая загрузка инструкций навыков стандарта `SKILL.md` (
      `load_skill`).
* **Изолированная песочница (`DockerSandbox`):** запуск `bash` (Linux), `pwsh` (PowerShell 7.6+) и `python3` с лимитами
  RAM/CPU, а также получение информации об ОС (`get_system_info`).
* **MCP-клиент (`MCPManager`):** самодельный клиент Model Context Protocol (JSON-RPC 2.0 поверх `stdio` и Streamable
  HTTP).
* **Поиск в интернете:** мультибэкенд-поиск (`web_search` через `ddgs`) и чтение текста страниц (`fetch_webpage`).
* **GitHub (`github_*`):** поиск публичных репо, список личных/закрытых репозиториев (`/user/repos`), чтение файлов,
  коммитов, issues, PR, создание веток, коммитов и Pull Request.
* **GitLab Т-Академии (`gitlab_*`):** поиск проектов, чтение кода/коммитов/MR, создание веток/коммитов/MR, проверка
  статуса CI/CD-пайплайнов и джобов тестов (`gitlab_get_latest_pipeline`).
* **Календари и напоминания:**
    * личный календарь в SQLite (`calendar_add_event`, `calendar_list_events`, `calendar_delete_event`);
    * чтение внешнего `.ics` с поддержкой повторяющихся правил `RRULE` и таймзоны (расписание ИТМО);
    * напоминания `once` / `interval` / `daily` с проактивной доставкой в Telegram.
* **Почта Gmail (`email_*`):** чтение без сброса статуса непрочитанных (`BODY.PEEK`), поиск, сохранение вложений в
  `workspace/attachments/`, перенос писем/рассылок в Корзину (`\Trash` по RFC 6154), отправка ответов с обязательным
  подтверждением черновика.
* **PDF-анализатор (`pdf_info`, `read_pdf`, `search_pdf`):** метаданные, оглавление, поиск по страницам и постраничное
  чтение документов через `pypdf`.
* **Распознавание изображений (Vision / OCR):** анализ фото, скриншотов кода, слайдов и рукописных задач прямо в
  Telegram (`F.photo`, скриншоты-документы) и через инструмент `analyze_image`.
* **Голосовые сообщения (STT):** расшифровка голосовых и аудиосообщений из Telegram через шлюз LiteLLM (
  `gemini-3.5-transcribe`) с дальнейшим выполнением запроса.
* **Погода (`get_weather_forecast`):** текущая погода и прогноз на 1–7 дней через Open-Meteo (по умолчанию
  Санкт-Петербург, либо любой город мира).
* **Маркетплейсы (`marketplace_search`, `marketplace_inspect_product`):** параллельный поиск товаров по Wildberries (
  API + fallback), Ozon, Яндекс Маркету, DNS, Авито и Мегамаркету с фильтрацией по бюджету.
* **Трекер расходов и выписки Сбера (`expense_*`):** ручной учёт трат, импорт PDF-выписок СберБанк Онлайн с
  дедупликацией (точный разбор сумм, времени и кодов авторизации) и месячная аналитика по категориям.

---

## Структура проекта

```text
personal-assistant/
├── .env                          # Локальные секреты (НЕ коммитить!)
├── .env.example                  # Шаблон переменных окружения
├── .gitignore
├── docker-compose.yml            # Оркестрация db, litellm, phoenix, sandbox, daemon
├── litellm_config.yaml           # Маршрутизация, пул моделей, fallback и cooldown шлюза
├── mcp_servers.json              # Конфигурация подключаемых MCP-серверов
├── pyproject.toml                # Зависимости проекта (Python 3.11+)
├── docker/
│   ├── daemon.Dockerfile         # Образ backend-демона + Docker CLI
│   └── sandbox.Dockerfile        # Образ песочницы (Python 3.12 + bash + PowerShell 7.6)
├── data/                         # SQLite БД (assistant.db) и история команд CLI
├── logs/                         # Структурированные JSON-логи (assistant.jsonl)
├── workspace/                    # Рабочая папка агента и песочницы
│   ├── AGENTS.md                 # Правила работы агента с проектом
│   ├── skills/                   # Навыки стандарта SKILL.md
│   │   ├── daily-briefing/
│   │   ├── email-assistant/
│   │   ├── expense-analytics/
│   │   ├── gitlab-homework/
│   │   ├── marketplace-shopping/
│   │   ├── meeting-notes/
│   │   ├── pdf-analyzer/
│   │   └── weather-forecast/
│   ├── notes/                    # Отчёты и конспекты, созданные ассистентом
│   ├── uploads/                  # Загруженные из Telegram фото, скриншоты и PDF
│   └── attachments/              # Вложения, скачанные из Gmail
└── src/assistant/
    ├── config.py                 # Загрузка конфигурации из .env
    ├── gateway.py                # HTTP-клиент к LiteLLM (chat, embeddings, audio/transcriptions)
    ├── tools.py                  # Реестр инструментов (ToolRegistry)
    ├── context.py                # Подсчёт токенов и compaction контекста
    ├── agent.py                  # Цикл агента (AgentLoop) + очистка base64/битых хвостов
    ├── fs_tools.py               # Инструменты работы с файлами в workspace
    ├── standards.py              # Загрузчик AGENTS.md и SKILL.md
    ├── sandbox.py                # Выполнение bash, pwsh, python3 в Docker-песочнице
    ├── mcp_client.py             # MCP-клиент (stdio и Streamable HTTP)
    ├── demo_mcp_server.py        # Локальный демо MCP-сервер (JSON-RPC 2.0)
    ├── memory.py                 # Персистентные сессии и гибридная память (вектор + FTS5)
    ├── observability.py          # OpenTelemetry (Phoenix), JSONL-логи и SpendTracker
    ├── task_queue.py             # Очередь задач и уведомлений в SQLite
    ├── runtime.py                # Единая точка сборки ядра и всех инструментов
    ├── daemon.py                 # FastAPI-сервер + TaskWorker + ReminderLoop + Telegram
    ├── cli.py                    # Терминальный клиент (rich + prompt_toolkit)
    ├── telegram_bot.py           # Telegram-бот (aiogram 3, HTML, ГС, фото/документы)
    └── integrations/             # Реализации прикладных навыков
        ├── web_search.py
        ├── github_tools.py
        ├── gitlab_tools.py
        ├── calendar_tools.py
        ├── reminders.py
        ├── marketplace_tools.py
        ├── weather_tools.py
        ├── pdf_tools.py
        ├── vision_tools.py
        ├── email_tools.py
        └── expense_tools.py
```

---

## Быстрый старт

### Требования

* **Docker Desktop** (запущен на macOS / Linux / Windows)
* **Python 3.11+** (для локального CLI-клиента)
* API-ключ **Google AI Studio** (`GEMINI_API_KEY`)

### 1. Клонирование и виртуальное окружение (для CLI)

```bash
git clone https://github.com/razgonyaevm/personal-assistant.git
cd personal-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Создание `.env` и генерация ключей шлюза

Скопируйте шаблон и сгенерируйте внутренние ключи для LiteLLM и PostgreSQL:

```bash
cp .env.example .env
printf "LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\nPOSTGRES_PASSWORD=%s\n" \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
```

Откройте `.env` и заполните ваш `GEMINI_API_KEY` (а также токены интеграций, описанные ниже).

### 3. Поднятие шлюза и выпуск бюджетного виртуального ключа

Запустите базу и LiteLLM Proxy:

```bash
docker compose up -d db litellm
```

1. Откройте в браузере **`http://localhost:4000/ui`** (логин `admin`, пароль — значение `LITELLM_MASTER_KEY` из `.env`).
2. Перейдите в **Virtual Keys -> Create New Key**, выберите **All Proxy Models**, задайте лимит бюджета (например,
   `$5.00`) и скопируйте ключ `sk-...`.
3. Вставьте его в `.env`:
   ```dotenv
   LITELLM_VIRTUAL_KEY=sk-...
   ```

### 4. Запуск всей системы

```bash
docker compose up -d --build
```

*(Если настраиваете Telegram впервые: напишите боту `/start` — он выведет ваш числовой `chat_id`. Впишите его в `.env`
в `TELEGRAM_ALLOWED_CHAT_ID=...` и выполните `docker compose up -d daemon`.)*

### 5. Запуск CLI-интерфейса

```bash
python -m assistant.cli
```

---

## Настройка интеграций (переменные `.env`)

Все секреты хранятся строго локально в `.env`:

| Переменная                                                                 | Обязательна | Описание                                                                              |
|:---------------------------------------------------------------------------|:-----------:|:--------------------------------------------------------------------------------------|
| `GEMINI_API_KEY`                                                           |     Да      | Ключ из Google AI Studio                                                              |
| `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `POSTGRES_PASSWORD`              |     Да      | Служебные ключи LiteLLM и Postgres                                                    |
| `LITELLM_VIRTUAL_KEY`                                                      |     Да      | Виртуальный бюджетный ключ из LiteLLM UI (`sk-...`)                                   |
| `TELEGRAM_BOT_TOKEN`                                                       |   Для ТГ    | Токен бота от `@BotFather`                                                            |
| `TELEGRAM_ALLOWED_CHAT_ID`                                                 |   Для ТГ    | Ваш личный числовой `chat_id` (белый список доступа)                                  |
| `GITHUB_TOKEN`                                                             |    Опц.     | GitHub PAT (Fine-grained с правами Contents/Issues/PR или Classic `repo`)             |
| `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_SSL_VERIFY`                          |    Опц.     | Адрес GitLab Т-Академии, токен со скоупом `api` и `false` для корп. SSL               |
| `EXTERNAL_ICS_URL`, `CALENDAR_TIMEZONE`                                    |    Опц.     | Ссылка или путь к `.ics` (например `workspace/itmo.ics`) и таймзона (`Europe/Moscow`) |
| `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`                                      |    Опц.     | Gmail-адрес и 16-символьный пароль приложения Google (требуется 2FA)                  |
| `EMAIL_IMAP_HOST`, `EMAIL_IMAP_PORT`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT` |    Опц.     | По умолчанию `imap.gmail.com:993` и `smtp.gmail.com:465`                              |

---

## Использование интерфейсов (CLI и Telegram)

Оба интерфейса подключены к одному `daemon` и разделяют **единую сессию `main`**, **долговременную память** и **очередь
задач**. Диалог, начатый в Telegram, можно в любой момент продолжить в CLI (и наоборот).

### Команды в CLI и Telegram

В CLI при вводе `/` появляется интерактивное выпадающее меню с подсказками (навигация стрелками `↑`/`↓` и выбор по
`Enter`, история прошлых сообщений по стрелке `↑`).

| Команда          | Описание                                                                   |
|:-----------------|:---------------------------------------------------------------------------|
| `/task <текст>`  | Поставить фоновую задачу в общую очередь (результат придёт уведомлением)   |
| `/tasks`         | Показать таблицу последних задач в очереди                                 |
| `/show <id>`     | Показать результат задачи по ID *(только в CLI)*                           |
| `/history`       | Показать последние сообщения и сжатое резюме текущей сессии                |
| `/stats`         | Сводка расхода запросов, токенов и USD по моделям                          |
| `/compact`       | Принудительно сжать контекст текущей сессии                                |
| `/reset`         | Очистить историю текущей сессии (долговременная память фактов сохраняется) |
| `/session <имя>` | Переключить или создать отдельную сессию (по умолчанию `main`)             |
| `/id`            | Показать ваш `chat_id` *(только в Telegram)*                               |
| `/help`          | Список команд                                                              |

### Примеры запросов

* **Учёба и расписание:** *«Сделай брифинг на завтра: пары в ИТМО, погода в Питере и активные напоминания»*
* **Напоминания:** *«Поставь ежедневное напоминание в 09:30 проверить задачи, и разовое через 15 минут выключить
  чайник»*
* **GitHub & GitLab:** *«Проверь статус последнего CI/CD пайплайна и открытые MR в проекте practice-06-city в GitLab»*,
  *«Какие коммиты были последними в моём репо xv6-riscv на GitHub?»*
* **Почта:** *«Покажи непрочитанные письма в Gmail, раздели на важные и рассылки, и удали рекламные рассылки в корзину»*
* **Фото / ГС / PDF (в Telegram):**
    * отправьте фото уравнения с подписью *«Реши уравнение с фото и проверь корни в Python»*;
    * отправьте PDF с подписью *«Сколько здесь страниц?»* или *«Найди раздел про дедлайны»*;
    * запишите голосовое сообщение с любой задачей.
* **Расходы и Сбер:** перешлите PDF-выписку Сбера с подписью *«Импортируй расходы за месяц и покажи чистую аналитику без
  переводов»* или напишите *«Запиши: кофе 250, обед 450»*.
* **Маркетплейсы:** *«Найди беспроводные наушники Sony до 35000 ₽ на WB, Ozon и Яндекс Маркете и сравни»*.

---

## Расширение: `AGENTS.md`, `SKILL.md` и MCP-серверы

### 1. Правила проекта (`workspace/AGENTS.md`)

Все инструкции, записанные в `workspace/AGENTS.md` (и вложенные `AGENTS.md`), автоматически подгружаются в системный
промпт агента.

### 2. Добавление нового навыка (`SKILL.md`)

Создайте папку `workspace/skills/<имя-навыка>/SKILL.md` с YAML-шапкой:

```markdown
---
name: my-custom-skill
description: Краткое описание, когда агент должен применять этот навык.
---

# Пошаговая инструкция для агента

...
```

Навык автоматически появится в каталоге агента после перезапуска `daemon`.

### 3. Подключение внешнего MCP-сервера (`mcp_servers.json`)

Добавьте сервер в `mcp_servers.json` (поддерживаются `stdio` и `http`):

```json
{
  "mcpServers": {
    "demo": {
      "transport": "stdio",
      "command": "python",
      "args": [
        "-m",
        "assistant.demo_mcp_server"
      ],
      "enabled": true
    },
    "remote-example": {
      "transport": "http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${SOME_API_TOKEN}"
      },
      "enabled": false
    }
  }
}
```

---

## Наблюдаемость и контроль расхода

1. **LiteLLM Dashboard (`http://localhost:4000/ui`):** виртуальные ключи, бюджеты, срабатывания fallback-цепочек,
   детальные логи и стоимость каждого запроса.
2. **Arize Phoenix (`http://localhost:6006`):** дерево OpenTelemetry-спанов (
   `agent.turn -> llm.chat_completion -> tool.*`), входные/выходные данные каждого шага и задержки.
3. **JSON-логи:** все события пишутся в `logs/assistant.jsonl` с привязкой к `trace_id` и `span_id`.
4. **Локальная сводка:** команда `/stats` в CLI и Telegram показывает суммарные токены и точный расход по заголовку
   `x-litellm-response-cost`.

---

## Повседневное управление (запуск, остановка, бэкапы)

* **Остановить контейнеры (с сохранением всех данных и базы):**
  ```bash
  docker compose stop
  ```
* **Запустить всё снова:**
  ```bash
  docker compose up -d
  ```
* **Пересобрать `daemon` после правки Python-кода в `src/`:**
  ```bash
  docker compose up -d --build daemon
  ```
* **Посмотреть логи бэкенда в реальном времени:**
  ```bash
  docker compose logs -f daemon
  ```
* **Важно:** не используйте `docker compose down -v`, если не хотите удалить volume PostgreSQL с виртуальными ключами и
  логами LiteLLM.

---

## Безопасность

* **Изоляция секретов и личных файлов:** `.env`, `data/`, `logs/`, `workspace/*.pdf`, `workspace/*.ics`,
  `workspace/uploads/`, `workspace/attachments/` исключены в `.gitignore` и не попадают в Git.
* **Изоляция исполнения кода:** все `run_shell` и `run_python` выполняются исключительно внутри контейнера
  `assistant-sandbox` (`512 MB` RAM, `1 CPU`, `no-new-privileges`) с доступом только к `./workspace`.
* **Авторизация Telegram:** бот отвечает только пользователю с `TELEGRAM_ALLOWED_CHAT_ID`.
* **Защита от Prompt Injection:** входящие письма из Gmail оборачиваются как неисполняемые внешние данные, а отправка
  email (`email_send`) заблокирована без явного подтверждения черновика пользователем (`confirmed=True`).