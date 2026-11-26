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


# Вызовы, которые сами нейтрализуют подстановку: их аргументы проверять не нужно.
ESCAPE_WRAPPERS = frozenset({"escapeHtml", "encodeURIComponent", "Number", "String", "rawOf", "JSON.stringify"})
JS_LITERALS = frozenset({"true", "false", "null", "undefined", "NaN", "Infinity"})
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def skip_balanced(text: str, open_idx: int, pairs: str = "{}") -> int:
    """Индекс пары к символу `pairs[0]`, стоящему на open_idx."""
    depth, idx = 0, open_idx
    while idx < len(text):
        depth += text[idx] == pairs[0]
        depth -= text[idx] == pairs[1]
        if depth == 0:
            return idx
        idx += 1
    raise AssertionError(f"неспаренный {pairs[0]!r} в фрагменте {text[:60]!r}")


def js_interpolations(expr: str, base: int = 0) -> list[tuple[str, int]]:
    """Пары `(содержимое, абсолютная позиция)` для каждого `${...}`, включая вложенные шаблоны.

    `re.findall(r"\\$\\{([^{}]*)\\}")` теряет подстановки с фигурными скобками внутри — условие с
    вложенным шаблоном как раз и прячет туда неэкранированную вставку.
    """
    out: list[tuple[str, int]] = []
    idx = 0
    while True:
        start = expr.find("${", idx)
        if start < 0:
            return out
        end = skip_balanced(expr, start + 1)
        out.append((expr[start + 2:end], base + start + 2))
        idx = end + 1


def js_code_identifiers(expr: str, base: int = 0) -> list[tuple[str, int]]:
    """Идентификаторы «кодового» контекста выражения с абсолютными позициями.

    Текст строковых и шаблонных литералов (HTML-разметка) и аргументы безопасных обёрток не выдаются
    за данные: в первом нет подстановок, во втором они уже экранированы.
    """
    ids: list[tuple[str, int]] = []
    idx, n = 0, len(expr)
    while idx < n:
        ch = expr[idx]
        if ch in "'\"":
            idx += 1
            while idx < n and expr[idx] != ch:
                idx += 2 if expr[idx] == "\\" else 1
            idx += 1
        elif ch == "`":
            idx += 1
            while idx < n and expr[idx] != "`":
                if expr[idx] == "$" and expr[idx + 1:idx + 2] == "{":
                    end = skip_balanced(expr, idx + 1)
                    ids.extend(js_code_identifiers(expr[idx + 2:end], base + idx + 2))
                    idx = end + 1
                else:
                    idx += 1
            idx += 1
        else:
            match = IDENT_RE.match(expr, idx)
            if not match:
                idx += 1
                continue
            token = match.group(0)
            if token in ESCAPE_WRAPPERS:
                paren = expr.find("(", match.end())
                if paren >= 0 and expr[match.end():paren].strip() == "":
                    idx = skip_balanced(expr, paren, "()") + 1  # аргументы обёртки уже экранированы
                    continue
            ids.append((token, base + match.end()))
            idx = match.end()
    return ids


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
            self.assertIn("prefers-reduced-motion", src, f"reduced-motion guard missing in {name}")

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

    def test_09_landing_and_dashboard_ui_text_are_free_of_pictograms(self) -> None:
        """Serious UI: никаких эмодзи и пиктограмм в кнопках, заголовках и статусах, только типографика."""
        pictographic = ((0x1F000, 0x1FAFF), (0x1F1E6, 0x1F1FF), (0x2100, 0x2BFF), (0xFE0F, 0xFE0F))
        allowed = set("✓✗·—→←↗★«»…№’")
        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            found = sorted(
                {
                    ch
                    for ch in src
                    if any(lo <= ord(ch) <= hi for lo, hi in pictographic) and ch not in allowed
                }
            )
            self.assertEqual(found, [], f"в интерфейсе {name} появились пиктограммы: {found}")
        self.assertIn("WS: LIVE", DASH, "живой статус-бейдж WebSocket пропал из панели")

    def test_10_structural_rules_are_landing_verbatim(self) -> None:
        """Каркас панели — это CSS лендинга без правок: сетка, шапка, hero, карточки, чипы, вкладки."""
        shared = (
            "body", ".container", "header", "footer", "footer a", "section", ".stats", ".stat-card",
            ".stat-card b", ".stat-card span", ".logo", ".logo i", ".tag", "h1 em", "h2", ".sec-sub",
            ".sec-eyebrow", ".card h3", ".card p", ".btn-secondary", ".gh-link", ".gh-link:hover",
            ".mode-tabs", ".mode-btn", ".mode-btn.active", ".drv", ".drv-api", ".drv-hybrid", ".drv-ui",
        )
        for selector in shared:
            self.assertEqual(
                declarations(rule_body(LANDING, selector)),
                declarations(rule_body(DASH, selector)),
                f"CSS-правило {selector} разбилось между лендингом и панелью",
            )
        # Компактность hero панели — единственное разрешённое отступление: только размер заголовка.
        self.assertIn("<h1>Управление. <em>Осмысленно.</em></h1>", DASH, "hero панели потерял курсивный акцент")
        self.assertLess(
            float(re.search(r"clamp\(([\d.]+)rem", rule_body(DASH, ".panel-hero h1")).group(1)),
            float(re.search(r"clamp\(([\d.]+)rem", rule_body(LANDING, "h1")).group(1)),
            "заголовок панели должен быть компактнее лендингового",
        )

    def test_11_nested_surfaces_and_button_hierarchy_match_the_landing(self) -> None:
        pane, mode_content = declarations(rule_body(DASH, ".pane")), declarations(rule_body(LANDING, ".mode-content"))
        for prop in ("background", "border", "border-radius"):
            self.assertEqual(pane[prop], mode_content[prop], f"вложенная белая подложка разошлась с `.mode-content` в {prop}")
        self.assertEqual(pane["border-radius"], "18px")
        self.assertEqual(
            declarations(rule_body(DASH, "details.adv code")),
            declarations(rule_body(LANDING, ".mode-content code")),
            "моно-врезка панели не совпадает с лендинговой",
        )
        self.assertEqual(
            declarations(rule_body(LANDING, ".btn-primary")),
            declarations(rule_body(DASH, ".btn-primary")),
            ".btn-primary панели обязан быть розовой pill-кнопкой лендинга целиком",
        )
        self.assertIn("box-shadow: none", rule_body(DASH, "button.btn-secondary"))
        # На карточку — одна розовая кнопка: вторичные действия обязаны быть классом btn-secondary.
        body = DASH.split("<body>", 1)[1].split("<script>", 1)[0]
        cards = re.split(r'\n        <div class="card">', body)
        self.assertGreaterEqual(len(cards), 9, "карточек панели стало меньше восьми")
        for card in cards[1:]:
            self.assertLessEqual(
                card.count('class="btn-primary"'), 1, "в карточке больше одной главной (розовой) кнопки"
            )
        self.assertIn('class="btn-secondary"', body)
        self.assertGreaterEqual(body.count('class="btn-secondary"'), 8, "вторичные действия панели потеряли свой стиль")

    def test_12_fields_chips_and_states_are_unified(self) -> None:
        """Единый стиль полей, фокус-кольцо и disabled-состояние вместо самостийных инлайн-стилей."""
        body = DASH.split("<body>", 1)[1].split("<script>", 1)[0]
        self.assertNotIn('style="', body, "в разметке панели снова полезли инлайн-атрибуты style=")
        self.assertNotIn(" style=", body)
        fields = declarations(rule_body(DASH, "input, select, textarea"))
        self.assertEqual(fields["border-radius"], "14px")
        self.assertEqual(fields["background"], "#fff")
        focus = declarations(rule_body(DASH, "input:focus-visible, select:focus-visible, textarea:focus-visible"))
        self.assertEqual(focus["outline"], "none")
        self.assertEqual(focus["border-color"], "var(--rose)")
        self.assertEqual(focus["box-shadow"], "0 0 0 3px rgba(225, 29, 72, 0.14)")
        self.assertIn("cursor: progress", rule_body(DASH, "button:disabled"))
        self.assertIn('class="drv drv-hybrid"', body, "статусные чипы панели должны жить в стиле лендингового .drv")
        for chip in ("wsBadge", "modeBadge", "stateBadge", "dryBadge"):
            self.assertIn(f'id="{chip}"', body, f"статус-чип {chip} пропал из шапки/карточек")

    def test_13_no_raw_json_in_the_interface(self) -> None:
        """`JSON.stringify(r)` в интерфейс запрещён: человекочитаемый статус + сворачиваемый моно-блок."""
        self.assertNotIn("JSON.stringify(r)", DASH)
        for line in DASH.splitlines():
            self.assertFalse("innerHTML" in line and "JSON.stringify" in line, f"сырой JSON лезет в innerHTML: {line}")
        self.assertIn(".textContent = raw", DASH, "сырые детали должны попадать в DOM только как текст")
        self.assertIn("out-raw", DASH, "нет компактного моно-блока для отладочных деталей")
        self.assertIn("function notify(", DASH, "нет единого канала статус-сообщений")

    def test_14_every_api_call_from_the_panel_is_a_registered_route(self) -> None:
        """Паритет маршрутов: любая строка /api/v1/... из панели обязана существовать в web/api.py."""
        api_src = (ROOT / "web/api.py").read_text(encoding="utf-8")
        routes = set(re.findall(r'@app\.(?:get|post|patch|put|delete|websocket)\("([^"]+)"', api_src))

        def registered(path: str) -> bool:
            # `/api/v1/dialogues/` + `${threadId}` — это маршрут с параметром `/api/v1/dialogues/{thread_id}`.
            return any(path == r or (r.startswith(path) and r[len(path):len(path) + 1] == "{") for r in routes)

        used = {
            p.split("?")[0]
            for p in re.findall(r"""['"](/api/v1/[^' "{$?]+\??[^' "{$]*)""", DASH)
            + re.findall(r"`(/api/v1/[^` ${?]+\??[^` ${]*)", DASH)
        }
        self.assertTrue(used, "панель перестала дёргать API")
        self.assertFalse(
            [p for p in sorted(used) if not registered(p)],
            f"панель зовёт несуществующие маршруты: {sorted(p for p in used if not registered(p))}",
        )
        self.assertIn("/ws/live", DASH)

    def test_15_connection_and_ai_cards_stay_scoped_to_their_mode(self) -> None:
        """Режим задаёт набор полей, а пресеты ИИ приходят из бэкенда, а не из воздуха."""
        api_src = (ROOT / "web/api.py").read_text(encoding="utf-8")
        modes = set(re.findall(r'^\s{4}"(\w+)": \(', re.search(r"MODE_DRIVERS[^\n]*=\s*\{(.*?)\n\}", api_src, re.DOTALL).group(1), re.M))
        self.assertEqual(modes, {"ui_vision", "web_vision", "api"})
        tabs = set(re.findall(r'class="mode-btn[^"]*" data-mode="(\w+)"', DASH))
        self.assertEqual(tabs, modes, "вкладки режимов панели разошлись с MODE_DRIVERS бэкенда")

        llm_src = (ROOT / "ai/universal_llm.py").read_text(encoding="utf-8")
        presets = re.findall(r'^\s{4}"(\w+)": \("https?://', re.search(r"PROVIDER_PRESETS[^\n]*=\s*\{(.*?)\n\}", llm_src, re.DOTALL).group(1), re.M)
        self.assertGreaterEqual(len(presets), 13, "пресетов провайдеров стало меньше тринадцати")
        self.assertIn("list_provider_presets", api_src)
        self.assertIn("aiPresets = c.presets", DASH, "панель обязана строить список провайдеров из ответа API")

        wrappers = set(re.findall(r'showField\(\'(\w+)\'', DASH))
        self.assertEqual(wrappers, {"wrapOs", "wrapUdid", "wrapAppium", "wrapPkg", "wrapWebUrl", "wrapHeadless", "wrapToken"})
        for wrapper in wrappers:
            self.assertIn(f'id="{wrapper}"', DASH, f"нет контейнера поля {wrapper} — переключение режимов его не покажет")

    def test_16_dynamic_markup_is_always_escaped(self) -> None:
        """Ни одной подстановки в innerHTML без экранирования: `${объект.поле}` и `${переменная}` — провал гейта.

        Проходят литералы, вызовы функций, обращения только внутри условия, `.length` (это число) и
        имена с суффиксом `Html` — по соглашению это уже собранный фрагмент разметки, см. renderReply.
        """
        statement_ends = re.compile(r"(?m)^\s*.*;\s*$")
        offenders: list[str] = []
        for match in re.finditer(r"\.innerHTML\s*=", DASH):
            tail = statement_ends.search(DASH, match.end())
            statement = DASH[match.start(): tail.end() if tail else match.end() + 400]
            for expr, base in js_interpolations(statement):
                for token, end in js_code_identifiers(expr, base):
                    rest = statement[end:].lstrip()
                    if rest[:1] == "(" or rest[:1] == "?" or rest[:2] in ("==", "!=", "&&", "||") or rest[:3] in ("===", "!=="):
                        continue  # обращение участвует только в условии или в вызове — в DOM оно не попадает
                    if token in JS_LITERALS or token.endswith(".length") or token.endswith("Html"):
                        continue
                    offenders.append(token)
        self.assertEqual(offenders, [], f"в innerHTML попало неэкранированное выражение: {sorted(set(offenders))}")
        self.assertIn("function escapeHtml(", DASH)
        self.assertGreaterEqual(DASH.count("escapeHtml("), 30, "экранирования в панели подозрительно мало")
        self.assertEqual(js_code_identifiers("escapeHtml(a.b)", 0), [], "гейт слеп к аргументам обёртки")
        sample = "a ? `<b>${a.c}</b>` : ''"
        self.assertEqual([part for part, _ in js_interpolations(sample, 0)], ["a.c"], "гейт слеп к вложенным шаблонам")
        self.assertEqual([token for token, _ in js_code_identifiers(sample, 0)], ["a", "a.c"],
                         "обращение внутри вложенного шаблона обязано доходить до проверки")

    def test_17_each_mode_reveals_only_its_own_fields(self) -> None:
        """Переключатель прячет поля чужого канала: ОС — только у adb/appium, URL — только у web_vision."""
        conds = dict(re.findall(r"showField\('(\w+)', ([^)\n]*)\)", DASH))
        self.assertEqual(set(conds), {"wrapOs", "wrapUdid", "wrapAppium", "wrapPkg", "wrapWebUrl", "wrapHeadless", "wrapToken"})
        expected = {
            "wrapOs": ("ui_vision",),
            "wrapPkg": ("ui_vision",),
            "wrapWebUrl": ("web_vision",),
            "wrapHeadless": ("web_vision",),
            "wrapToken": ("api",),
        }
        for wrapper, tokens in expected.items():
            for token in tokens:
                self.assertIn(token, conds[wrapper], f"поле {wrapper} показывается не в своём режиме: {conds[wrapper]}")
            other_modes = {"ui_vision", "web_vision", "api"} - {tokens[0]}
            self.assertFalse(
                [m for m in other_modes if re.search(rf"mode\s*===\s*'{m}'", conds[wrapper])],
                f"поле {wrapper} открыто и в чужом режиме: {conds[wrapper]}",
            )

    def test_18_mutating_requests_cannot_be_double_clicked(self) -> None:
        """Любой POST/PATCH живёт внутри withBusy: кнопка гаснет на время запроса и получает текст-статус."""
        busy_spans = [(m.start(), m.end()) for m in re.finditer(r"await withBusy\(", DASH)]
        self.assertGreaterEqual(len(busy_spans), 10, "busy-обёртка потеряна у большинства запросов панели")
        for match in re.finditer(r"method:\s*'(POST|PATCH|DELETE)'", DASH):
            enclosed = any(start < match.start() for start, _ in busy_spans)
            self.assertTrue(enclosed, f"запись в API вне withBusy на позиции {DASH[:match.start()].count(chr(10)) + 1}")
        body = re.search(r"async function withBusy\([^)]*\)\s*\{(.*?)\n    \}", DASH, re.DOTALL).group(1)
        self.assertIn("btn.disabled = true", body)
        self.assertIn("btn.textContent = busyText", body)
        self.assertIn("btn.disabled = false", body, "кнопка не возвращается в активное состояние после ответа")

    def test_19_landing_header_wraps_before_it_overflows(self) -> None:
        """На 390px шапка обязана переноситься: без этого документ шире вьюпорта на 46px."""
        static_idx = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
        for name, src in (("index.html", LANDING), ("web/static/index.html", static_idx)):
            media = re.search(r"@media \(max-width: 640px\) \{(.*?)\}\s*\n", src, flags=re.DOTALL)
            self.assertIsNotNone(media, f"{name}: нет мобильной правки шапки")
            block = re.sub(r"\s+", " ", media.group(1))
            self.assertIn("header { flex-wrap: wrap", block, f"{name}: header не переносится на мобильном")
            self.assertIn("nav a { white-space: nowrap", block, f"{name}: пункты меню рвутся по словам")
            self.assertNotIn("overflow-x: hidden", block, f"{name}: переполнение замазано, а не исправлено")

    def test_20_panel_button_never_leads_to_a_404(self) -> None:
        """На GitHub Pages панель не раздаётся, поэтому кнопка ведёт в «Быстрый старт», а локально — в /dashboard."""
        self.assertIn('href="/dashboard"', LANDING, "разметка обязана оставаться рабочей под FastAPI")
        self.assertIn("location.hostname.endsWith('github.io')", LANDING)
        readme_anchor = "https://github.com/ssrjkk/twinby-swipe-bot#"
        self.assertIn(readme_anchor, LANDING, "нет запасной ссылки для GitHub Pages")
        self.assertIn("Запустить локально", LANDING, "подпись на Pages должна объяснять, что дальше")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        slug = LANDING.split(readme_anchor, 1)[1].split("'")[0]
        self.assertIn(f"## {slug.replace('-', ' ').capitalize()}", readme, f"якорь #{slug} ведёт в несуществующий раздел README")

    def test_21_narrow_viewport_hero_type_is_clamped(self) -> None:
        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            self.assertIn("@media (max-width: 380px)", src, f"{name}: narrow hero breakpoint missing")
            self.assertIn("h1 { font-size: clamp(2.2rem, 11vw, 2.6rem); }", src, f"{name}: narrow hero type can overflow")

    def test_22_bubbles_render_above_the_page_background(self) -> None:
        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            self.assertEqual(declarations(rule_body(src, ".blobs"))["z-index"], "0", f"{name}: bubbles are hidden below the canvas")
            container = declarations(rule_body(src, ".container"))
            self.assertEqual(container["position"], "relative", f"{name}: content stacking context missing")
            self.assertEqual(container["z-index"], "1", f"{name}: content must stay above bubbles")

    def test_23_rose_ink_on_soft_surface_meets_aa(self) -> None:
        def luminance(value: str) -> float:
            value = value.lstrip("#")
            channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
            linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
            return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722

        for name, src in (("index.html", LANDING), ("dashboard.html", DASH)):
            tokens = css_vars(src)
            ink, soft = luminance(tokens["--rose-ink"]), luminance(tokens["--rose-soft"])
            ratio = (max(ink, soft) + 0.05) / (min(ink, soft) + 0.05)
            self.assertGreaterEqual(ratio, 4.5, f"{name}: rose ink contrast is {ratio:.2f}:1")
            selectors = [".gh-link", ".tag", ".mode-content code"]
            if name == "index.html":
                selectors.append(".step-badge")
            for selector in selectors:
                self.assertEqual(declarations(rule_body(src, selector))["color"], "var(--rose-ink)", f"{name}: {selector} lost accessible ink")


if __name__ == "__main__":
    unittest.main()
