#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/cloudus/projektek/tojasar-dashboard"
ERR_LOG="$PROJECT_ROOT/data/scraper.err.log"
TELEGRAM_ENV="${TELEGRAM_ENV:-/Users/cloudus/.claude/channels/telegram/.env}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-8578193341}"

# Hol tartott az err.log a futas ELEJEN. A riasztas csak az AZOTA keletkezett
# sorokat mutatja (RED1 M-1, 2026-09-18): a naplo append-only, sosem forog, es
# nincs benne idobelyeg — a `tail -n 10` emiatt mutathatott 100%-ban tobb napos
# tartalmat, es a "+N retry-sor" szam elettartam-kumulativ lett volna.
ERR_LOG_START_BYTES=0
[[ -f "$ERR_LOG" ]] && ERR_LOG_START_BYTES="$(wc -c < "$ERR_LOG" | tr -d ' ')"

# 1, ha a futas IDOKORLAT NELKUL ment (hianyzott a binaris).
TIMEOUT_MISSING=0

# send_alert CSAK kuld — NEM lep ki. (RED1 N-1, 2026-09-14: az elozo valtozat
# `exit`-tel zart, ezert a riasztas megette a publikalast: egyetlen lemarado
# sorozat miatt a dashboard veglegesen befagyott volna.)
send_alert() {
  local exit_code="${1:-1}"
  set +e

  local when err_tail text retry_lines
  when="$(date '+%F %H:%M')"
  if [[ -f "$ERR_LOG" ]]; then
    # A retry-sorokat KISZURJUK a riasztasbol (RED1 M-1, 2026-09-18): merve, hogy
    # egy stale-riasztasnal a tail 10 sorabol 9 lehet retry-zaj, es az erdemi
    # kontextus kiszorul. A dontő sor mindig megmarad (a ciklus UTAN irodik), de
    # a kore adott sorok is kellenek. A retry TENYET nem nyeljuk el: a szamat
    # odairjuk — egy 50-es szam onmagaban diagnozis.
    local now_bytes from_byte
    now_bytes="$(wc -c < "$ERR_LOG" | tr -d ' ')"
    # Ha a naplo kozben KISEBB lett (kezi torles/rotacio), az egeszet olvassuk:
    # a hianyzo kontextus rosszabb, mint a regi.
    if (( now_bytes < ERR_LOG_START_BYTES )); then from_byte=1; else from_byte=$((ERR_LOG_START_BYTES + 1)); fi
    retry_lines="$(tail -c "+$from_byte" "$ERR_LOG" | grep -c '^warn: retry: ' || true)"
    err_tail="$(tail -c "+$from_byte" "$ERR_LOG" | grep -v '^warn: retry: ' | tail -n 10)"
    if [[ "${retry_lines:-0}" -gt 0 ]]; then
      err_tail="$err_tail"$'\n'"(+${retry_lines} retry-sor kihagyva a naplóból)"
    fi
  else
    err_tail="(err.log nem található: $ERR_LOG)"
  fi

  text=$'⚠️ Tojásár-scraper HIBA '"$when"$'\nexit code: '"$exit_code"$'\n\nerr.log tail:\n'"$err_tail"

  # A hianyzo idokorlat-binaris KONFIGURACIO-DRIFT, nem tranziens hiba: a gat
  # nemán kikapcsolva marad. A naplo errol nem eleg (RED1 M-2) — a rendszernek
  # van egy sajat hibaosztalya arra, amikor "a kapu naplozza, hogy vedene, de
  # nem ved". Ezert a RIASZTAS SZOVEGEBE is bekerul.
  if (( TIMEOUT_MISSING )); then
    text="$text"$'\n\n⚠️ A GAT NEM VEDETT: az idokorlat-binaris hianyzik ('"$TIMEOUT_BIN"$') — ez a futas idokorlat NELKUL ment. Konfiguracio-drift, nem mulo hiba.'
  fi

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
      # IGAZOLT kezbesites. RED1 N-3: a scrape.py CSAK ebbol tudja meg, hogy a
      # riasztas tenyleg megerkezett — enelkul az elozo valtozat a kuldes
      # MEGKISERLESEKOR mar "jelentett"-re allitotta a sorozatot, es egy bukott
      # kuldes VEGLEG elnyelte a jelzest.
      : > "${TOJASAR_ALERT_MARKER:-$PROJECT_ROOT/data/.alert-sent}"
    else
      echo "watchdog: Telegram failure alert send failed (HTTP ${http_code:-none})" >&2
    fi
  else
    echo "watchdog: TELEGRAM_BOT_TOKEN missing; cannot send failure alert" >&2
  fi

  # "megkiseretlem" marker — ez MINDIG letrejon. A tesztek erre allitanak, ha
  # azt kerdezik, riasztott-e egyaltalan; a fenti marker azt jelenti, KIMENT.
  # A marker a kuldott SZOVEGET is tartalmazza (2026-09-18): igy utolag latszik,
  # mit tartalmazott a riasztas — es igy merheto, hogy a retry-zaj tenyleg
  # kimarad belole. Enelkul a szures nem lenne bizonyithato, csak remelheto.
  printf '%s\n' "$text" > "${TOJASAR_ALERT_ATTEMPT_MARKER:-$PROJECT_ROOT/data/.alert-attempted}"
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
# ---------------------------------------------------------------------------
# KULSO IDOKORLAT (RED1 H-2, 2026-09-18).
# A retry-ablak bovitese utan a legrosszabb eset ~1,5 ora: ha egy forras nem
# hibazik, hanem HALLGAT, akkor 6 kiserlet x 30 mp timeout + ~59 mp rahagyas =
# 239 mp EGY hivasra, 13 forras x 2 hivas. Eddig SEMMI nem vagta el (nincs
# ExitTimeOut a plistben, nincs timeout-minutes a CI-ben) — es a riasztas is
# csak a scrape.py visszaterese UTAN fut, tehat addig a rendszer NEMA.
#
# TELJES UT kell: a `timeout` a /opt/homebrew/bin-ben van, a launchd
# alapertelmezett PATH-jan (/usr/bin:/bin:/usr/sbin:/sbin) NINCS RAJTA — merve,
# negativ kontrollal. A `-x` guard miatt hianyzo binarisnal a futas
# idokorlat NELKUL megy tovabb (fail-open): egy hianyzo mereszkoz nem
# akaszthatja meg a napi adatgyujtest, de KIMONDJA magat a naploban.
# ---------------------------------------------------------------------------
# `-` es NEM `:-` : az URES ertek is ervenyes valasz ("nincs mivel merni"), es
# nem eshet vissza nemán a produkcios utra. A `:-` az uresre is a defaultot adja,
# amitol a "nincs binaris" eset a fejleszto gepen zoldnek latszott volna. (RED1 H-2.)
TIMEOUT_BIN="${TOJASAR_TIMEOUT_BIN-/opt/homebrew/bin/timeout}"
TIMEOUT_SECONDS="${TOJASAR_TIMEOUT_SECONDS:-900}"

scrape_rc=0
if [[ -x "$TIMEOUT_BIN" ]]; then
  # -k 30: ha a SIGTERM-re nem all le, 30 mp mulva KILL. Ma nincs signal-kezelo,
  # tehat a SIGTERM eleg — de egy jovobeli `finally` mellett a gat orokre varna.
  "$TIMEOUT_BIN" -k 30 "$TIMEOUT_SECONDS" /usr/bin/python3 scraper/scrape.py || scrape_rc=$?
  if (( scrape_rc == 124 )); then
    echo "scraper: IDOTULLEPES (${TIMEOUT_SECONDS}s) - a futas felbeszakadt, riasztas kovetkezik" >&2
  fi
else
  TIMEOUT_MISSING=1
  echo "warn: idokorlat-binaris nem talalhato ($TIMEOUT_BIN) - a scraper IDOKORLAT NELKUL fut" >&2
  /usr/bin/python3 scraper/scrape.py || scrape_rc=$?
fi

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

# A hianyzo gat AKKOR IS szoljon, ha a futas egyebkent sikeres volt — kulonben a
# vedelem-vesztes pont a jo napokon marad nema. De csak EGYSZER, amig a helyzet
# fennall: esemeny, nem allapot. (A marker torlodik, amint a binaris visszater.)
MISSING_MARKER="${TOJASAR_TIMEOUT_MISSING_MARKER:-$PROJECT_ROOT/data/.timeout-bin-missing}"
if (( TIMEOUT_MISSING )); then
  if [[ ! -f "$MISSING_MARKER" ]] && (( scrape_rc == 0 || scrape_rc == 2 )); then
    send_alert "$scrape_rc"
    : > "$MISSING_MARKER"
  fi
else
  rm -f "$MISSING_MARKER"
fi

exit "$scrape_rc"
