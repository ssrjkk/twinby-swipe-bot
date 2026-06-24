"""
Mac Bot — iOS Simulator + Appium (Apple Silicon M1/M2/M3)
==========================================================
xcrun simctl list devices | grep -i booted
appium &
python run_mac.py

.env:
  AGENT=claude
  SIM_UDID=XXXX-XXXX-...
  SIM_PLATFORM_VERSION=17.0
"""

import os
import random
import time

from appium import webdriver
from appium.options import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException

from agent_factory import create_agent
from config import APPIUM_HOST, DELAY_MAX, DELAY_MIN, LIKE_THRESHOLD, MAX_SWIPES, TWINBY_BUNDLE

SIM_UDID             = os.getenv("SIM_UDID", "")
SIM_PLATFORM_VERSION = os.getenv("SIM_PLATFORM_VERSION", "17.0")


def create_driver():
    opts = XCUITestOptions()
    opts.bundle_id        = TWINBY_BUNDLE
    opts.platform_version = SIM_PLATFORM_VERSION
    opts.automation_name  = "XCUITest"
    opts.is_simulator     = True
    opts.no_reset         = True
    if SIM_UDID:
        opts.udid = SIM_UDID
    driver = webdriver.Remote(APPIUM_HOST, options=opts)
    print(f"[device] iOS Simulator  ios={SIM_PLATFORM_VERSION}")
    return driver


def close_popups(driver):
    for label in ["Close", "Later", "Not Now", "Закрыть", "Позже"]:
        try:
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, label).click()
            time.sleep(0.3)
        except NoSuchElementException:
            pass


def screenshot(driver) -> bytes:
    return driver.get_screenshot_as_png()


def get_data(driver) -> tuple[list[bytes], str]:
    shots = [screenshot(driver)]
    size  = driver.get_window_size()
    w, h  = size["width"], size["height"]
    for _ in range(2):
        driver.tap([(int(w * 0.72), int(h * 0.38))])
        time.sleep(0.4)
        shots.append(screenshot(driver))
    bio = ""
    for acc_id in ["bio", "about", "description"]:
        try:
            bio = driver.find_element(AppiumBy.ACCESSIBILITY_ID, acc_id).text or ""
            break
        except NoSuchElementException:
            pass
    return shots, bio


def swipe_card(driver, decision: str):
    size = driver.get_window_size()
    w, h = size["width"], size["height"]
    cx, cy = w // 2, int(h * 0.45)
    btn_ids = {"like": ["like", "Like", "heart"], "skip": ["dislike", "Dislike", "nope"]}
    for acc_id in btn_ids[decision]:
        try:
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, acc_id).click()
            return
        except NoSuchElementException:
            pass
    target_x = int(w * 0.9) if decision == "like" else int(w * 0.1)
    driver.swipe(cx, cy, target_x, cy, 300)


def main():
    agent = create_agent()
    print("=" * 50)
    print(f"  Twinby Bot  |  Mac Simulator  |  {agent.name.upper()}")
    print("=" * 50)

    driver = create_driver()
    time.sleep(3)
    stats = {"likes": 0, "skips": 0, "errors": 0}

    for i in range(1, MAX_SWIPES + 1):
        close_popups(driver)
        time.sleep(random.uniform(0.5, 1.0))

        try:
            shots, bio = get_data(driver)
        except Exception as e:
            print(f"[{i:02d}] ⚠️  данные: {e}")
            stats["errors"] += 1
            swipe_card(driver, "skip")
            time.sleep(2)
            continue

        try:
            result = agent.analyze(shots, bio)
        except Exception as e:
            print(f"[{i:02d}] ⚠️  агент: {e}")
            stats["errors"] += 1
            swipe_card(driver, "skip")
            time.sleep(2)
            continue

        if result.decision == "like" and result.confidence < LIKE_THRESHOLD:
            result.decision = "skip"

        icon = "💚" if result.decision == "like" else "❌"
        print(f"[{i:02d}] {icon} {result.decision.upper():4s}  {result.confidence:3d}%  — {result.reason}")
        swipe_card(driver, result.decision)
        stats["likes" if result.decision == "like" else "skips"] += 1
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    driver.quit()
    print(f"\n  Готово! 💚 {stats['likes']}  ❌ {stats['skips']}  ⚠️ {stats['errors']}")


if __name__ == "__main__":
    main()
