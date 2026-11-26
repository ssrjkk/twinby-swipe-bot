import os
import tempfile
import unittest
from pathlib import Path

from config import upsert_env_values, write_private_text
from core.adapters.declarative_client import DeclarativeRestDatingClient
from core.safe_paths import resolve_within
from tools.har_importer import export_api_profile_from_har


class TestPathConfinement(unittest.TestCase):
    def test_relative_and_absolute_paths_inside_base_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(resolve_within(base, "profile.json"), base / "profile.json")
            inside = base / "nested" / "profile.json"
            self.assertEqual(resolve_within(base, inside), inside)

    def test_traversal_and_absolute_outside_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            base.mkdir()
            for candidate in ("../secret.json", Path(tmp) / "secret.json"):
                with self.assertRaises(ValueError):
                    resolve_within(base, candidate)

    def test_profile_loader_rejects_outside_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            base.mkdir()
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                DeclarativeRestDatingClient.from_profile_file(outside, profiles_dir=base)

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
            self.assertNotIn("second\n", text)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
