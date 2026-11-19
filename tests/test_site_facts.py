"""Гейты витрины: опубликованные цифры обязаны совпадать с кодом и прогоном.

Числа в docs/index.html (и его зеркале web/static/index.html) сверяются с
реестром платформ, порогом продукта и реальной коллекцией pytest; мета-теги,
README и og-card проверяются на согласованность и отсутствие устаревших заявлений.
"""

import html
import re
import tomllib
import struct
import subprocess
import sys
from pathlib import Path

from config import settings
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from filters.engine import FilterConfig

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "docs" / "index.html"
MIRROR = ROOT / "web" / "static" / "index.html"
SHOWCASE = ROOT / "docs" / "showcase" / "index.html"
README = ROOT / "README.md"


def _html() -> str:
    return LANDING.read_text(encoding="utf-8")


def _pill(name: str) -> str:
    match = re.search(rf'<b data-gate="{name}">([^<]+)</b>', _html())
    assert match, f"пилюля data-gate={name} отсутствует"
    return match.group(1).strip()


def _plain_pills() -> list[str]:
    return re.findall(r'<div class="pill"><b>([^<]+)</b>', _html())


def test_heart3_brand_is_consistent_and_archive_notes_are_not_published():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "heart3"
    for path in (LANDING, MIRROR, SHOWCASE):
        source = path.read_text(encoding="utf-8")
        title = html.unescape(re.search(r"<title>([^<]+)</title>", source).group(1))
        assert title.startswith("<3 —")
        assert "Twinby Bot" not in source
        assert "twinby bot" not in source
        assert "&lt;3" in source
    assert README.read_text(encoding="utf-8").startswith("# &lt;3 — Heart3")
    assert not list(ROOT.glob("SESSION_*"))


def test_landing_and_served_mirror_are_identical():
    assert LANDING.read_bytes() == MIRROR.read_bytes()


def test_threshold_pill_matches_product_invariant():
    import device.config as device_config

    assert _plain_pills()[0] == "75%"
    assert settings.min_compatibility == 75
    assert device_config.LIKE_THRESHOLD == 75
    assert FilterConfig().min_compatibility == 75


def test_platform_pills_match_registry():
    counts = {t: 0 for t in AutomationDriverType}
    for spec in GLOBAL_DATING_REGISTRY.values():
        counts[spec.driver] += 1
    pills = _plain_pills()
    assert pills[1] == str(len(GLOBAL_DATING_REGISTRY)) == "28"
    assert pills[2] == "3"
    assert len(list((ROOT / "device").glob("run_*.py"))) == 3
    html = _html()
    assert f"{counts[AutomationDriverType.DIRECT_API]} · direct API" in html
    assert f"{counts[AutomationDriverType.HYBRID_API_UI]} · hybrid API + UI" in html
    assert f"{counts[AutomationDriverType.UI_DRIVER_ONLY]} · UI-only" in html


def test_tests_pill_equals_real_pytest_collection():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    assert match, proc.stdout[-2000:]
    assert _pill("tests") == match.group(1)


def test_meta_tags_are_consistent_and_current():
    html = _html()
    desc = re.search(r'<meta name="description" content="([^"]+)"', html).group(1)
    og = re.search(r'<meta property="og:description" content="([^"]+)"', html).group(1)
    tw = re.search(r'<meta name="twitter:description" content="([^"]+)"', html).group(1)
    assert desc == og == tw
    assert "28" in desc and "75%" in desc
    assert "InsightFace" not in html and "CLIP" not in html


def test_og_card_is_1200x630_png():
    data = (ROOT / "docs" / "og-card.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1200, 630)


def test_showcase_page_links_home_and_uses_module_launch():
    html = SHOWCASE.read_text(encoding="utf-8")
    assert 'href="../index.html"' in html
    assert "python -m device.run_android" in html


def test_readme_states_measured_facts_and_module_launch():
    text = README.read_text(encoding="utf-8")
    assert re.search(r"<!-- test-count -->(\d+) тестов", text).group(1) == _pill(
        "tests"
    )
    assert "не подключены к мэтчам" in text
    assert "не подтверждение поддержки 28 сервисов" in text
    assert "python -m device.run_android" in text
    assert "39 passed" not in text
    assert "python run_android.py" not in text
    assert ".env.example" in text
