# Архитектурный журнал проекта `<3` (v3.9.0)

## 1. Реализованные контуры работы
- **Визуал (`index.html`, `web/static/index.html`, `web/static/dashboard.html`):** полностью сохранён оригинальный светлый дизайн (`#fffbfc`), типографика `Instrument Serif` + `Plus Jakarta Sans` + `JetBrains Mono` и анимированные плавающие розовые пузыри/сферы.
- **UI-Vision (`MODE=ui_vision`):** Android ADB и Android/iOS Appium W3C с автозапуском приложений (`launch_app`), детекцией `FLAG_SECURE`, сжатием скриншотов до JPEG, VLM-граундингом кнопок (`like_button`, `pass_button`, `dismiss_button`), автозакрытием попапов, опциональной локальной проверкой лиц (`FaceConsistencyChecker`) и антибан-ритмом (`Humanizer`).
- **Web-Vision (`MODE=web_vision`):** Playwright с постоянным профилем браузера, автоподбором URL из реестра 28 платформ, картой CSS-селекторов (11 веб-дейтингов) и VLM-граундингом кнопок.
- **Декларативный API (`MODE=api`):** `heart3 har-export` конвертирует реальный `.har` дамп в `./data/api_profiles/<platform>.json`; `DeclarativeRestDatingClient`, `TwinbyStealthClient`, `PureClient` и `TaborClient` поддерживают и `curl_cffi`, и fallback на `httpx.AsyncClient`.
- **Чат-агент и календарь:** 3-узловой `DatingLangGraphEngine` (`Profiler → Strategist → Humanizer`) + `PromptThompsonBandit` с автонаградой в `ChatWorker`, визуальный `VisualChatAssistant` (`/chatstep` в Telegram и кнопка в `/dashboard`), персистентные диалоги в SQLite (`dialogue_threads`, `chat_messages`) и Google Calendar (`/slots`, `/book`, `/api/v1/calendar/*`).
- **Внедрение в `ssrjkk/twinby-swipe-bot`:** `scripts/sync_to_repo.py` (Windows/macOS/Linux), `scripts/apply_to_repo.sh`, `scripts/apply_to_repo.ps1`, `.github/workflows/ci.yml` и `.github/workflows/pages.yml`.
