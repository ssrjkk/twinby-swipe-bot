"""Контрактные и статические проверки целостности репозитория <3.
Запускаются без внешних зависимостей: `python -m unittest tests/test_stdlib_contracts.py` или `pytest`.
"""
import ast
import json
import py_compile
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestRepositoryContracts(unittest.TestCase):
    def test_01_all_python_files_compile_and_no_bare_except_or_strenum(self) -> None:
        py_files = sorted(ROOT.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 35)
        for pf in py_files:
            if "__pycache__" in pf.parts or ".venv" in pf.parts:
                continue
            py_compile.compile(str(pf), doraise=True)
            src = pf.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(pf.relative_to(ROOT)))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    self.assertIsNotNone(node.type, f"Bare except found in {pf}:{node.lineno}")
            if pf.name != "test_stdlib_contracts.py":
                self.assertNotIn("StrEnum", src, f"Python 3.10 incompatibility (StrEnum) in {pf}")
                self.assertNotIn("twinby_bot.db", src, f"Stale DB name in {pf}")

    def test_02_config_and_env_example_100_percent_parity(self) -> None:
        cfg_tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
        cfg_fields = set()
        for node in ast.walk(cfg_tree):
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        if item.target.id != "model_config":
                            cfg_fields.add(item.target.id.upper())
        env_keys = set(re.findall(r"^([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), flags=re.M))
        missing = cfg_fields - env_keys
        self.assertEqual(missing, set(), f"Missing fields in .env.example: {missing}")

    def test_03_landing_pages_synced_and_pink_bubble_design_preserved(self) -> None:
        root_idx = (ROOT / "index.html").read_text(encoding="utf-8")
        static_idx = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
        dash = (ROOT / "web/static/dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(root_idx, static_idx, "index.html and web/static/index.html diverged!")
        for page_name, html_text in [("index.html", root_idx), ("dashboard.html", dash)]:
            self.assertIn("#fffbfc", html_text, f"Light theme #fffbfc missing in {page_name}")
            self.assertIn(".orb", html_text, f"Floating pink bubbles (.orb) missing in {page_name}")
            self.assertIn("#e11d48", html_text, f"Rose accent missing in {page_name}")

    def test_04_dashboard_routes_and_security_escaping(self) -> None:
        api_src = (ROOT / "web/api.py").read_text(encoding="utf-8")
        dash_src = (ROOT / "web/static/dashboard.html").read_text(encoding="utf-8")
        bot_src = (ROOT / "bot/telegram.py").read_text(encoding="utf-8")
        cw_src = (ROOT / "core/chat_worker.py").read_text(encoding="utf-8")

        api_routes = set(re.findall(r'@app\.(?:get|post|patch|put|delete|websocket)\("([^"]+)"', api_src))
        js_calls = set(re.findall(r"fetchJson\([\'\"](/api/[^\'\"?]+)", dash_src)) | set(re.findall(r"apiPost\([\'\"](/api/[^\'\"]+)", dash_src))
        self.assertTrue(js_calls.issubset(api_routes), f"Dashboard calls unknown API routes: {js_calls - api_routes}")
        self.assertIn("escapeHtml(", dash_src)
        self.assertIn("X-API-Token", dash_src)
        self.assertIn("token=", dash_src)
        self.assertIn("html.escape(", bot_src)
        self.assertIn("html.escape(", cw_src)

    def test_05_repo_metadata_workflows_scripts_and_agent_guide(self) -> None:
        for req in [
            "AGENT_INTEGRATION_GUIDE.md",
            "README.md",
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "Dockerfile",
            "Makefile",
            ".gitignore",
            ".dockerignore",
            ".env.example",
            ".github/workflows/ci.yml",
            ".github/workflows/pages.yml",
            "scripts/sync_to_repo.py",
            "scripts/apply_to_repo.sh",
            "scripts/apply_to_repo.ps1",
        ]:
            self.assertTrue((ROOT / req).exists(), f"Missing required repo asset: {req}")

        # Verify version parity across pyproject, api, cli, SESSION_STATE
        st = json.loads((ROOT / "SESSION_STATE.json").read_text(encoding="utf-8"))
        ver = st["version"]
        self.assertIn(f'version = "{ver}"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn(f'version="{ver}"', (ROOT / "web/api.py").read_text(encoding="utf-8"))
        self.assertIn(f"v{ver}", (ROOT / "cli.py").read_text(encoding="utf-8"))

        # Verify bash syntax of apply_to_repo.sh when bash is installed
        if shutil.which("bash"):
            res = subprocess.run(["bash", "-n", str(ROOT / "scripts/apply_to_repo.sh")], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"bash -n failed: {res.stderr}")



    def test_06_pyproject_packaging_includes_top_level_modules_and_static_html(self) -> None:
        pyp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('py-modules = ["cli", "config", "main"]', pyp)
        self.assertIn('[tool.setuptools.package-data]', pyp)
        self.assertIn('web = ["static/*.html"]', pyp)

    def test_07_dockerfile_and_compose_have_non_root_user_and_healthchecks(self) -> None:
        df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dc = (ROOT / "docker-compose.enterprise.yml").read_text(encoding="utf-8")
        self.assertIn("USER heart3", df)
        self.assertIn("HEALTHCHECK", df)
        self.assertIn("/api/v1/health", df)
        self.assertGreaterEqual(dc.lower().count("healthcheck:"), 3)

    def test_08_zero_unused_settings_in_runtime_code(self) -> None:
        cfg_tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
        cfg_fields = []
        for node in ast.walk(cfg_tree):
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        if item.target.id != "model_config":
                            cfg_fields.append(item.target.id)
        runtime_src = ""
        for pf in ROOT.rglob("*.py"):
            if pf.name != "config.py" and "tests" not in pf.parts:
                runtime_src += pf.read_text(encoding="utf-8") + "\n"
        unused = [f for f in cfg_fields if f"settings.{f}" not in runtime_src]
        self.assertEqual(unused, [], f"Unused config.py settings: {unused}")

    def test_09_dashboard_uses_dynamic_28_platforms_and_calendar(self) -> None:
        dash = (ROOT / "web/static/dashboard.html").read_text(encoding="utf-8")
        self.assertIn("/api/v1/platforms", dash)
        self.assertIn("/api/v1/calendar/slots", dash)

    def test_10_startup_checks_bind_before_uvicorn_and_menu_before_polling(self) -> None:
        """Порядок запуска в main(): защита bind'а раньше сервера, меню команд раньше polling'а.

        Сами по себе guard_public_bind и register_bot_commands бесполезны, если их никто не зовёт,
        поэтому проверяется именно вызов и его место, а не только существование функции.
        """
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        calls: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"guard_public_bind", "register_bot_commands", "Config", "start_polling"}:
                calls.setdefault(name, node.lineno)
        self.assertEqual(
            sorted(calls),
            ["Config", "guard_public_bind", "register_bot_commands", "start_polling"],
            f"main() потерял обязательный вызов: нашлось {sorted(calls)}",
        )
        self.assertLess(calls["guard_public_bind"], calls["Config"], "открытый bind проверяется уже после сборки сервера")
        self.assertLess(calls["register_bot_commands"], calls["start_polling"], "меню команд грузится после старта polling'а")


if __name__ == "__main__":
    unittest.main()
