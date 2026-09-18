#!/usr/bin/env python3
"""Regresszios tesztek a tojasar-scraper 2026-09-14-i javitasaira.

A KOZOS HIBAFORMA, amit ez a teszt oriz:
    "a hiba-dontes megelozi a hatast"
Haromszor buktunk el rajta ugyanazon a napon:
  1. scrape.py    — a hibakapu a MENTES elott allt   -> [A1]
  2. scrape.py    — a stale-kapu a dontes utan allt  -> [B1]
  3. run_daily.sh — a riasztas a PUBLIKALAS elott    -> [C]
Plusz a nema valtozata: a hibaelnyeles sikernek latszott -> [A5]

A teszt NEM fugg sem a halozattol, sem az elo DB-tol: sajat DB-t epit, es a
scrape_source-t monkeypatcheli. Igy friss klonon is fut.

Futtatas:  /usr/bin/python3 scraper/test_partial_failure.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_DIR = PROJECT_ROOT / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import scrape  # noqa: E402
import store  # noqa: E402
from sources import get_sources  # noqa: E402

# A `timeout` helye kornyezetenkent mas, a MECHANIZMUS viszont ugyanaz. Ezert a
# teszt a tenylegesen elerheto binarissal mer, a PRODUKCIOS utvonal helyesseget
# pedig kulon allitas orzi (csak Darwinon — a launchd ott hivja a run_daily.sh-t,
# es az alap-PATH-jan nincs `timeout`).
#
# 2026-09-18, KET egymasra epulo hiba ugyanezen a ponton:
#   1. a teszt a homebrew-utat varta el -> a CI-ben 3 allitas elbukott;
#   2. a javitas kommentje azt allitotta, hogy "a CI Linuxon fut" — ez MERES
#      NELKULI feltetelezes volt. A `tests.yml` SZANDEKOSAN `macos-latest`
#      (a zsh-szemantika miatt), es a macOS egyaltalan nem szallit `timeout`-ot,
#      ezert a CI-futas 3 helyett 5 bukast adott.
# A megoldas nem itt van, hanem a workflow-ban: `brew install coreutils`. Ez a
# ket sor csak azt biztositja, hogy a mechanizmus barhol merheto legyen.
TIMEOUT_BIN = shutil.which("timeout") or shutil.which("gtimeout") or ""
PROD_TIMEOUT_BIN = Path("/opt/homebrew/bin/timeout")

TODAY = date.today()
SOURCE_KEYS = [s.key for s in get_sources(None)]
FAILING_KEY = SOURCE_KEYS[-1]

results: list[tuple[str, bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append((name, ok, f"kapott={got!r} vart={want!r}"))
    print(f"  {'OK  ' if ok else 'BUKIK'}  {name}: kapott={got!r} vart={want!r}")


def observation(key: str, day: date, price: float | None = 123.4) -> dict:
    iso = day.isocalendar()
    return {
        "key": key, "label": f"teszt {key}", "country": "NL",
        "category": "kelteto", "size": None, "color": None, "unit": "EUR/100",
        "week_iso": f"{iso[0]}-W{iso[1]:02d}", "observed_date": day.isoformat(),
        "price": price, "change": None,
        "fetched_at": f"{day.isoformat()}T09:00:00+00:00",
        "source_url": "https://example.invalid/teszt",
    }


def seed(db: Path, days_back: list[int]) -> None:
    """Sajat DB — nem az elo adatbazis masolata (RED1 F-5)."""
    rows = [observation(key, TODAY - timedelta(days=d))
            for d in days_back for key in SOURCE_KEYS]
    store.store_observations(rows, db)


def count_rows(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    finally:
        conn.close()


def rows_on(db: Path, day: date) -> int:
    """Hany megfigyeles all a DB-ben ERRE a napra.

    Ezt merjuk a sorok szamanak NOVEKEDESE helyett (2026-09-18). Az upsert
    kulcsa `UNIQUE(series_id, week_iso)` — vagyis a HET. Ha a magvetett pont
    es a mai adat egy ISO-hetbe esik, a mentes FELULIR, nem beszur: a delta
    akkor is 0, ha minden rendben ment. Ket iranyban hazudott:
      · hamis PIROS — "nem mentodott semmi" (csutortok-vasarnap, 4 nap a 7-bol)
      · hamis ZOLD  — a "semmi nem ment be" allitas akkor is teljesult volna,
                      ha a rossz adat CSENDBEN felulirja a regit
    A "bent van-e a mai sor" kerdes naptartol fuggetlen, es erosebb allitas.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM observation WHERE observed_date = ?", (day.isoformat(),)
        ).fetchone()[0]
    finally:
        conn.close()


def stored_prices(db: Path, day: date) -> set:
    """Milyen ARAK allnak a DB-ben erre a napra.

    Azert kell, mert a keszlet eddig KIZAROLAG sorokat szamolt: egy "minden ar
    x100" tipusu mertekegyseg-hiba 63/63 zolden atment rajta (RED1 M-4 mutacio,
    2026-09-18). A sor megletenel egy fokkal tobb kerdes, hogy JO-E, ami bement.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT DISTINCT price FROM observation WHERE observed_date = ?",
                (day.isoformat(),),
            )
        }
    finally:
        conn.close()


def run_main(db: Path, out: Path, patch):
    before = count_rows(db) if db.exists() else 0
    scrape.scrape_source = patch
    code = scrape.main(["--db", str(db), "--out", str(out)])
    return code, before, count_rows(db)


# --- forras-viselkedesek -----------------------------------------------------
def one_fails(session, source):
    if source.key == FAILING_KEY:
        raise RuntimeError("szimulalt forraskieses")
    return [observation(source.key, TODAY)]


def all_fail(session, source):
    raise RuntimeError("szimulalt teljes kieses")


def all_ok(session, source):
    return [observation(source.key, TODAY)]


def all_frozen(session, source):
    """HTTP 200, szep parse — de a forras adata befagyott (RED1 F-2)."""
    return [observation(source.key, TODAY - timedelta(days=90))]


def one_bad_row(session, source):
    if source.key == FAILING_KEY:
        return [observation(source.key, TODAY, price=None)]
    return [observation(source.key, TODAY)]


def every_row_bad(session, source):
    """Minden forras valaszol, de EGYETLEN sor sem tarolhato (RED1 N-2)."""
    return [observation(source.key, TODAY, price=None)]


# --- shell-harness -----------------------------------------------------------
def shell_case(tmp: Path, exit_code: int, change_data: bool, with_remote: bool = True,
               sleep_seconds: float = 0, extra_env: dict | None = None, tag: str = "",
               err_log_seed: list[str] | None = None, runtime_err_lines: list[str] | None = None):
    """A VALODI run_daily.sh, /bin/zsh alatt (ahogy a launchd hivja).

    `with_remote=False` -> a `git push` bukik: ezzel meressuk, hogy a trap
    TOVABBRA is jelenti a git-hibakat (pozitiv kontroll).
    """
    work = tmp / f"shell{exit_code}{'c' if change_data else ''}{'r' if with_remote else 'n'}{tag}"
    (work / "scraper").mkdir(parents=True)
    (work / "dashboard").mkdir()
    (work / "data").mkdir()
    script = (SCRAPER_DIR / "run_daily.sh").read_text(encoding="utf-8")
    script = "\n".join(
        f'PROJECT_ROOT="{work}"' if line.startswith("PROJECT_ROOT=") else line
        for line in script.splitlines()
    )
    (work / "scraper" / "run_daily.sh").write_text(script, encoding="utf-8")
    body = "import sys\n"
    if runtime_err_lines:
        # Elesben a launchd iranyitja a stderr-t az err.log-ba, tehat a futas
        # KOZBEN keletkezo sorok odakerulnek. A harness ezt utanozza: a hamis
        # scraper maga fuzi hozza oket. A kulonbseg lenyeges — a riasztas csak a
        # MOSTANI futas sorait mutatja, es ezt csak igy lehet merni.
        body += ("import pathlib\n"
                 "_p = pathlib.Path('data/scraper.err.log')\n"
                 "_p.write_text((_p.read_text() if _p.exists() else '') + "
                 f"{'chr(10)'}.join({runtime_err_lines!r}) + chr(10))\n")
    if sleep_seconds:
        # A hallgato forras szimulacioja: a scraper EL, csak nem ter vissza.
        body += f"import time\ntime.sleep({sleep_seconds})\n"
    if change_data:
        body += ('import pathlib, json\n'
                 'pathlib.Path("dashboard/data.json").write_text(json.dumps({"x":2}))\n')
    body += f"sys.exit({exit_code})\n"
    (work / "scraper" / "scrape.py").write_text(body, encoding="utf-8")
    (work / "dashboard" / "data.json").write_text('{"x":1}\n', encoding="utf-8")
    if err_log_seed is not None:
        (work / "data" / "scraper.err.log").write_text(
            "\n".join(err_log_seed) + "\n", encoding="utf-8")
    marker = work / "data" / ".alert-attempted"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(work),
        "TELEGRAM_ENV": "/nonexistent/telegram.env",   # sosem kuld valodi uzenetet
        "TOJASAR_ALERT_ATTEMPT_MARKER": str(marker),
        "TOJASAR_ALERT_MARKER": str(work / "data" / ".alert-sent"),
    }
    env.update(extra_env or {})
    setup = [["git", "init", "-q", "."], ["git", "add", "-A"],
             ["git", "-c", "user.name=t", "-c", "user.email=t@t",
              "commit", "-qm", "init"]]
    if with_remote:
        bare = work.parent / f"{work.name}-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                       env=env, check=True, capture_output=True)
        setup += [["git", "remote", "add", "origin", str(bare)],
                  ["git", "push", "-q", "-u", "origin", "HEAD"]]
    for cmd in setup:
        subprocess.run(cmd, cwd=work, env=env, check=True, capture_output=True)
    proc = subprocess.run(["/bin/zsh", "scraper/run_daily.sh"],
                          cwd=work, env=env, capture_output=True, text=True)
    commits = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=work,
                             env=env, capture_output=True, text=True).stdout.strip()
    return {
        "rc": proc.returncode,
        "riasztott": marker.exists(),
        "riasztas_szovege": marker.read_text(encoding="utf-8") if marker.exists() else "",
        "kezbesitve": (work / "data" / ".alert-sent").exists(),
        "commitok": int(commits),
        "log": proc.stdout + proc.stderr,
    }


# --- retry-harness -----------------------------------------------------------
class _Counting(BaseHTTPRequestHandler):
    """Mindig 500-at ad — kiveve, ha `fail_first` utan gyogyulnia kell."""

    hits = 0
    stamps: list[float] = []
    fail_first: int | None = None

    def do_GET(self):  # noqa: N802
        cls = type(self)
        cls.hits += 1
        cls.stamps.append(time.monotonic())
        healed = cls.fail_first is not None and cls.hits > cls.fail_first
        self.send_response(200 if healed else 500)
        self.end_headers()
        self.wfile.write(b"ok" if healed else b"nope")

    def log_message(self, *a):  # csend
        pass


def retry_probe(*, fail_first: int | None = None, **session_kwargs) -> dict:
    """Egy lokalis szerverre kuld EGY kerest, es megmeri, mi tortent valojaban.

    Vissza: hany kiserlet erkezett be, mekkorak voltak a szunetek kozottuk,
    mi lett a vegso statusz, es mit irt a scraper a stderr-re. A szunetek azert
    kellenek, mert a "novekvo rahagyas" maskepp nem allitas, csak remeny.
    """
    _Counting.hits = 0
    _Counting.stamps = []
    _Counting.fail_first = fail_first
    srv = HTTPServer(("127.0.0.1", 0), _Counting)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    err = io.StringIO()
    status: int | None = None
    try:
        session = scrape.build_session(**session_kwargs)
        with contextlib.redirect_stderr(err):
            try:
                status = session.get(f"http://127.0.0.1:{srv.server_port}/x", timeout=5).status_code
            except Exception:
                status = None
        stamps = list(_Counting.stamps)
        return {
            "hits": _Counting.hits,
            "gaps": [round(b - a, 2) for a, b in zip(stamps, stamps[1:])],
            "status": status,
            "stderr": err.getvalue(),
        }
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def live_backoff_window() -> tuple:
    """Az ELES rahagyas-sorozat es a teljes ablak — ALVAS NELKUL, 0 mp alatt.

    A commit egesz indoka az volt, hogy az ablak 6,7 mp-rol ~1 percre no. Ezt
    eddig EGYETLEN allitas sem orizte (RED1 M-2): a konfig-meres csak a szorzot
    nezte, a viselkedes-szonda pedig IDEGEN ertekekkel fut (0.1-es faktorral,
    hogy gyors legyen). Pont a lenyeg csuszott at kettejuk kozott.

    A Retry sajat `get_backoff_time()`-jat kerdezzuk meg szintetikus elozmennyel,
    igy a valodi ertekek merhetok anelkul, hogy varnank rajuk.
    """
    from urllib3.util.retry import RequestHistory

    retry = scrape.build_session().get_adapter("https://x/").max_retries
    base = retry.new(backoff_jitter=0.0)  # a veletlen szoras a mercet zajossa tenne
    waits = []
    for n in range(1, (retry.total or 0) + 1):
        hist = tuple(RequestHistory("GET", "https://x/", None, 500, None) for _ in range(n))
        waits.append(round(base.new(history=hist).get_backoff_time(), 1))
    return waits, round(sum(waits), 1)


def live_retry_config() -> dict:
    """Amit az ELES session tenylegesen visel — nem amit a konstans mond.

    Kulon meres, mert a ket dolog elcsuszhat egymastol: a konstans maradhat
    helyes akkor is, ha a build_session mar nem hasznalja.
    """
    retry = scrape.build_session().get_adapter("https://x/").max_retries
    return {
        "total": retry.total,
        "backoff_factor": retry.backoff_factor,
        "backoff_max": retry.backoff_max,
        "backoff_jitter": retry.backoff_jitter,
        "connect": retry.connect,
        "status_forcelist": tuple(sorted(retry.status_forcelist or ())),
        "post_is_retried": "POST" in (retry.allowed_methods or ()),
    }


# --- UI-harness --------------------------------------------------------------
def render_freshness_cases(tmp: Path) -> dict:
    html = (PROJECT_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    start = html.index("function renderFreshness(")
    end = html.index("function curCat(")
    fn = html[start:end]
    stale_payload = {
        "categories": [{"series": [{"key": "a", "label": "A",
                                    "updated_through": "2026-07-01",
                                    "days_since_update": 60}]}],
        "freshness": {"stale_after_days": 21, "series_stale": [
            {"key": "a", "label": "A", "days_since_update": 60}]},
    }
    cases = {
        "v04_nincs_freshness": {"categories": [{"series": [{"key": "a"}]}]},
        "ures": {"categories": []},
        "null_datum": {"categories": [{"series": [{"key": "a",
                                                   "updated_through": None}]}],
                       "freshness": {"series_stale": []}},
        "elavult": stale_payload,
    }
    js = tmp / "rf.js"
    js.write_text(
        "const S={};"
        "function $(sel){const k=sel.slice(1);"
        "  if(!S[k])S[k]={textContent:'',style:{display:''}};return S[k];}\n"
        + fn +
        "const cases=" + json.dumps(cases) + ";\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)){\n"
        "  S['freshwarn']={textContent:'',style:{display:''}};\n"
        "  S['lastpoint']={textContent:''};\n"
        "  try{ renderFreshness(cases[k]); out[k]={ok:true,"
        "    display:S['freshwarn'].style.display, txt:S['freshwarn'].textContent};}\n"
        "  catch(e){ out[k]={ok:false, err:String(e)};}\n"
        "}\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(js)], capture_output=True, text=True)
    if proc.returncode != 0:
        return {"_hiba": proc.stderr.strip()[:200]}
    return json.loads(proc.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tojasar-teszt-") as tmpdir:
        tmp = Path(tmpdir)
        # Heti racson (7 tobbszorosei): igy a magvetett pontok garantaltan MAS
        # ISO-hetbe esnek, mint a mai adat, es a szamolt ritmus is pontosan 7
        # napos — a het napjatol fuggetlenul. A regi [10, 3] ertekpar a sajat
        # naptari helyzetetol fuggott: csutortoktol vasarnapig a 3 napos pont
        # a mai hetbe esett, felulirodott, es a ritmus 10 naposra ugrott.
        recent = [14, 7]

        print("\n[A1] EGY forras bukik, a tobbi jo -> a jo adat NEM veszhet el")
        db, out = tmp / "a1.db", tmp / "a1.json"
        seed(db, recent)
        code, before, after = run_main(db, out, one_fails)
        check("kilepesi kod = EXIT_DEGRADED", code, scrape.EXIT_DEGRADED)
        check("pontosan a 12 jo forras mai sora all a DB-ben", rows_on(db, TODAY),
              len(SOURCE_KEYS) - 1)
        # A `rows_on` a mai sorra kerdez — de attol meg a REGI adat elveszhetne.
        # Ezt a dimenziot a regi delta-meres orizte; nem szabad elhagyni (RED1 M-3).
        check("a meglevo adat nem semmisult meg", after >= before, True)

        check("data.json ujragenaralodott", out.exists(), True)

        print("\n[A2] MINDEN forras bukik -> riaszt, data.json NEM kap hamis datumot")
        db, out = tmp / "a2.db", tmp / "a2.json"
        seed(db, recent)
        code, _, _ = run_main(db, out, all_fail)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)
        check("semmi nem mentodott", rows_on(db, TODAY), 0)
        check("data.json NEM irodott ki", out.exists(), False)

        print("\n[A3] MINDEN forras jo -> tiszta siker")
        db, out = tmp / "a3.db", tmp / "a3.json"
        seed(db, recent)
        code, before, after = run_main(db, out, all_ok)
        check("kilepesi kod = EXIT_OK", code, scrape.EXIT_OK)
        check("mind a 13 forras mai sora bement", rows_on(db, TODAY), len(SOURCE_KEYS))
        check("a meglevo adat nem semmisult meg", after >= before, True)
        # Nem eleg, hogy BEMENT — az is kerdes, hogy JO-E. Egy "minden ar x100"
        # mertekegyseg-hiba eddig 63/63 zolden atment (RED1 M-4).
        check("a tarolt ar egyezik a felkinalttal", stored_prices(db, TODAY), {123.4})

        print("\n[A4] EGY rossz sor nem dobhatja el a tobbi forras koteget")
        db, out = tmp / "a4.db", tmp / "a4.json"
        seed(db, recent)
        code, _, _ = run_main(db, out, one_bad_row)
        check("a 12 jo forras mai adata bekerult", rows_on(db, TODAY), len(SOURCE_KEYS) - 1)
        check("degradaltnak jelzi (nem nema siker)", code, scrape.EXIT_DEGRADED)

        print("\n[A5] MINDEN sor elutasitva -> RIASZT (nem nema siker) [RED1 N-2]")
        db, out = tmp / "a5.db", tmp / "a5.json"
        seed(db, recent)
        code, before, after = run_main(db, out, every_row_bad)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)
        check("tenyleg semmi nem ment be", rows_on(db, TODAY), 0)
        check("a meglevo adat nem semmisult meg", after >= before, True)


        print("\n[A6] A kihagyottak TOBBSEGBEN -> RIASZT, nem csak degradalt [RED1 N-5]")
        db, out = tmp / "a6.db", tmp / "a6.json"
        seed(db, recent)

        def mostly_bad(session, source):
            good = observation(source.key, TODAY)
            bad = [observation(source.key, TODAY - timedelta(days=i), price=None)
                   for i in range(1, 4)]
            return [good] + bad

        code, _, _ = run_main(db, out, mostly_bad)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)

        print("\n[B1] BEFAGYOTT forras (mind 200, regi adat) -> RIASZT elsore")
        db, out = tmp / "b1.db", tmp / "b1.json"
        seed(db, [120, 100, 90])
        code, _, _ = run_main(db, out, all_frozen)
        check("kilepesi kod = EXIT_ALERT (nem 0!)", code, scrape.EXIT_ALERT)

        print("\n[B2] Kezbesites NEM igazolt -> UJRA riaszt (nem nyeli el) [RED1 N-3]")
        code, _, _ = run_main(db, out, all_frozen)
        check("masodszor is EXIT_ALERT, mert nem jott nyugta", code, scrape.EXIT_ALERT)
        check("az allapotfajl letrejott",
              (db.parent / "alert-state.json").exists(), True)
        state = json.loads((db.parent / "alert-state.json").read_text(encoding="utf-8"))
        check("a jeloltek FUGGOBEN vannak, nem jelentettkent",
              bool(state["pending_stale"]) and not state["reported_stale"], True)

        print("\n[B2b] IGAZOLT kezbesites utan -> mar NEM riaszt (esemeny, nem allapot)")
        (db.parent / ".alert-sent").touch()      # a shell ezt csak HTTP 200-nal irja
        code, _, _ = run_main(db, out, all_frozen)
        check("kilepesi kod = EXIT_DEGRADED", code, scrape.EXIT_DEGRADED)
        check("a nyugta-marker elfogyott", (db.parent / ".alert-sent").exists(), False)
        code, _, _ = run_main(db, out, all_frozen)
        check("es tovabbra is csendben marad", code, scrape.EXIT_DEGRADED)

        print("\n[B2c] A visszaallt sorozat kikerul -> kesobb ujra tud szolni")
        code, _, _ = run_main(db, out, all_ok)
        state = json.loads((db.parent / "alert-state.json").read_text(encoding="utf-8"))
        check("a nyilvantartas kiurult", state["reported_stale"] + state["pending_stale"], [])

        print("\n[B3] Friss adat -> nincs fals elavultsag, es a kuszob SOROZATONKENTI")
        db, out = tmp / "b3.db", tmp / "b3.json"
        seed(db, recent)
        code, _, _ = run_main(db, out, all_ok)
        rows = store.series_freshness(db)
        check("nincs elavult sorozat", sum(1 for r in rows if r["stale"]), 0)
        check("a kuszob sorozatonkent szamolodik (nem a globalis 21)",
              all(r["stale_after_days"] < store.STALE_AFTER_DAYS for r in rows), True)

        print("\n[B4] A kuszob NEM tanulja meg a sajat romlasat [RED1 N-4]")
        db4 = tmp / "b4.db"
        # egy REGI, mar meggyogyult 60 napos kieses + azota 30 het heti ritmus
        days = [400, 340]                       # <- a 60 napos res
        days += [7 * i for i in range(30, 0, -1)]
        rows = [observation("teszt_sorozat", TODAY - timedelta(days=d)) for d in days]
        store.store_observations(rows, db4)
        by_key = {r["key"]: r for r in store.series_freshness(db4)}
        kuszob = by_key["teszt_sorozat"]["stale_after_days"]
        check("a regi kieses kioregszik (nem fujja fel a kuszobot)",
              kuszob <= 20, True)
        check("es a felso korlat alatt marad",
              kuszob <= store.MAX_STALE_AFTER_DAYS, True)

        db5 = tmp / "b5.db"
        # lassan ritkulo forras: a kuszob nem szaladhat el a vegtelenbe
        acc, days5 = 0, []
        for gap in (7, 7, 7, 10, 14, 20, 28, 40, 55, 70):
            acc += gap
            days5.append(acc)
        base = max(days5)
        rows5 = [observation("ritkulo", TODAY - timedelta(days=base - d)) for d in days5]
        store.store_observations(rows5, db5)
        by_key5 = {r["key"]: r for r in store.series_freshness(db5)}
        check("a ritkulo forras kuszobe is korlatos",
              by_key5["ritkulo"]["stale_after_days"] <= store.MAX_STALE_AFTER_DAYS, True)

        print("\n[C] run_daily.sh /bin/zsh alatt: PUBLIKALAS a riasztas ELOTT [N-1]")
        r0 = shell_case(tmp, 0, change_data=True)
        check("exit 0 -> rc 0", r0["rc"], 0)
        check("exit 0 -> nem riaszt", r0["riasztott"], False)
        check("exit 0 -> publikalt (uj commit)", r0["commitok"], 2)

        r2 = shell_case(tmp, 2, change_data=True)
        check("exit 2 -> rc 2", r2["rc"], 2)
        check("exit 2 -> NEM riaszt", r2["riasztott"], False)
        check("exit 2 -> PUBLIKALT", r2["commitok"], 2)

        r1 = shell_case(tmp, 1, change_data=True)
        check("exit 1 -> rc 1", r1["rc"], 1)
        check("exit 1 -> RIASZT", r1["riasztott"], True)
        check("exit 1 -> MEGIS PUBLIKALT (ez volt a N-1 hiba)", r1["commitok"], 2)
        # RED1 N-3: a kezbesites-marker CSAK HTTP 200-nal keletkezhet. Itt nincs
        # token, tehat a kuldes BUKIK — ha a marker megis letrejonne, a scraper
        # azt hinne, hogy a riasztas megerkezett, es masnap elnemulna.
        check("exit 1 -> a kuldes bukott, NINCS kezbesites-nyugta",
              r1["kezbesitve"], False)
        check("exit 0 -> nyugta sincs (nem is riasztott)", r0["kezbesitve"], False)

        rp = shell_case(tmp, 0, change_data=True, with_remote=False)
        check("push-hiba -> TOVABBRA is riaszt (a trap a helyen van)",
              rp["riasztott"], True)
        check("push-hiba -> nem-nulla rc", rp["rc"] != 0, True)

        print("\n[D] A frissesseg ki van mondva az exportban")
        payload = json.loads((tmp / "a1.json").read_text(encoding="utf-8"))
        every = [s for c in payload["categories"] for s in c["series"]]
        check("minden sorozatnak van updated_through",
              all("updated_through" in s for s in every), True)
        check("minden sorozatnak van sajat kuszobe",
              all("stale_after_days" in s for s in every), True)
        check("freshness blokk letezik", "freshness" in payload, True)

        print("\n[E] Retry-reteg: 500-as valasz -> tobbszor probal [RED1 M1 res]")
        # FONTOS: a vart erteket NEM a kodbol vesszuk. Az elso valtozat
        # `scrape.RETRY_TOTAL + 1`-et irt — ezzel a konstans mutalasa egyutt
        # mozgatta a mercet is, es a "retry kikapcsolva" mutacio ATMENT a
        # teszten. A teszt csak akkor allitas, ha a vart ertek fuggetlen.
        cfg = live_retry_config()
        check("az ELES session 5 ujraprobalast visel (= 6 kiserlet)", cfg["total"], 5)
        check("novekvo rahagyas: a szorzo 2.0", cfg["backoff_factor"], 2.0)
        check("a rahagyas 30 masodpercnel megall", cfg["backoff_max"], 30.0)
        check("van veletlen szoras (nem egyszerre ter vissza mind)", cfg["backoff_jitter"] > 0, True)
        check("az 500 ujraprobalando", 500 in cfg["status_forcelist"], True)
        check("a POST is (a chart-lekeres az)", cfg["post_is_retried"], True)
        check("a 'nincs halo' eset kulon, SZUKEBB kereten bukik", cfg["connect"], 2)

        # Az eles ablak — a commit fo allitasa. Alvas nelkul merve (RED1 M-2).
        waits, ablak = live_backoff_window()
        check("az eles rahagyas-sorozat 0, 4, 8, 16, 30 mp", waits, [0.0, 4.0, 8.0, 16.0, 30.0])
        check("az eles ablak osszesen 58 mp", ablak, 58.0)

        # A viselkedest KIS rahagyassal merjuk (masodpercek, nem perc), az eles
        # ertekeket a fenti konfig-meres orzi. Igy mindketto gat marad.
        probe = retry_probe(backoff_factor=0.1, backoff_max=1.0, backoff_jitter=0.0)
        check("tartos 500-ra 6 kiserlet megy el", probe["hits"], 6)
        check("es tenyleg tobbszor probal, nem egyszer", probe["hits"] > 1, True)
        gaps = probe["gaps"]
        check("a rahagyas NO, nem allando",
              len(gaps) >= 3 and gaps[1] > gaps[0] and gaps[2] > gaps[1], True, )
        # A szoveget NEM kotjuk a kiserletszamhoz: azt a fenti allitas orzi. Itt
        # egyetlen kerdes van — megszolal-e egyaltalan a retry-reteg.
        check("a kimerult retry KIMONDJA magat a naploban",
              "kiserletre HTTP 500" in probe["stderr"]
              or "minden kiserlet elbukott" in probe["stderr"], True)

        healed = retry_probe(fail_first=2, backoff_factor=0.1, backoff_max=1.0, backoff_jitter=0.0)
        check("atmeneti 500 utan a 3. kiserlet atmegy", healed["status"], 200)
        check("es pontosan 3 kiserletbe kerult", healed["hits"], 3)
        check("a naplo megnevezi, hanyadikra sikerult", "3. kiserletre HTTP 200" in healed["stderr"], True)

        quiet = retry_probe(fail_first=0, backoff_factor=0.1, backoff_max=1.0, backoff_jitter=0.0)
        check("elsore sikeres keres: 1 kiserlet", quiet["hits"], 1)
        check("es NEM zajong a naploban", "retry:" in quiet["stderr"], False)

        print("\n[G] Kulso idokorlat: a NEMA futas nem loghat orakig [RED1 H-2]")
        # A scraper EL, csak nem ter vissza (hallgato forras). Eddig semmi nem
        # vagta el: se a shell, se a plist, se a CI.
        check("van idokorlat-binaris, amivel merni lehet", bool(TIMEOUT_BIN), True)
        if platform.system() == "Darwin":
            # A PRODUKCIOS utvonal helyessege — ezt csak a Mac-en van ertelme
            # kerdezni, mert a launchd ott hivja a run_daily.sh-t.
            check("a produkcios idokorlat-binaris letezik es futtathato",
                  PROD_TIMEOUT_BIN.is_file() and os.access(PROD_TIMEOUT_BIN, os.X_OK), True)
        g1 = shell_case(tmp, 0, True, sleep_seconds=3, tag="to",
                        extra_env={"TOJASAR_TIMEOUT_SECONDS": "1",
                                   "TOJASAR_TIMEOUT_BIN": TIMEOUT_BIN})
        check("idotullepesnel a kilepesi kod 124", g1["rc"], 124)
        check("es RIASZT (nem marad nema)", g1["riasztott"], True)
        check("a naplo kimondja az idotullepest", "IDOTULLEPES" in g1["log"], True)

        # FAIL-OPEN kontroll: hianyzo idokorlat-binaris nem akaszthatja meg a napi
        # adatgyujtest. (A `timeout` a /opt/homebrew/bin-ben van, a launchd
        # alapertelmezett PATH-jan NINCS rajta — ezert kell teljes ut es guard.)
        g2 = shell_case(tmp, 0, True, tag="nobin",
                        extra_env={"TOJASAR_TIMEOUT_BIN": "/nonexistent/timeout"})
        check("hianyzo idokorlat-binarisnal a futas ATTOL MEG lemegy", g2["rc"], 0)
        check("de kimondja, hogy idokorlat nelkul fut", "IDOKORLAT NELKUL" in g2["log"], True)
        # A hianyzo gat KONFIGURACIO-DRIFT: a sikeres futas sem fedheti el
        # (RED1 M-2 — "a kapu naplozza, hogy vedene, de nem ved").
        check("a hianyzo gat akkor is RIASZT, ha a futas sikeres", g2["riasztott"], True)
        check("es a riasztas SZOVEGE is kimondja, nem csak a naplo",
              "A GAT NEM VEDETT" in g2["riasztas_szovege"], True)

        # Az URES utvonal is "nincs mivel merni" — NEM eshet vissza nemán a
        # produkcios defaultra. Ez a `${VAR-default}` es a `${VAR:-default}`
        # kozti kulonbseg, es pontosan ezen a ponton latszott volna zoldnek egy
        # olyan CI, ahol egyaltalan nincs `timeout`. (RED1 H-2.)
        g5 = shell_case(tmp, 0, True, tag="ures", extra_env={"TOJASAR_TIMEOUT_BIN": ""})
        check("ures idokorlat-utvonal: fail-open, nem csendes visszaeses",
              "IDOKORLAT NELKUL" in g5["log"], True)

        # ...de csak EGYSZER, amig a helyzet fennall (esemeny, nem allapot).
        g2b = shell_case(tmp, 0, True, tag="nobin2",
                         extra_env={"TOJASAR_TIMEOUT_BIN": "/nonexistent/timeout",
                                    "TOJASAR_TIMEOUT_MISSING_MARKER": str(tmp / "mm.marker")})
        check("elso alkalommal szol", g2b["riasztott"], True)
        g2c = shell_case(tmp, 0, True, tag="nobin3",
                         extra_env={"TOJASAR_TIMEOUT_BIN": "/nonexistent/timeout",
                                    "TOJASAR_TIMEOUT_MISSING_MARKER": str(tmp / "mm.marker")})
        check("masodszor MAR NEM ismetli magat", g2c["riasztott"], False)

        # A riasztas a MOSTANI futas sorait mutassa (RED1 M-1): a naplo
        # append-only es sosem forog, korabban a tail 10 sora lehetett 100%-ban
        # tobb napos tartalom.
        # Es a masik irany: ha CSAK regi sor van, a riasztas akkor is kimegy —
        # ures kontextussal, de nem hallgat el (a riasztas ténye fontosabb).
        g4 = shell_case(tmp, 1, False, tag="scope",
                        err_log_seed=["warn: EZ EGY REGI, TEGNAPELOTTI SOR"] * 15)
        check("csak regi sorok mellett is kimegy a riasztas", g4["riasztott"], True)
        check("de a regi tartalmat nem adja ki mai gyanant",
              "TEGNAPELOTTI" in g4["riasztas_szovege"], False)

        # A riasztas olvashatosaga: a tail 10 sorat a retry-zaj felemesztheti
        # (RED1 M-1 merte: stale-riasztasnal 9/10 sor lehet retry).
        zaj = ["warn: retry: GET https://x/y — 6. kiserletre HTTP 500"] * 12
        g3 = shell_case(tmp, 1, False, tag="zaj",
                        err_log_seed=["warn: EZ EGY REGI, TEGNAPELOTTI SOR"] * 15,
                        runtime_err_lines=zaj + ["warn: A LENYEGI HIBA"])
        check("a riasztasbol a retry-zaj kimarad", "retry: GET" in g3["riasztas_szovege"], False)
        check("de az erdemi sor BENT marad", "A LENYEGI HIBA" in g3["riasztas_szovege"], True)
        check("es a kihagyott sorok szama meg van nevezve",
              "+12 retry-sor" in g3["riasztas_szovege"], True)
        check("a KORABBI futasok sorai viszont nem", "TEGNAPELOTTI" in g3["riasztas_szovege"], False)

        print("\n[F] renderFreshness regi/hianyos sement sem dol el [RED1 M2 res]")
        ui = render_freshness_cases(tmp)
        if "_hiba" in ui:
            check("node-harness lefutott", ui["_hiba"], "")
        else:
            check("v0.4 (nincs freshness) nem dob hibat", ui["v04_nincs_freshness"]["ok"], True)
            check("ures categories nem dob hibat", ui["ures"]["ok"], True)
            check("null updated_through nem dob hibat", ui["null_datum"]["ok"], True)
            check("elavultnal a savo LATSZIK", ui["elavult"].get("display"), "block")
            check("es megnevezi a sorozatot",
                  "60 napja" in ui["elavult"].get("txt", ""), True)
            check("v0.4-en a savo REJTVE", ui["v04_nincs_freshness"].get("display"), "none")

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 66)
    print(f"OSSZESEN {len(results)} allitas, BUKOTT {len(failed)}")
    for name, _, detail in failed:
        print(f"  BUKIK: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
