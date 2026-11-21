"""Единый визуальный язык лендинга и панели (доработка «А»): палитра, пузыри, типографика, стекло.
Проверки чисто текстовые и работают без единой внешней зависимости:
`python -m unittest tests/test_stdlib_unified_design.py` или `pytest`.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANDING = (ROOT / "index.html").read_text(encoding="utf-8")
DASH = (ROOT / "web/static/dashboard.html").read_text(encoding="utf-8")

# Жёсткая инвариантная палитра гайда: эти семь токенов обязаны совпадать на обеих страницах.
CORE_TOKENS = ("--bg", "--surface", "--text", "--muted", "--rose", "--rose-soft", "--rose-border")


def css_vars(src: str) -> dict[str, str]:
    """Собирает `:root { --ключ: значение; }` в словарь (значение нормализовано по пробелам)."""
    block = re.search(r":root\s*\{(.*?)\}", src, flags=re.DOTALL)
    assert block, ":root block not found"
    out: dict[str, str] = {}
    for line in block.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip().rstrip(";").strip()
        if key.startswith("--"):
            out[key] = re.sub(r"\s+", " ", value)
    return out


def rule_body(src: str, selector: str) -> str:
    """Тело первого CSS-правила с точным селектором, например `.card`."""
    match = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)\}}", src, flags=re.DOTALL)
    assert match, f"CSS rule for {selector!r} not found"
    return re.sub(r"\s+", " ", match.group(1)).strip()


def declarations(css_rule: str) -> dict[str, str]:
    """`.a { x: 1; y: 2 }` -> {'x': '1', 'y': '2'} — сравниваем панели по набору объявлений."""
    out: dict[str, str] = {}
    for chunk in css_rule.split(";"):
        if ":" in chunk:
            key, _, value = chunk.partition(":")
            out[key.strip()] = value.strip()
    return out


def keyframes_body(src: str, name: str) -> str:
    """Тело @keyframes через баланс скобок: правило бывает и в одну строку, и в несколько."""
    start = re.search(rf"@keyframes\s+{re.escape(name)}\s*\{{", src)
    assert start, f"@keyframes {name} not found"
    depth = 0
    for idx in range(start.end() - 1, len(src)):
        depth += src[idx] == "{"
        depth -= src[idx] == "}"
        if depth == 0:
            return re.sub(r"\s+", " ", src[start.end():idx]).strip()
    raise AssertionError(f"@keyframes {name} has unbalanced braces")


def visible_text(chunk: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)).strip()


class TestUnifiedDesignLanguage(unittest.TestCase):
    def test_01_landing_and_dashboard_share_the_exact_palette(self) -> None:
        landing_vars, dash_vars = css_vars(LANDING), css_vars(DASH)
        for token in CORE_TOKENS:
            self.assertIn(token, landing_vars, f"{token} missing on the landing")
            self.assertIn(token, dash_vars, f"{token} missing in the dashboard")
            self.assertEqual(
                landing_vars[token],
                dash_vars[token],
                f"{token} drifted apart: landing {landing_vars[token]!r} vs dashboard {dash_vars[token]!r}",
            )
        self.assertEqual(dash_vars["--bg"], "#fffbfc", "Light theme must stay #fffbfc")
        self.assertEqual(dash_vars["--rose"], "#e11d48")

    def test_02_both_pages_carry_three_blobs_and_five_glossy_orbs(self) -> None:
        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            self.assertEqual(len(re.findall(r'<div class="blob blob-\d"', src)), 3, f"blob count in {name}")
            self.assertEqual(len(re.findall(r'<div class="orb orb-\d"', src)), 5, f"orb count in {name}")
            self.assertEqual(len(re.findall(r"(?m)^\s*\.blob-\d\s*\{", src)), 3, f"blob rules in {name}")
            self.assertEqual(len(re.findall(r"(?m)^\s*\.orb-\d\s*\{", src)), 5, f"orb rules in {name}")
            self.assertEqual(
                sorted(set(re.findall(r"@keyframes (\w+)", src))),
                ["floatOrb", "riseBubble"],
                f"animation set changed in {name}",
            )
            self.assertIn('class="blobs"', src, f"bubble layer missing in {name}")

    def test_03_bubble_and_blob_animations_are_identical(self) -> None:
        for kf in ("floatOrb", "riseBubble"):
            self.assertEqual(
                keyframes_body(LANDING, kf),
                keyframes_body(DASH, kf),
                f"@keyframes {kf} diverged between landing and dashboard",
            )
            self.assertIn("0%", keyframes_body(DASH, kf))

    def test_04_typography_stack_is_shared_and_serif_italic_is_actually_used(self) -> None:
        landing_link = re.search(r"fonts\.googleapis\.com/css2\?([^\"]+)", LANDING)
        dash_link = re.search(r"fonts\.googleapis\.com/css2\?([^\"]+)", DASH)
        self.assertIsNotNone(landing_link)
        self.assertIsNotNone(dash_link)
        self.assertEqual(landing_link.group(1), dash_link.group(1), "Google Fonts payloads differ")
        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            for family in ("Instrument Serif", "Plus Jakarta Sans", "JetBrains Mono"):
                self.assertIn(family, src, f"{family} not used in {name}")
            self.assertIn("'Instrument Serif', Georgia, serif", src, f"serif italic accent unused in {name}")
            self.assertIn("font-style: italic", src, f"italic accent style missing in {name}")
        self.assertIn("<em>Control Panel</em>", DASH, "serif italic accent missing in the panel header")

    def test_05_glass_card_recipe_is_copied_verbatim(self) -> None:
        landing_card, dash_card = declarations(rule_body(LANDING, ".card")), declarations(rule_body(DASH, ".card"))
        for prop in ("background", "backdrop-filter", "border", "border-radius", "padding"):
            self.assertIn(prop, landing_card)
            self.assertEqual(
                landing_card[prop],
                dash_card.get(prop),
                f"dashboard .card `{prop}` is {dash_card.get(prop)!r}, landing has {landing_card[prop]!r}",
            )
        self.assertEqual(landing_card["border-radius"], "24px")

    def test_06_primary_button_is_the_landing_rose_pill(self) -> None:
        landing_btn = declarations(rule_body(LANDING, ".btn-primary"))
        dash_btn = declarations(rule_body(DASH, "button"))
        for prop in ("background", "color", "border-radius", "box-shadow", "font-weight"):
            self.assertEqual(
                landing_btn[prop],
                dash_btn.get(prop),
                f"dashboard button `{prop}` is {dash_btn.get(prop)!r}, landing .btn-primary has {landing_btn[prop]!r}",
            )
        self.assertEqual(landing_btn["box-shadow"], "0 12px 26px -6px rgba(225, 29, 72, 0.35)")
        self.assertIn("transform: translateY(-2px)", rule_body(DASH, "button:hover"))
        self.assertIn("transform: translateY(-2px)", rule_body(LANDING, ".btn-primary:hover"))

    def test_07_header_and_footer_carry_the_landing_copy(self) -> None:
        landing_footer = re.search(r"<footer>(.*?)</footer>", LANDING, flags=re.DOTALL).group(1)
        dash_footer = re.search(r"<footer>(.*?)</footer>", DASH, flags=re.DOTALL).group(1)
        self.assertEqual(visible_text(landing_footer), visible_text(dash_footer), "footer copy diverged from the landing")
        for href in ("https://github.com/ssrjkk/twinby-swipe-bot", "https://ssrjkk.github.io/"):
            self.assertIn(href, DASH, f"{href} link missing in the panel footer")
        self.assertIn(".sec-eyebrow", DASH, "landing eyebrow class not ported into the panel")
        self.assertGreaterEqual(DASH.count('class="sec-eyebrow"'), 8, "panel cards lost their eyebrow labels")

    def test_08_panel_still_ships_the_landing_without_touching_it(self) -> None:
        self.assertEqual(
            (ROOT / "index.html").read_bytes(),
            (ROOT / "web/static/index.html").read_bytes(),
            "index.html and web/static/index.html must stay byte-identical",
        )
        self.assertIn("#fffbfc", LANDING)
        self.assertIn("#e11d48", LANDING)

    def test_09_dashboard_ui_text_is_free_of_pictograms(self) -> None:
        """Панель — Serious UI: никаких эмодзи и пиктограмм в кнопках, заголовках и статусах, только типографика."""
        pictographic = ((0x1F000, 0x1FAFF), (0x1F1E6, 0x1F1FF), (0x2100, 0x2BFF), (0xFE0F, 0xFE0F))
        allowed = set("✓✗·—→←↗★«»…№’")
        found = sorted(
            {
                ch
                for ch in DASH
                if any(lo <= ord(ch) <= hi for lo, hi in pictographic) and ch not in allowed
            }
        )
        self.assertEqual(found, [], f"в интерфейсе панели появились пиктограммы: {found}")
        self.assertIn("WS: LIVE", DASH, "живой статус-бейдж WebSocket пропал из панели")



if __name__ == "__main__":
    unittest.main()
