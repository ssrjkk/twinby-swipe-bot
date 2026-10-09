<div align="center">

# 🤖 Twinby Bot

[![CI](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)



**AI-агент для автоматического подбора анкет в Twinby**

[![CI](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ssrjkk/twinby-swipe-bot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Анализирует фото и bio через Claude / GPT-4o / Gemini,  
ставит лайк только при совпадении выше 75%.  
Работает на Android, iPhone и Mac (iOS Simulator).

</div>

---

## ✨ Возможности

- 🧠 **Мультиагентность** — Claude, OpenAI, Gemini или свой агент
- 📱 **Три платформы** — Android (uiautomator2), iPhone и Mac (Appium)
- 👁️ **Vision-анализ** — скрины анкеты + bio отправляются в AI
- 🎯 **Умный отбор** — лайк только при уверенности > 75%
- 🔄 **CI/CD** — GitHub Actions на Python 3.10 / 3.11 / 3.12
- 🧩 **Расширяемость** — добавь своего агента за 3 шага

---

## 📁 Структура

```
twinby-swipe-bot/
├── .github/workflows/ci.yml   # GitHub Actions
├── agents/
│   ├── base.py                # BaseAgent + Decision
│   ├── claude_agent.py        # Anthropic Claude
│   ├── openai_agent.py        # OpenAI GPT-4o
│   ├── gemini_agent.py        # Google Gemini
│   └── custom_agent.py        # шаблон для своего
├── tests/
│   └── test_all.py            # 35 тестов
├── agent_factory.py           # фабрика агентов
├── config.py                  # настройки из .env
├── run_android.py             # запуск на Android
├── run_ios.py                 # запуск на iPhone
├── run_mac.py                 # запуск на Mac (Simulator)
├── requirements.txt
└── .env.example
```

---

## 🚀 Быстрый старт

```bash
git clone https://github.com/ssrjkk/twinby-swipe-bot.git
cd twinby-swipe-bot
pip install -r requirements.txt
cp .env.example .env          # заполни ключи
```

---

## 📱 Запуск

### Android
```bash
# Включи USB-отладку на телефоне
adb devices                   # убедись что виден
python run_android.py
```

### iPhone
```bash
npm install -g appium
appium driver install xcuitest
appium &                      # в отдельном терминале

# UDID телефона:
xcrun xctrace list devices

# В .env → IOS_UDID=твой-udid
python run_ios.py
```

### Mac (iOS Simulator, Apple Silicon)
```bash
# Xcode → Simulator → запусти iPhone
# Установи Twinby в симулятор

# UDID симулятора:
xcrun simctl list devices | grep -i booted

# В .env → SIM_UDID=твой-udid
appium &
python run_mac.py
```

---

## 🤖 Агенты

Выбор агента через `.env`:

```env
AGENT=claude    # по умолчанию
AGENT=openai
AGENT=gemini
```

Или при запуске:
```bash
AGENT=openai python run_android.py
```

### Добавить своего агента

1. Скопируй `agents/custom_agent.py` → `agents/my_agent.py`
2. Реализуй метод `analyze(screenshots, bio) -> Decision`
3. В `agent_factory.py` добавь в `REGISTRY`:
   ```python
   "myagent": ("agents.my_agent", "MyAgent"),
   ```
4. В `.env`: `AGENT=myagent`

---

## ⚙️ Настройки `.env`

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AGENT` | `claude` | Агент: claude / openai / gemini / custom |
| `ANTHROPIC_API_KEY` | — | Ключ Claude |
| `OPENAI_API_KEY` | — | Ключ OpenAI |
| `GEMINI_API_KEY` | — | Ключ Gemini |
| `LIKE_THRESHOLD` | `75` | Мин. уверенность для лайка (0–100) |
| `MAX_SWIPES` | `50` | Лимит свайпов за сессию |
| `DELAY_MIN` | `3` | Мин. пауза между свайпами (сек) |
| `DELAY_MAX` | `7` | Макс. пауза между свайпами (сек) |
| `IOS_UDID` | `auto` | UDID iPhone |
| `IOS_PLATFORM_VERSION` | `17.0` | Версия iOS |
| `SIM_UDID` | — | UDID iOS Simulator |

---

## 🧪 Тесты

```bash
pytest tests/ -v
# 35 passed
```

CI запускается автоматически на каждый push в main.

---

## 📦 Зависимости

```bash
pip install -r requirements.txt

# Опционально (нужный агент):
pip install openai            # для AGENT=openai
pip install google-genai      # для AGENT=gemini
```

---

## 👤 Автор

**Ситников Сергей Алексеевич**  
QA Automation Engineer · Saint Petersburg

[![GitHub](https://img.shields.io/badge/GitHub-ssrjkk-181717?logo=github)](https://github.com/ssrjkk)
[![Telegram](https://img.shields.io/badge/Telegram-@ssrjkk-2CA5E0?logo=telegram)](https://t.me/ssrjkk)
[![Email](https://img.shields.io/badge/Email-ray013lefe@gmail.com-D14836?logo=gmail)](mailto:ray013lefe@gmail.com)

---

<div align="center">
<sub>Stack: Python · Playwright · Appium · uiautomator2 · Claude · pytest · GitHub Actions</sub>
</div>


## Installation

```bash
git clone https://github.com/ssrjkk/twinby-swipe-bot.git
cd twinby-swipe-bot
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```
