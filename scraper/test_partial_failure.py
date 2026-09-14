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

import json
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
def shell_case(tmp: Path, exit_code: int, change_data: bool, with_remote: bool = True):
    """A VALODI run_daily.sh, /bin/zsh alatt (ahogy a launchd hivja).

    `with_remote=False` -> a `git push` bukik: ezzel meressuk, hogy a trap
    TOVABBRA is jelenti a git-hibakat (pozitiv kontroll).
    """
    work = tmp / f"shell{exit_code}{'c' if change_data else ''}{'r' if with_remote else 'n'}"
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
    if change_data:
        body += ('import pathlib, json\n'
                 'pathlib.Path("dashboard/data.json").write_text(json.dumps({"x":2}))\n')
    body += f"sys.exit({exit_code})\n"
    (work / "scraper" / "scrape.py").write_text(body, encoding="utf-8")
    (work / "dashboard" / "data.json").write_text('{"x":1}\n', encoding="utf-8")
    marker = work / "data" / ".alert-attempted"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(work),
        "TELEGRAM_ENV": "/nonexistent/telegram.env",   # sosem kuld valodi uzenetet
        "TOJASAR_ALERT_ATTEMPT_MARKER": str(marker),
        "TOJASAR_ALERT_MARKER": str(work / "data" / ".alert-sent"),
    }
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
        "kezbesitve": (work / "data" / ".alert-sent").exists(),
        "commitok": int(commits),
        "log": proc.stdout + proc.stderr,
    }


# --- retry-harness -----------------------------------------------------------
class _Counting(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b"nope")

    def log_message(self, *a):  # csend
        pass


def retry_hits() -> int:
    _Counting.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Counting)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        session = scrape.build_session()
        try:
            session.get(f"http://127.0.0.1:{srv.server_port}/x", timeout=5)
        except Exception:
            pass
        return _Counting.hits
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


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
        recent = [10, 3]          # ket friss adatpont -> ~7 napos ritmus

        print("\n[A1] EGY forras bukik, a tobbi jo -> a jo adat NEM veszhet el")
        db, out = tmp / "a1.db", tmp / "a1.json"
        seed(db, recent)
        code, before, after = run_main(db, out, one_fails)
        check("kilepesi kod = EXIT_DEGRADED", code, scrape.EXIT_DEGRADED)
        check("pontosan a 12 jo forras sora kerult be", after - before,
              len(SOURCE_KEYS) - 1)
        check("data.json ujragenaralodott", out.exists(), True)

        print("\n[A2] MINDEN forras bukik -> riaszt, data.json NEM kap hamis datumot")
        db, out = tmp / "a2.db", tmp / "a2.json"
        seed(db, recent)
        code, before, after = run_main(db, out, all_fail)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)
        check("semmi nem mentodott", after, before)
        check("data.json NEM irodott ki", out.exists(), False)

        print("\n[A3] MINDEN forras jo -> tiszta siker")
        db, out = tmp / "a3.db", tmp / "a3.json"
        seed(db, recent)
        code, before, after = run_main(db, out, all_ok)
        check("kilepesi kod = EXIT_OK", code, scrape.EXIT_OK)
        check("mentodott", after > before, True)

        print("\n[A4] EGY rossz sor nem dobhatja el a tobbi forras koteget")
        db, out = tmp / "a4.db", tmp / "a4.json"
        seed(db, recent)
        code, before, after = run_main(db, out, one_bad_row)
        check("a 12 jo forras adata bekerult", after - before, len(SOURCE_KEYS) - 1)
        check("degradaltnak jelzi (nem nema siker)", code, scrape.EXIT_DEGRADED)

        print("\n[A5] MINDEN sor elutasitva -> RIASZT (nem nema siker) [RED1 N-2]")
        db, out = tmp / "a5.db", tmp / "a5.json"
        seed(db, recent)
        code, before, after = run_main(db, out, every_row_bad)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)
        check("tenyleg semmi nem ment be", after, before)

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
        hits = retry_hits()
        check("a scraper ujraprobal (1 keres + 3 retry = 4 talalat)", hits, 4)
        check("es tenyleg tobbszor probal, nem egyszer", hits > 1, True)

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
