# <3 — AI Matchmaking Agent

[![CI](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml)

Локальный мультиплатформенный ассистент для дейтинга с визуальным анализом анкет и чатов (VLM-граундинг кнопок), настраиваемым скорингом, 3-узловым чат-агентом (`Profiler → Strategist → Humanizer`), Thompson Sampling бандитами, Web Control Panel (`/dashboard`), Google Calendar и Telegram-ботом.

Лендинг проекта: **https://ssrjkk.github.io/twinby-swipe-bot/**  
Гайд для AI-агента по внедрению в репозиторий: **`AGENT_INTEGRATION_GUIDE.md`**

## Три рабочих контура

1. **`MODE=ui_vision` (Android ADB / Android & iOS Appium):**
   Снимает 1–2 экрана карточки, проверяет `FLAG_SECURE`, сжимает скриншот до JPEG, через VLM извлекает поля анкеты и координаты кнопок `like_button` / `pass_button` / `dismiss_button`, автоматически закрывает попапы и нажимает нужную кнопку (или свайпает по кривой Безье).
2. **`MODE=web_vision` (Playwright):**
   Открывает веб-версию выбранной платформы (`PLATFORM=mamba|vk_dating|tinder|bumble|badoo|tabor|...` — URL подбирается автоматически из реестра 28 платформ) с постоянным профилем `./data/browser_profile`.
3. **`MODE=api` (Декларативный HAR-профиль):**
   Выполните `heart3 har-export dump.har --platform twinby`, чтобы из реального `.har` дампа сгенерировать `./data/api_profiles/twinby.json` — `DeclarativeRestDatingClient` автоматически подхватит его с ретраями 429/5xx, cookies и авторефрешем токена на 401.

## Быстрый старт

```bash
pip install -e ".[test]"
pytest -v
python -m unittest discover -s tests -p "test_stdlib_*.py" -v

# Интерактивная настройка .env (любой ИИ-провайдер и формат ключа)
heart3 setup
heart3 doctor

# Проверка устройства / браузера и 1-шаговый тест
heart3 test-device
heart3 step
heart3 chat-step

# Запуск сервиса (лендинг :8080/, Control Panel :8080/dashboard)
heart3 run
```

## Внедрение в репозиторий `ssrjkk/twinby-swipe-bot`

```bash
# Универсально (Windows / macOS / Linux):
python scripts/sync_to_repo.py --target ./twinby-swipe-bot --clone --commit --push

# Или через shell / PowerShell:
./scripts/apply_to_repo.sh /path/to/twinby-swipe-bot --commit --push
.\scripts\apply_to_repo.ps1 -TargetRepo C:\path\to\twinby-swipe-bot -Commit -Push
```
