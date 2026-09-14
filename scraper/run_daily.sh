#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/cloudus/projektek/tojasar-dashboard"
ERR_LOG="$PROJECT_ROOT/data/scraper.err.log"
TELEGRAM_ENV="${TELEGRAM_ENV:-/Users/cloudus/.claude/channels/telegram/.env}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-8578193341}"

notify_failure() {
  local exit_code="${1:-1}"
  set +e

  local when err_tail text
  when="$(date '+%F %H:%M')"
  if [[ -f "$ERR_LOG" ]]; then
    err_tail="$(tail -n 10 "$ERR_LOG")"
  else
    err_tail="(err.log nem található: $ERR_LOG)"
  fi

  text=$'⚠️ Tojásár-scraper HIBA '"$when"$'\nexit code: '"$exit_code"$'\n\nerr.log tail:\n'"$err_tail"

  if [[ -r "$TELEGRAM_ENV" ]]; then
    source "$TELEGRAM_ENV"
  fi

  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    local response_file http_code
    response_file="/tmp/tojasar-watchdog-telegram-response.json"
    http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${text}" || true)"
    if [[ "$http_code" == "200" ]]; then
      echo "watchdog: Telegram failure alert sent (HTTP 200)" >&2
    else
      echo "watchdog: Telegram failure alert send failed (HTTP ${http_code:-none})" >&2
    fi
  else
    echo "watchdog: TELEGRAM_BOT_TOKEN missing; cannot send failure alert" >&2
  fi

  exit "$exit_code"
}

trap 'notify_failure $?' ERR

cd "$PROJECT_ROOT"

if [[ "${TOJASAR_FORCE_FAIL:-0}" == "1" ]]; then
  echo "simulated failure for watchdog test" >&2
  false
fi

# A scraper kilepesi kodja jelentest hordoz (lasd scrape.py EXIT_* konstansok):
#   0 = minden forras rendben
#   2 = degradalt: volt forraskieses, DE az adat mentve es exportalva -> NEM riasztunk
#   barmi mas = valodi baj -> riasztas Tominak
# FONTOS (RED1 F-1, 2026-09-14): a `set +e` zsh-ben NEM kapcsolja ki a
# `trap ... ERR`-t — az elso valtozatom ezt hitte, es emiatt a 2-es kod MEGIS
# riasztast valtott, sot a notify_failure `exit`-tel zart, igy a lenti git-blokk
# le sem futott (a dashboard befagyva maradt). A `|| ...` alak viszont a
# parancsot a trap alol is kiveszi. A trap marad a helyen: a git-blokk hibait
# tovabbra is jelentenie kell.
scrape_rc=0
/usr/bin/python3 scraper/scrape.py || scrape_rc=$?

if (( scrape_rc == 2 )); then
  echo "scraper: degradalt futas (exit 2) - az adat mentve es exportalva, riasztas nelkul" >&2
elif (( scrape_rc != 0 )); then
  notify_failure "$scrape_rc"
fi

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
