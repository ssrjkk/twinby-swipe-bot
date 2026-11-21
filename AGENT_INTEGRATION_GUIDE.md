# 🤖 AGENT INTEGRATION GUIDE — `<3` v3.9.0 → `ssrjkk/twinby-swipe-bot`

> **Для кого этот документ:** для AI coding-агента (Cursor, Claude Code, OpenCode, Codex, Zed и др.), которому пользователь передал этот ZIP-архив с задачей **внедрить `<3` v3.9.0 в репозиторий `https://github.com/ssrjkk/twinby-swipe-bot`**, проверить работоспособность на локальной машине и довести до зелёного состояния в Git/CI.
>
> **Главный принцип:** действуй строго по шагам ниже, **не выдумывай** обходных костылей, **не переписывай** архитектуру без причины и **проверяй каждый шаг реальным запуском команд**.

---

## 1. Твоя миссия и критерии успеха

1. Распаковать / взять содержимое папки `heart3/` из архива.
2. Безопасно синхронизировать его в локальный клон `https://github.com/ssrjkk/twinby-swipe-bot` с помощью **`scripts/sync_to_repo.py`** (рекурсивно обновляет все файлы, удаляет устаревшие файлы даже во вложенных подкаталогах, но **гарантированно сохраняет** `.git/`, `.env`, `.env.bak`, `data/`, `.venv/`).
3. Установить пакет и прогнать **всю матрицу верификации** (компиляция, контрактные тесты, `pytest`, `unittest`, проверка CLI `heart3` и non-editable установки `pip install .`).
4. Провести аудит `git status` и `git diff` (проверить, что секреты и БД не попали в индекс).
5. Сделать коммит и пуш (если у тебя есть права и пользователь подтвердил пуш) и вернуть пользователю короткий структурированный отчёт по шаблону из раздела 7.

---

## 2. Жёсткие инварианты (ЗАПРЕЩЕНО НАРУШАТЬ)

1. **Оригинальный визуальный стиль (`index.html`, `web/static/index.html`, `web/static/dashboard.html`):**
   - Светлая тема `#fffbfc`, розовый акцент `#e11d48`, шрифты `Instrument Serif` + `Plus Jakarta Sans` + `JetBrains Mono`, плавающие розовые пузыри (`.blobs`, `.blob`, `.orb`). **Не менять палитру на тёмную и не убирать пузыри.**
   - Корневой `index.html` (для GitHub Pages) и `web/static/index.html` (для FastAPI `/`) **обязаны быть байт-в-байт идентичны**.
2. **Безопасность и защита секретов:**
   - В `git status` **никогда** не должно быть `.env`, `.env.bak`, `data/`, `*.db`, `*.har`, `.venv/`.
   - В `web/static/dashboard.html` все данные из API экранируются через `escapeHtml()`.
   - В `bot/telegram.py` и `core/chat_worker.py` все пользовательские данные при `parse_mode="HTML"` экранируются через `html.escape()`.
   - При заданном `WEB_API_TOKEN` все `/api/v1/*` (кроме `/api/v1/health`), `/api/v1/worker/last-screenshot` (через `X-API-Token` или `?token=`) и `/ws/live?token=...` обязаны возвращать `401` / `WS 1008` без валидного токена.
3. **Совместимость Python 3.10–3.12 и graceful fallbacks:**
   - Не использовать `StrEnum` (появился в 3.11) и пустой `except:`.
   - Приложение и CLI `heart3` обязаны запускаться при базовой установке `pip install -e .` без опциональных extras (`curl-cffi`, `playwright`, `langgraph`, `insightface`, `redis`).
   - В `pyproject.toml` обязательны `py-modules = ["cli", "config", "main"]` и `[tool.setuptools.package-data] web = ["static/*.html"]` (чтобы non-editable `pip install .` и Docker-сборка включали CLI и HTML).
4. **Безопасные дефолты:**
   - В `config.py` и `.env.example` всегда `DRY_RUN=true` и `AUTO_SEND_REPLIES=false`.

---

## 3. Карта архитектуры релиза v3.9.0

| Компонент | Файлы | За что отвечает |
|---|---|---|
| **Точки входа и конфиг** | `main.py`, `cli.py`, `config.py`, `pyproject.toml` | Запуск сервиса, CLI (`setup`, `doctor`, `platforms`, `test-ai`, `test-device`, `step`, `chat-step`, `har-inspect`, `har-export`, `run`), настройки из `.env` |
| **ИИ-шлюз и чат-агент** | `ai/universal_llm.py`, `ai/langgraph_agent.py`, `ai/chat_agent.py`, `ai/bandits.py`, `ai/calendar_tool.py` | OpenAI / Anthropic / Native Gemini / OpenRouter / DeepSeek / Ollama / Custom Header; 3-узловой граф `Profiler → Strategist → Humanizer`; Thompson Sampling Bandit (Redis или JSON); Google Calendar |
| **Визуальная автоматизация** | `core/ui_driver.py`, `core/image_utils.py`, `filters/vision_card_parser.py`, `core/visual_chat_worker.py` | Android ADB, Android/iOS Appium W3C, Playwright; VLM-граундинг кнопок `[0..1000]`; автозакрытие попапов; детекция `FLAG_SECURE`; сжатие PNG→JPEG; чтение и отправка в открытый чат; `launch_app()` |
| **API-контур (HAR → REST)** | `tools/har_importer.py`, `core/adapters/declarative_client.py`, `core/client.py`, `core/adapters/pure_and_cis.py`, `core/factory.py` | Экспорт `.har` дампа в `./data/api_profiles/<platform>.json`; авторефреш `401`, ретраи `429/5xx`, безопасный `204`, `curl_cffi` + `httpx` fallback для всех 28 платформ |
| **Ядро, фильтры, БД** | `core/worker.py`, `core/chat_worker.py`, `core/humanizer.py`, `core/proxy/manager.py`, `core/backup_service.py`, `filters/*`, `database/*` | Главный цикл свайпов (с проверкой дневного лимита внутри батча и изоляцией ошибок колбэков), скоринг Pure/без %, локальный FaceCheck, SQLite CRUD + онлайн-бэкапы |
| **Интерфейсы** | `web/api.py`, `web/events.py`, `web/schemas.py`, `web/static/dashboard.html`, `index.html`, `bot/telegram.py` | FastAPI Control Panel, WebSocket `/ws/live`, динамический выбор всех 28 платформ, календарь, сохранённые диалоги, Telegram-бот (`/start`, `/step`, `/chatstep`, `/reply`, `/slots`, `/book`, `/backup`) |
| **Внедрение и CI/CD** | `scripts/sync_to_repo.py`, `scripts/apply_to_repo.sh`, `scripts/apply_to_repo.ps1`, `.github/workflows/ci.yml`, `.github/workflows/pages.yml`, `Dockerfile`, `docker-compose.enterprise.yml` | Безопасная рекурсивная синхронизация в репо, CI на Python 3.10–3.12, деплой `index.html` на GitHub Pages, non-root Docker с `HEALTHCHECK` |

---

## 4. Пошаговый протокол внедрения (выполняй строго по порядку)

### Шаг 1. Preflight и страховка текущего репозитория

```bash
python --version   # требуется >= 3.10
git --version

# Если локального клона ещё нет:
git clone https://github.com/ssrjkk/twinby-swipe-bot.git ./twinby-swipe-bot

# Если клон уже существует — проверь ветку и создай страховочную ветку:
cd ./twinby-swipe-bot
git status
git branch backup-pre-v39 || true
cd ..
```

### Шаг 2. Предпросмотр и рекурсивная синхронизация

Используй **`scripts/sync_to_repo.py`** (работает одинаково на Windows, macOS и Linux):

```bash
# 1. Dry-run (ничего не записывает, показывает сколько файлов скопируется и удалится):
python heart3/scripts/sync_to_repo.py --target ./twinby-swipe-bot --dry-run

# 2. Реальная синхронизация (сохраняет .git/, .env, .env.bak, data/, .venv/):
python heart3/scripts/sync_to_repo.py --target ./twinby-swipe-bot
```

> Если нужен bash/PowerShell вариант:
> - Linux/macOS: `./heart3/scripts/apply_to_repo.sh ./twinby-swipe-bot`
> - Windows PowerShell: `.\heart3\scriptspply_to_repo.ps1 -TargetRepo .	winby-swipe-bot`

### Шаг 3. Чистое виртуальное окружение и установка

```bash
cd ./twinby-swipe-bot
python -m venv .venv

# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -e ".[test]"
```

*(Опционально — ставить только если пользователь явно использует эти режимы: `.[web-browser]` + `playwright install chromium`, `.[api]`, `.[langgraph]`, `.[vision-local]`, `.[calendar]`, `.[enterprise]`.)*

### Шаг 4. Обязательная матрица верификации (ALL MUST PASS)

Запусти все 6 проверок. Переходить к коммиту можно **только если каждая команда вернула exit code `0`**:

```bash
# 1. Компиляция байткода всех модулей
python -m compileall -q ai bot core database filters tools web scripts cli.py main.py config.py

# 2. Проверка shell-скрипта (Linux/macOS/Git Bash)
bash -n scripts/apply_to_repo.sh

# 3. Полный прогон pytest (unit + SQLite + FastAPI + WEB_API_TOKEN 401/200 + WebSocket + Telegram + loopback HTTP)
pytest -v

# 4. Полный прогон stdlib-тестов (контракты + E2E + ADB subprocess + W3C Appium + Multi-LLM Chaos)
python -m unittest discover -s tests -p "test_stdlib_*.py" -v

# 5. Проверка CLI entrypoint и реестра 28 платформ
heart3 --help
heart3 platforms

# 6. Проверка non-editable установки (как в Docker / production wheel)
python -m pip install --no-deps .
python -c "import cli, config, main; from pathlib import Path; import web; assert (Path(web.__file__).parent / 'static/dashboard.html').exists(); print('PACKAGING OK')"
pip install -e ".[test]"
```

### Шаг 5. Живой smoke-тест (если пользователь предоставил ключ ИИ / телефон / браузер)

```bash
# Если .env нет — скопируй шаблон (не перезаписывай существующий пользовательский .env!):
test -f .env || cp .env.example .env

# Проверка ключа ИИ (требует реальный LLM_API_KEY в .env):
heart3 doctor

# Если подключён Android по USB (adb devices) или включён MODE=web_vision:
heart3 test-device
heart3 step         # 1 карточка в DRY_RUN=true (реальный свайп не выполняется)
heart3 chat-step    # чтение открытого чата с экрана (черновик без отправки)
```

### Шаг 6. Аудит Git, коммит и публикация

```bash
git status
# ОБЯЗАТЕЛЬНО проверь: в untracked/modified НЕТ .env, .env.bak, data/, *.db, *.har, .venv/, __pycache__/

git add -A
git commit -m "feat: release <3 v3.9.0 (zero tech-debt, packaging fixed, full test matrix)"
git push origin main
```

Если в репозитории включён branch protection — создай ветку `feat/heart3-v3.9.0`, запушь её и открой PR.

### Шаг 7. Проверка после пуша в GitHub

1. **Actions → `<3 CI`:** убедись, что matrix-джобы Python `3.10`, `3.11` и `3.12` зелёные.
2. **Settings → Pages:** источник публикации должен быть **GitHub Actions**, после чего workflow `Deploy <3 Landing to GitHub Pages` обновит `https://ssrjkk.github.io/twinby-swipe-bot/`.

---

## 5. План отката (Rollback)

Если по какой-то причине нужно вернуться к прежнему состоянию репозитория:

```bash
git checkout backup-pre-v39
# либо отменить последний коммит без потери локальных .env и data/:
git reset --hard HEAD~1
```

Локальные `.env`, `.env.bak` и `data/heart3.db` никогда не отслеживаются Git и остаются в целости.

---

## 6. Плейбук диагностики (Troubleshooting)

| Проблема | Причина | Решение |
|---|---|---|
| `Утилита 'adb' не найдена в PATH` | Не установлен Android Platform-Tools | Поставить `platform-tools`, проверить `adb devices` |
| `screen_type: secure_black_screen` | Android-приложение (например Pure) ставит `FLAG_SECURE` | Использовать `MODE=web_vision` или Appium на устройстве с разрешённым захватом экрана |
| `401 Unauthorized` в `/dashboard` | В `.env` задан `WEB_API_TOKEN` | Ввести токен через кнопку **«🔑 API Token»** в шапке `/dashboard` (подставляется в `X-API-Token`, `?token=` для скриншота и `/ws/live?token=...`) |
| `MODE=api` возвращает `404`/`401` | Платформа обновила приватный API | Снять `.har` в Proxyman/Charles/mitmproxy и выполнить `heart3 har-export dump.har --platform <id>` — создастся `./data/api_profiles/<id>.json` |
| Кириллица не печатается по ADB | Стандартный `adb shell input text` не понимает UTF-8 | Установить на телефон `ADBKeyboard.apk` (`ADB_INPUT_B64` уже встроен) либо использовать `web_vision` / `appium` |
| Ошибка `ImportError: playwright` | Не установлен extra `[web-browser]` | `pip install -e ".[web-browser]" && playwright install chromium` |

---

## 7. Шаблон финального отчёта агента пользователю

После завершения внедрения выведи пользователю короткий отчёт:
- **Синхронизация:** сколько файлов скопировано/удалено, сохранены ли `.env` и `data/`.
- **Результаты проверок:** `compileall`, `pytest -v` (кол-во passed), `unittest` (кол-во OK), `heart3 platforms`, проверка packaging.
- **Git / CI:** ветка, хеш коммита, статус `git push` (или причина, если не пушил).
- **Следующий шаг для пользователя:** что нужно вписать в `.env` (например `LLM_API_KEY`) и команда запуска (`heart3 doctor && heart3 run`).
