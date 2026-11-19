import os
from dotenv import load_dotenv

load_dotenv()

# AI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AGENT = os.getenv("AGENT", "claude")  # claude | openai | gemini | custom

# Бот
LIKE_THRESHOLD = int(os.getenv("LIKE_THRESHOLD", "75"))
MAX_SWIPES = int(os.getenv("MAX_SWIPES", "50"))
DELAY_MIN = float(os.getenv("DELAY_MIN", "3"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "7"))

# Twinby
TWINBY_PACKAGE = "com.twinby"
TWINBY_ACTIVITY = "com.twinby.MainActivity"
TWINBY_BUNDLE = "com.twinby.app"

# Appium
APPIUM_HOST = os.getenv("APPIUM_HOST", "http://127.0.0.1:4723")
