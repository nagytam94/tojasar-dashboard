#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/cloudus/projektek/tojasar-dashboard"
ERR_LOG="$PROJECT_ROOT/data/scraper.err.log"
TELEGRAM_ENV="${TELEGRAM_ENV:-/Users/cloudus/.claude/channels/telegram/.env}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-8578193341}"

# send_alert CSAK kuld — NEM lep ki. (RED1 N-1, 2026-09-14: az elozo valtozat
# `exit`-tel zart, ezert a riasztas megette a publikalast: egyetlen lemarado
# sorozat miatt a dashboard veglegesen befagyott volna.)
send_alert() {
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

  # marker: a tesztek erre allitanak, nem a naploszovegre (RED1 javaslata)
  : > "${TOJASAR_ALERT_MARKER:-$PROJECT_ROOT/data/.alert-sent}"
  set -e
}

# A trap-nek KELL kilepnie — ott valodi, varatlan hiba tortent.
notify_failure() {
  send_alert "${1:-1}"
  exit "${1:-1}"
}

trap 'notify_failure $?' ERR

# A dashboard publikalasa. Fuggveny, hogy `return`-nel zarhasson: korabban
# `exit`-ek voltak benne, es igy nem lehetett volna a hiba-dontes ELE tenni.
publish_dashboard() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "git repository not initialized; skipping dashboard/data.json push"
    return 0
  fi
  git add dashboard/data.json
  if git diff --cached --quiet -- dashboard/data.json; then
    echo "dashboard/data.json unchanged; skipping commit/push"
    return 0
  fi
  local today
  today="$(date +%F)"
  git commit -m "data: daily refresh ${today}"
  git push
}

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

# ---------------------------------------------------------------------------
# A SORREND A LENYEG (RED1 N-1, 2026-09-14).
# Eloszor PUBLIKALUNK, es csak azutan dontunk a riasztasrol. Haromszor buktunk
# el ugyanezen a formán: a hiba-dontes megelozte a hatast.
#   1. scrape.py — a hibakapu a MENTES elott allt
#   2. scrape.py — a stale-kapu a dontes utan allt
#   3. run_daily.sh — a riasztas a PUBLIKALAS elott allt   <- ez itt
# Egy lemarado sorozat nem akadalyozhatja meg a masik 49 friss adatanak
# kikeruleset.
# ---------------------------------------------------------------------------
publish_dashboard

if (( scrape_rc == 2 )); then
  echo "scraper: degradalt futas (exit 2) - az adat mentve es publikalva, riasztas nelkul" >&2
elif (( scrape_rc != 0 )); then
  send_alert "$scrape_rc"
fi

exit "$scrape_rc"
