# &lt;3 — Heart3

[![CI](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml)

**<3 (Heart3)** — новое имя Twinby Bot: автоматизация Twinby на устройстве
и экспериментальное API-ядро на Python 3.11+. Python-пакет называется `heart3`;
адрес репозитория пока прежний. Идентификаторы приложения, переменные `TWINBY_*`,
путь существующей базы и Docker-volume не переименовываются, чтобы не потерять данные.
[Витрина](https://ssrjkk.github.io/twinby-swipe-bot/) · [Архитектура](ARCHITECTURE.md) · [Изменения](CHANGELOG.md)

## Что реализовано — и где границы

| Компонент | Фактическое состояние |
|---|---|
| Device-слой | Отдельные циклы Android (uiautomator2), iPhone и iOS Simulator на Mac (Appium/XCUITest). Фото и bio передаются выбранному vision-провайдеру. |
| API-запуск | `main.py` создаёт один Twinby-клиент, Telegram-пульт и FastAPI. Worker запускается кнопкой в Telegram, не автоматически. |
| Реестр | 28 спецификаций: 10 помечены direct API, 12 hybrid, 6 UI-only. Это проектные метаданные, **не подтверждение поддержки 28 сервисов**. |
| Адаптеры | Код отдельных клиентов есть для Twinby, Pure и Tabor. Остальные записи фабрика отклоняет. Контракты API проверены только моками; актуальность реальных эндпоинтов не подтверждена. |
| Скоринг | Возраст, расстояние, наличие фото/bio и порог совместимости. Нативная оценка либо текстовый LLM-скоринг. В API-ядре нет извлечения признаков из фотографий. |
| Порог | По умолчанию **не ниже 75%**, включая ровно 75. `MIN_COMPATIBILITY` и `LIKE_THRESHOLD` настраиваются независимо. |
| AI-модули | LangGraph, Redis bandits и Google Calendar — отдельные экспериментальные компоненты. Они **не подключены к мэтчам**, не ведут переписку и не бронируют встречи автоматически. |
| Web | Публичная информационная страница и защищённые REST/WS endpoints; браузерной панели управления нет. События из worker в WS пока не публикуются. |
| Проверки | <!-- test-count -->302 тестов в коллекции; число сверяется с витриной. Прохождение моков не заменяет проверку на устройстве и контрактов сервиса. |

В этой сборке нет доказанной защиты от блокировок, работающего multi-account swarm
или гарантии совместимости с текущей версией приложений. Используйте только собственный
аккаунт и разрешённые сервисом способы доступа. Учитывайте правила платформ и передачу
личных данных профиля внешнему AI-провайдеру. Не выводите диагнозы или чувствительные
характеристики человека из фото.

## Установка

```bash
git clone https://github.com/ssrjkk/twinby-swipe-bot.git
cd twinby-swipe-bot
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
```

В PowerShell вместо `cp` можно использовать `Copy-Item .env.example .env`.
Никогда не коммитьте заполненный `.env`.

## Запуск API-ядра

В `.env` обязательны `TWINBY_ACCESS_TOKEN`, `TG_BOT_TOKEN` и положительный
`TG_ADMIN_ID`. Для web-доступа задайте отдельный `WEB_ADMIN_TOKEN`; пустое значение
отключает доступ к данным, а не авторизацию. Для LLM заполните `AI_PROVIDER`,
`LLM_BASE_URL`, `LLM_API_KEY` и `LLM_MODEL` согласованно с контрактом провайдера.

```bash
python main.py
```

В Telegram: `/start` → «Запустить» / «Остановить» / «Статистика».
HTTP по умолчанию слушает только `127.0.0.1:8080`. Не открывайте его наружу без TLS
и ограничения доступа. REST `/api/v1/overview` и `/api/v1/matches`, а также WebSocket
`/ws/live` требуют `Authorization: Bearer <WEB_ADMIN_TOKEN>`; секреты в URL не принимаются.
WS предназначен для клиентов, поддерживающих заголовки, не для browser WebSocket API.

Одна база — один аккаунт и один процесс. Повторные попытки неизвестного результата
свайпа не отправляются автоматически: намерение записывается до запроса и занимает
дневной лимит. Статистика включает такие попытки, а не только подтверждённые действия.
Бэкап отправляется Telegram-администратору только для файловой SQLite-базы, указанной
в `DATABASE_URL`; это передача данных внешнему сервису. PostgreSQL и `:memory:` таким
бэкапом не обслуживаются. Смешивать аккаунты/платформы в одной базе нельзя.

## Device-слой

Для device-слоя токены Telegram/Twinby API не нужны. Заполните `AGENT` и ключ
выбранного провайдера. `AGENT=custom` — шаблон, не готовая AI-модель.

```bash
# Android с USB-отладкой и установленным Twinby
adb devices
python -m device.run_android

# iPhone — Appium должен работать на macOS с Xcode
npm install -g appium
appium driver install xcuitest
appium
# В другом терминале, IOS_UDID и IOS_PLATFORM_VERSION заданы в .env
python -m device.run_ios

# iOS Simulator на Mac: приложение должно быть установлено в симулятор
# SIM_UDID и SIM_PLATFORM_VERSION заданы в .env
python -m device.run_mac
```

OpenAI/Gemini vision SDK устанавливаются отдельно:

```bash
python -m pip install openai google-genai
```

Проверки драйверов используют подставное устройство: живые свайпы и совместимость
текущих UI-селекторов с Twinby на телефоне не проверены.

## Настройки

Полный шаблон — [.env.example](.env.example).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MIN_COMPATIBILITY` / `LIKE_THRESHOLD` | `75` / `75` | Независимые пороги API/device |
| `DAILY_LIKE_LIMIT` | `150` | API: дневной бюджет попыток лайка (UTC) |
| `MIN_SWIPE_DELAY` / `MAX_SWIPE_DELAY` | `2.5` / `7.5` | Базовая пауза API; дополнительные паузы могут быть длиннее |
| `MAX_DISTANCE_KM`, `MIN_AGE`, `MAX_AGE` | `25`, `18`, `30` | Фильтры API |
| `REQUIRE_BIO` | `false` | Требовать непустое описание |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/twinby_bot.db` | Отдельная база одного аккаунта |
| `WEB_ADMIN_TOKEN` | пусто | Отдельный секрет web API; пусто = нет доступа |
| `WEB_ALLOWED_ORIGINS` | localhost / 127.0.0.1:8080 | Разрешённые origin, не замена авторизации |
| `AGENT` | `claude` | Device: claude / openai / gemini / custom |
| `MAX_SWIPES` | `50` | Максимум итераций device-сессии |
| `DELAY_MIN` / `DELAY_MAX` | `3` / `7` | Паузы device-слоя |

## Экспериментальные AI-компоненты

```bash
python -m pip install ".[agent]"
```

- `UniversalAIGateway`: OpenAI-совместимые endpoints и Anthropic Messages.
  URL и модель задаются явно; выбор `AI_PROVIDER` не подбирает их автоматически.
  Gemini поддерживается **только через OpenAI-совместимый endpoint**, заканчивающийся
  на `/openai`, не через нативный `generateContent` API.
- `DatingLangGraphEngine.run_turn`: черновик ответа и результат редактора;
  максимум два прохода редактора (один возврат к стратегу). Не отправляет сообщения.
- `PromptThompsonBandit`: выбор вариантов и запись результатов в предоставленный
  Redis-клиент. Автоматического сбора результатов нет.
- `book_dating_event_tool`: отдельный явный вызов создаёт 90-минутное событие в личном
  Google Calendar через `GOOGLE_*`. Требуются OAuth-разрешения; модуль не рассылает
  приглашения и не подключён к графу или worker.

## Проверка

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -v
python -m black --check --line-length=88 agents device tests ai core database filters web bot config.py main.py
python -m flake8 agents device tests ai core database filters web bot config.py main.py --max-complexity=10 --max-line-length=120
python -m pip_audit --strict
```

Тестовая конфигурация заменяет ключи и отключает чтение `.env`. Python-соединения
ограничивает pytest-socket; native curl entrypoints блокируются до коллекции тестов,
а проверяемые адаптеры используют httpx/respx. Это защита тестового процесса,
не OS-level сетевая песочница. CI настроен на Python 3.11/3.12; локальный успешный
прогон не означает, что удалённый CI уже выполнился.

## Структура

`device/` и `agents/` — автоматизация устройства; `core/`, `filters/`, `database/` —
экспериментальное API-ядро; `ai/` — отдельные AI-компоненты; `bot/` — Telegram-пульт;
`web/` — HTTP/WS API и копия витрины; `docs/` — GitHub Pages; `tests/` — проверки.

## English

**<3 (Heart3)**, formerly Twinby Bot, combines on-device automation with an experimental, **single-account Twinby
API runtime**. The registry contains 28 platform specifications, not 28 verified
integrations. Only Twinby, Pure and Tabor have dedicated client code; their live API
contracts remain unverified. Unsupported factory selections fail explicitly.

The default threshold is **at least 75**, not strictly greater than 75. Device and API
thresholds are independent. LangGraph, Redis bandits and Google Calendar are standalone
experiments, not an automatic post-match conversation or booking pipeline. The web root
is a public landing page, not a dashboard; data endpoints use a separate Bearer token.
Use one account/process per database. Unknown swipe outcomes remain counted attempts
and are not retried automatically. Never treat mock tests as proof of live compatibility.

Install `requirements.txt`, configure `.env.example`, then run `python main.py` or
`python -m device.run_android` (`run_ios` / `run_mac` on an Appium/Xcode host).
Install `.[agent]` for standalone AI components and `requirements-dev.txt` for tests.

**SSRJKK** — [GitHub](https://github.com/ssrjkk) · [Telegram](https://t.me/ssrjkk) · [Projects hub](https://ssrjkk.github.io/) · [Bot catalogue](https://t.me/ssrjkk_bot)
