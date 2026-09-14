#!/usr/bin/env python3
"""Regresszios tesztek a tojasar-scraper 2026-09-14-i javitasaira.

Mit oriz ez a teszt (mindharom valodi, elesben megtortent hibaosztaly):

  A) A hiba-kapu a MENTES ELOTT allt -> egy forras kiesese eldobta az osszes
     tobbi forras aznapi adatat. (2026-09-14 07:30: 13-bol 12 forras sikeres,
     2755 megfigyeles, a DB-be 0 sor.)
  B) Az elavultsag-kapu a "nem volt hiba" ag UTAN allt -> egy befagyott, de
     HTTP 200-at ado forras mellett a rendszer exit 0-t adott. (RED1 F-2)
  C) A `set +e` zsh-ben nem kapcsolja ki a `trap ERR`-t -> a degradalt futas
     megis riasztott, es a git-blokk le sem futott. (RED1 F-1)

A teszt NEM fugg sem a halozattol, sem az elo DB-tol: sajat DB-t epit, es a
scrape_source-t monkeypatcheli. Igy friss klonon is fut.

Futtatas:  /usr/bin/python3 scraper/test_partial_failure.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta
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


def observation(key: str, day: date) -> dict:
    return {
        "key": key,
        "label": f"teszt {key}",
        "country": "NL",
        "category": "kelteto",
        "size": None,
        "color": None,
        "unit": "EUR/100",
        "week_iso": f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}",
        "observed_date": day.isoformat(),
        "price": 123.4,
        "change": None,
        "fetched_at": f"{day.isoformat()}T09:00:00+00:00",
        "source_url": "https://example.invalid/teszt",
    }


def build_db(path: Path, seed_day: date) -> int:
    """Sajat DB — nem az elo adatbazis masolata (RED1 F-5)."""
    seeded = [observation(key, seed_day) for key in SOURCE_KEYS]
    return store.store_observations(seeded, path)


def count_rows(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    finally:
        conn.close()


def run_main(tmp: Path, name: str, patch, seed_day: date):
    db, out = tmp / f"{name}.db", tmp / f"{name}.json"
    build_db(db, seed_day)
    before = count_rows(db)
    scrape.scrape_source = patch
    code = scrape.main(["--db", str(db), "--out", str(out)])
    return code, before, count_rows(db), out


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
    return [observation(source.key, TODAY - timedelta(days=60))]


def one_bad_row(session, source):
    """Egy forras ertelmezhetetlen arat ad; a tobbi 12 adata NEM veszhet el."""
    if source.key == FAILING_KEY:
        bad = observation(source.key, TODAY)
        bad["price"] = None
        return [bad]
    return [observation(source.key, TODAY)]


def shell_case(tmp: Path, exit_code: int) -> tuple[int, str]:
    """A VALODI run_daily.sh, /bin/zsh alatt (ahogy a launchd hivja), stub scraperrel."""
    work = tmp / f"shell{exit_code}"
    (work / "scraper").mkdir(parents=True)
    (work / "dashboard").mkdir()
    script = (SCRAPER_DIR / "run_daily.sh").read_text(encoding="utf-8")
    script = "\n".join(
        f'PROJECT_ROOT="{work}"' if line.startswith("PROJECT_ROOT=") else line
        for line in script.splitlines()
    )
    (work / "scraper" / "run_daily.sh").write_text(script, encoding="utf-8")
    (work / "scraper" / "scrape.py").write_text(
        f"import sys\nsys.exit({exit_code})\n", encoding="utf-8"
    )
    (work / "dashboard" / "data.json").write_text('{"x":1}\n', encoding="utf-8")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(work),
        # szandekosan nem letezo utvonal: a teszt SOSEM kuldhet valodi uzenetet
        "TELEGRAM_ENV": "/nonexistent/telegram.env",
    }
    for cmd in (["git", "init", "-q", "."], ["git", "add", "-A"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=work, env=env, check=True, capture_output=True)
    proc = subprocess.run(
        ["/bin/zsh", "scraper/run_daily.sh"],
        cwd=work, env=env, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tojasar-teszt-") as tmpdir:
        tmp = Path(tmpdir)
        recent = TODAY - timedelta(days=3)

        print("\n[A1] EGY forras bukik, a tobbi jo -> a jo adat NEM veszhet el")
        code, before, after, out = run_main(tmp, "egy_bukik", one_fails, recent)
        check("kilepesi kod = EXIT_DEGRADED", code, scrape.EXIT_DEGRADED)
        check("pontosan a 12 jo forras sora kerult be",
              after - before, len(SOURCE_KEYS) - 1)
        check("data.json ujragenaralodott", out.exists(), True)

        print("\n[A2] MINDEN forras bukik -> riaszt, es a data.json NEM kap hamis datumot")
        code, before, after, out = run_main(tmp, "mind_bukik", all_fail, recent)
        check("kilepesi kod = EXIT_ALERT", code, scrape.EXIT_ALERT)
        check("semmi nem mentodott", after, before)
        check("data.json NEM irodott ki (nincs hamis generated_at)", out.exists(), False)

        print("\n[A3] MINDEN forras jo -> tiszta siker")
        code, before, after, _ = run_main(tmp, "mind_jo", all_ok, recent)
        check("kilepesi kod = EXIT_OK", code, scrape.EXIT_OK)
        check("mentodott", after > before, True)

        print("\n[B1] BEFAGYOTT forras: mind HTTP 200, de regi adat -> RIASZT")
        code, _, _, out = run_main(tmp, "befagyott", all_frozen,
                                   TODAY - timedelta(days=60))
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("kilepesi kod = EXIT_ALERT (nem 0!)", code, scrape.EXIT_ALERT)
        check("a freshness blokk jelzi az elavulast",
              len(payload["freshness"]["series_stale"]) > 0, True)

        print("\n[B2] Friss adat -> NINCS fals elavultsag-riasztas")
        code, _, _, out = run_main(tmp, "friss", all_ok, recent)
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("nincs elavult sorozat", len(payload["freshness"]["series_stale"]), 0)
        check("a kuszob a mert 14 napos maximum FOLOTT van",
              store.STALE_AFTER_DAYS > 14, True)

        print("\n[A4] Egy rossz SOR nem dobhatja el a tobbi forras koteget")
        code, before, after, _ = run_main(tmp, "rossz_sor", one_bad_row, recent)
        check("a 12 jo forras adata bekerult",
              after - before, len(SOURCE_KEYS) - 1)

        print("\n[C] run_daily.sh /bin/zsh alatt — a trap ERR nem lohet ki mindent")
        rc0, log0 = shell_case(tmp, 0)
        check("exit 0 -> script rc=0", rc0, 0)
        check("exit 0 -> nincs riasztas", "cannot send failure alert" in log0, False)

        rc2, log2 = shell_case(tmp, 2)
        check("exit 2 -> script rc=0", rc2, 0)
        check("exit 2 -> NINCS riasztas", "cannot send failure alert" in log2, False)
        check("exit 2 -> a degradalt uzenet kiirodott",
              "degradalt futas" in log2, True)
        check("exit 2 -> a git-blokk ELERHETO",
              "skipping commit/push" in log2, True)

        rc1, log1 = shell_case(tmp, 1)
        check("exit 1 -> script rc=1", rc1, 1)
        check("exit 1 -> RIASZT", "cannot send failure alert" in log1, True)

        print("\n[D] A frissesseg ki van mondva az exportban")
        payload = json.loads((tmp / "egy_bukik.json").read_text(encoding="utf-8"))
        every = [s for c in payload["categories"] for s in c["series"]]
        check("minden sorozatnak van updated_through",
              all("updated_through" in s for s in every), True)
        check("minden sorozatnak van days_since_update",
              all("days_since_update" in s for s in every), True)
        check("freshness blokk letezik", "freshness" in payload, True)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 64)
    print(f"OSSZESEN {len(results)} allitas, BUKOTT {len(failed)}")
    for name, _, detail in failed:
        print(f"  BUKIK: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
