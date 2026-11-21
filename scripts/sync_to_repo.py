#!/usr/bin/env python3
"""Кроссплатформенный (Windows/macOS/Linux) скрипт безопасного внедрения <3 v3.9.0 в репозиторий
https://github.com/ssrjkk/twinby-swipe-bot без зависимости от rsync/robocopy.

Рекурсивно синхронизирует все файлы и подкаталоги, удаляет устаревшие файлы даже внутри вложенных папок,
при этом ГАРАНТИРОВАННО сохраняет .git/, .env, .env.bak, data/, .venv/, venv/.

Примеры:
  python scripts/sync_to_repo.py --target ./twinby-swipe-bot --dry-run
  python scripts/sync_to_repo.py --target ./twinby-swipe-bot --clone --commit
  python scripts/sync_to_repo.py --target ./twinby-swipe-bot --commit --push
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/ssrjkk/twinby-swipe-bot.git"
PRESERVE_NAMES = {".git", ".env", ".env.bak", "data", ".venv", "venv", "__pycache__", ".pytest_cache"}


def _force_utf8_stdout() -> None:
    # Windows consoles default to the ANSI codepage (cp1251 on ru-RU), which cannot encode the
    # report glyphs and aborts main() with UnicodeEncodeError after the sync already ran.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _is_preserved(rel_path: Path) -> bool:
    return any(part in PRESERVE_NAMES for part in rel_path.parts)


def sync_directories(src_root: Path, target_repo: Path, dry_run: bool = False) -> dict[str, int]:
    copied = 0
    deleted = 0

    # 1. Collect all source relative file paths
    src_files: set[Path] = set()
    for root, dirs, filenames in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in PRESERVE_NAMES]
        rel_dir = Path(root).relative_to(src_root)
        for fn in filenames:
            if fn.endswith(".pyc") or fn in PRESERVE_NAMES:
                continue
            rel_file = rel_dir / fn
            src_files.add(rel_file)

    # 2. Recursively remove stale files inside target_repo (including nested subdirs!) while preserving user data
    for root, dirs, filenames in os.walk(target_repo, topdown=False):
        rel_dir = Path(root).relative_to(target_repo)
        if _is_preserved(rel_dir):
            continue
        for fn in filenames:
            rel_file = rel_dir / fn
            if _is_preserved(rel_file):
                continue
            if rel_file not in src_files:
                if not dry_run:
                    (target_repo / rel_file).unlink()
                deleted += 1
        # Remove empty stale directories (except repo root and preserved dirs)
        if rel_dir != Path(".") and not (src_root / rel_dir).exists():
            if not dry_run:
                try:
                    (target_repo / rel_dir).rmdir()
                except OSError:
                    pass

    # 3. Copy all project files cleanly
    for rel_file in sorted(src_files):
        src_file = src_root / rel_file
        dest_file = target_repo / rel_file
        if not dry_run:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
        copied += 1

    return {"copied": copied, "deleted": deleted}


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print(">>>", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def main() -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Внедрение <3 v3.9.0 в репозиторий twinby-swipe-bot")
    parser.add_argument("--target", required=True, help="Путь к локальному клону twinby-swipe-bot")
    parser.add_argument("--clone", action="store_true", help="Склонировать репозиторий, если папки ещё нет")
    parser.add_argument("--branch", default="", help="Создать/переключиться на ветку перед коммитом")
    parser.add_argument("--commit", action="store_true", help="Сделать git add -A && git commit")
    parser.add_argument("--push", action="store_true", help="Выполнить git push после коммита")
    parser.add_argument("--dry-run", action="store_true", help="Только показать количество операций без записи")
    args = parser.parse_args()

    src_root = Path(__file__).resolve().parent.parent
    target = Path(args.target).resolve()

    if not target.exists():
        if args.clone:
            _run(["git", "clone", REPO_URL, str(target)])
        else:
            print(f"❌ Папка {target} не найдена. Добавьте --clone или выполните git clone {REPO_URL}")
            return 1

    if not (target / ".git").exists():
        print(f"❌ {target} не является git-репозиторием.")
        return 1

    if args.branch and not args.dry_run:
        _run(["git", "checkout", "-B", args.branch], cwd=target)

    stats = sync_directories(src_root, target, dry_run=args.dry_run)
    print(
        f"✅ Синхронизация завершена: скопировано={stats['copied']}, "
        f"удалено устаревших={stats['deleted']} (dry_run={args.dry_run})"
    )

    if args.dry_run:
        return 0

    _run(["git", "status", "--short"], cwd=target)
    if args.commit:
        _run(["git", "add", "-A"], cwd=target)
        _run(
            ["git", "commit", "-m", "feat: release <3 v3.9.0 (zero tech-debt, packaging fixed, full test matrix)"],
            cwd=target,
        )
        if args.push:
            _run(["git", "push", "-u", "origin", "HEAD"], cwd=target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
