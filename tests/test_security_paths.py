import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import upsert_env_values, write_private_text
from core.adapters import declarative_client
from core.adapters.declarative_client import DeclarativeRestDatingClient
from core.safe_names import safe_filename
from tools.har_importer import export_api_profile_from_har


class TestFilenameConfinement(unittest.TestCase):
    def test_safe_filename_accepts_only_plain_names(self) -> None:
        self.assertEqual(safe_filename("RealApp"), "realapp")
        self.assertEqual(safe_filename("real-app_2"), "real-app_2")

    def test_safe_filename_rejects_paths_and_traversal(self) -> None:
        for candidate in ("../secret.json", "/tmp/secret.json", "nested/profile.json", "..\\secret.json"):
            with self.assertRaises(ValueError):
                safe_filename(candidate)

    def test_profile_loader_uses_the_fixed_profiles_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with patch.object(declarative_client, "API_PROFILES_DIR", base):
                with self.assertRaises(ValueError):
                    DeclarativeRestDatingClient.from_profile_file(str(outside))
                inside = base / "realapp.json"
                inside.write_text("{}", encoding="utf-8")
                with self.assertRaises(ValueError):
                    DeclarativeRestDatingClient.from_profile_file("realapp.json")

    def test_har_export_rejects_platform_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har = Path(tmp) / "capture.har"
            har.write_text('{"log":{"entries":[]}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                export_api_profile_from_har(str(har), platform="../escape", output_dir=str(Path(tmp) / "out"))


class TestPrivateEnvWrites(unittest.TestCase):
    def test_runtime_env_writes_use_the_private_helper(self) -> None:
        root = Path(__file__).resolve().parent.parent
        config_src = (root / "config.py").read_text(encoding="utf-8")
        cli_src = (root / "cli.py").read_text(encoding="utf-8")
        self.assertIn("def write_private_text", config_src)
        self.assertIn("write_private_text(env_path", cli_src)
        self.assertIn("os.chmod(backup, 0o600)", cli_src)

    def test_env_writes_are_private_and_single_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_private_text(path, "TOKEN=secret\n")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            upsert_env_values({"LLM_API_KEY": "first\nsecond"}, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("LLM_API_KEY=firstsecond", text)
            self.assertNotIn("\nsecond", text)
            self.assertEqual(text.splitlines()[-1], "LLM_API_KEY=firstsecond")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
