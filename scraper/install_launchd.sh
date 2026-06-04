#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/cloudus/projektek/tojasar-dashboard"
PLIST_NAME="com.cloudus.tojasar-dashboard-scraper.plist"
SOURCE_PLIST="$PROJECT_ROOT/scraper/$PLIST_NAME"
TARGET_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SOURCE_PLIST" "$TARGET_PLIST"
launchctl bootout "gui/$(id -u)" "$TARGET_PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET_PLIST"
launchctl print "gui/$(id -u)/com.cloudus.tojasar-dashboard-scraper"
