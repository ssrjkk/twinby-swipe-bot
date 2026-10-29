"""
Android Bot — uiautomator2
==========================
pip install uiautomator2 anthropic python-dotenv
adb devices

.env:  AGENT=claude  (или openai / gemini / custom)
python run_android.py
"""

import random
import time
from io import BytesIO

import uiautomator2 as u2

from agent_factory import create_agent
from config import (
    DELAY_MAX,
    DELAY_MIN,
    LIKE_THRESHOLD,
    MAX_SWIPES,
    TWINBY_ACTIVITY,
    TWINBY_PACKAGE,
)

LIKE_IDS = ["btn_like", "button_like", "iv_like", "like_button", "ic_like"]
SKIP_IDS = ["btn_dislike", "button_dislike", "iv_dislike", "nope_button", "ic_dislike"]
CLOSE_IDS = ["btn_close", "button_close", "iv_close", "close"]
CLOSE_TEXTS = ["Закрыть", "Позже", "Не сейчас", "Пропустить", "✕"]
BIO_IDS = ["tv_about", "tv_bio", "tv_description", "about_text", "tv_name"]


def connect(serial=None) -> u2.Device:
    d = u2.connect(serial)
    print(f"[device] {d.serial}")
    return d


def close_popups(d: u2.Device):
    for text in CLOSE_TEXTS:
        el = d(text=text)
        if el.exists(timeout=0.4):
            el.click()
            time.sleep(0.3)
    for rid in CLOSE_IDS:
        el = d(resourceIdMatches=f".*{rid}.*")
        if el.exists(timeout=0.3):
            el.click()
            time.sleep(0.3)


def screenshot(d: u2.Device) -> bytes:
    img = d.screenshot(format="pillow")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_data(d: u2.Device) -> tuple[list[bytes], str]:
    shots = [screenshot(d)]
    w, h = d.window_size()
    for _ in range(2):
        d.click(w * 0.72, h * 0.38)
        time.sleep(0.4)
        shots.append(screenshot(d))
    bio = ""
    for rid in BIO_IDS:
        el = d(resourceIdMatches=f".*{rid}.*")
        if el.exists(timeout=0.4):
            bio = el.get_text() or ""
            break
    return shots, bio


def swipe(d: u2.Device, decision: str):
    ids = LIKE_IDS if decision == "like" else SKIP_IDS
    for rid in ids:
        el = d(resourceIdMatches=f".*{rid}.*")
        if el.exists(timeout=0.4):
            el.click()
            return
    w, h = d.window_size()
    cx, cy = w // 2, h // 2
    target_x = int(w * 0.9) if decision == "like" else int(w * 0.1)
    d.swipe(cx, cy, target_x, cy, duration=0.25)


def main():
    agent = create_agent()
    print("=" * 50)
    print(f"  Twinby Bot  |  Android  |  {agent.name.upper()}")
    print("=" * 50)

    d = connect()
    d.app_start(TWINBY_PACKAGE, activity=TWINBY_ACTIVITY, wait=True)
    time.sleep(3)

    stats = {"likes": 0, "skips": 0, "errors": 0}

    for i in range(1, MAX_SWIPES + 1):
        close_popups(d)
        time.sleep(random.uniform(0.5, 1.0))

        try:
            shots, bio = get_data(d)
        except Exception as e:
            print(f"[{i:02d}] ⚠️  данные: {e}")
            stats["errors"] += 1
            swipe(d, "skip")
            time.sleep(2)
            continue

        try:
            result = agent.analyze(shots, bio)
        except Exception as e:
            print(f"[{i:02d}] ⚠️  агент: {e}")
            stats["errors"] += 1
            swipe(d, "skip")
            time.sleep(2)
            continue

        if result.decision == "like" and result.confidence < LIKE_THRESHOLD:
            result.decision = "skip"

        icon = "💚" if result.decision == "like" else "❌"
        print(
            f"[{i:02d}] {icon} {result.decision.upper():4s}  {result.confidence:3d}%  — {result.reason}"
        )
        swipe(d, result.decision)
        stats["likes" if result.decision == "like" else "skips"] += 1
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    print(f"\n  Готово! 💚 {stats['likes']}  ❌ {stats['skips']}  ⚠️ {stats['errors']}")


if __name__ == "__main__":
    main()
