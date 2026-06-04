#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/cloudus/projektek/tojasar-dashboard"
cd "$PROJECT_ROOT"

/usr/bin/python3 scraper/scrape.py

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "git repository not initialized; skipping dashboard/data.json push"
  exit 0
fi

git add dashboard/data.json
if git diff --cached --quiet -- dashboard/data.json; then
  echo "dashboard/data.json unchanged; skipping commit/push"
  exit 0
fi

today="$(date +%F)"
git commit -m "data: daily refresh ${today}"
git push
