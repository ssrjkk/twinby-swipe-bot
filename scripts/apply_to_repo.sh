#!/usr/bin/env bash
set -euo pipefail

# Безопасное внедрение <3 v3.9.0 в локальный клон https://github.com/ssrjkk/twinby-swipe-bot
# Использование:
#   ./scripts/apply_to_repo.sh /path/to/twinby-swipe-bot [--commit] [--push]

if [ $# -lt 1 ]; then
  echo "Использование: $0 /path/to/twinby-swipe-bot [--commit] [--push]"
  exit 1
fi

TARGET_REPO="$1"
shift || true
DO_COMMIT=false
DO_PUSH=false
for arg in "$@"; do
  [ "$arg" = "--commit" ] && DO_COMMIT=true
  [ "$arg" = "--push" ] && DO_PUSH=true
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -d "$TARGET_REPO/.git" ]; then
  echo "Ошибка: $TARGET_REPO не является git-репозиторием."
  printf 'Выполните: git clone https://github.com/ssrjkk/twinby-swipe-bot.git "%s"
' "$TARGET_REPO"
  exit 1
fi

echo "Копирую файлы <3 v3.9.0 в $TARGET_REPO (сохраняя .git/, .env, data/)..."
rsync -av --delete   --exclude '.git/'   --exclude '.env'   --exclude 'data/'   --exclude '.venv/'   --exclude 'venv/'   --exclude '__pycache__/'   --exclude '*.pyc'   "$SRC_ROOT/" "$TARGET_REPO/"

cd "$TARGET_REPO"
git status --short

if [ "$DO_COMMIT" = true ]; then
  git add -A
  git commit -m "feat: release <3 v3.9.0 (full verification, all test layers, agent integration guide)"
  if [ "$DO_PUSH" = true ]; then
    git push origin HEAD
  else
    echo "Коммит создан! Выполните: git push origin main"
  fi
fi
