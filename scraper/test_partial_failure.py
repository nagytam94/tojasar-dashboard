#!/usr/bin/env python3
"""Regresszios teszt a 2026-09-14-i gyoker-javitasra.

A GYOKER, amit ez a teszt orzi: a scrape.py main()-jeben a hiba-kapu korabban a
MENTES ELOTT allt, ezert egyetlen forras kiesese eldobta az OSSZES tobbi forras
aznapi adatat. Merve 2026-09-14 07:30: 13 forrasbol 12 sikeres (2755 megfigyeles),
a DB-be aznap 0 sor kerult, a dashboard befagyott, es Tomi riasztast kapott.

A halozatot NEM hivjuk: a scrape_source monkeypatchelve van, igy a teszt
determinisztikus es azt meri, amit modositottunk — a main() sorrendjet es a
kilepesi kodokat.

Futtatas:  /usr/bin/python3 scraper/test_partial_failure.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_DIR = PROJECT_ROOT / "scraper"
LIVE_DB = PROJECT_ROOT / "data" / "eggprices.db"
sys.path.insert(0, str(SCRAPER_DIR))

import scrape  # noqa: E402

STAMP = f"{date.today().isoformat()}T09:00:00+00:00"
FAILING_KEY = "napos_csibe_cenyrolnicze"

results: list[tuple[str, bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append((name, ok, f"kapott={got!r} vart={want!r}"))
    print(f"  {'OK  ' if ok else 'BUKIK'}  {name}: kapott={got!r} vart={want!r}")


def observation_count(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    finally:
        conn.close()


def make_observation(key: str) -> dict:
    return {
        "key": key,
        "label": f"teszt {key}",
        "country": "NL",
        "category": "kelteto",
        "size": None,
        "color": None,
        "unit": "EUR/100",
        "week_iso": "2026-W38",
        "observed_date": STAMP[:10],
        "price": 123.4,
        "change": None,
        "fetched_at": STAMP,
        "source_url": "https://example.invalid/teszt",
    }


def one_source_fails(session, source):
    if source.key == FAILING_KEY:
        raise RuntimeError("szimulalt forraskieses")
    return [make_observation(source.key)]


def every_source_fails(session, source):
    raise RuntimeError("szimulalt teljes kieses")


def every_source_ok(session, source):
    return [make_observation(source.key)]


def run(tmp: Path, name: str, patch, mutate_db=None):
    db = tmp / f"{name}.db"
    out = tmp / f"{name}.json"
    shutil.copy(LIVE_DB, db)
    if mutate_db:
        mutate_db(db)
    before = observation_count(db)
    scrape.scrape_source = patch
    code = scrape.main(["--db", str(db), "--out", str(out)])
    return code, before, observation_count(db), out


def main() -> int:
    if not LIVE_DB.exists():
        print(f"HIBA: nincs meg a DB: {LIVE_DB}")
        return 1

    with tempfile.TemporaryDirectory(prefix="tojasar-teszt-") as tmpdir:
        tmp = Path(tmpdir)

        print("\n[1] EGY forras bukik, a tobbi jo -> a jo adat NEM veszhet el")
        code, before, after, out = run(tmp, "egy_bukik", one_source_fails)
        check("kilepesi kod = EXIT_DEGRADED (2)", code, scrape.EXIT_DEGRADED)
        check("a jo adat elmentodott", after > before, True)
        check("data.json ujragenaralodott", out.exists(), True)

        print("\n[2] MINDEN forras bukik -> riasztas, es nincs mit menteni")
        code, before, after, _ = run(tmp, "mind_bukik", every_source_fails)
        check("kilepesi kod = EXIT_ALERT (1)", code, scrape.EXIT_ALERT)
        check("semmi nem mentodott", after, before)

        print("\n[3] MINDEN forras jo -> tiszta siker")
        code, before, after, _ = run(tmp, "mind_jo", every_source_ok)
        check("kilepesi kod = EXIT_OK (0)", code, scrape.EXIT_OK)
        check("mentodott", after > before, True)

        print("\n[4] Elavult sorozat -> a degradalt futas is RIASZT (nem nemul el)")

        def age_everything(db: Path) -> None:
            old_day = (date.today() - timedelta(days=40)).isoformat()
            conn = sqlite3.connect(db)
            conn.execute("UPDATE observation SET observed_date = ?", (old_day,))
            conn.commit()
            conn.close()

        code, _, _, out = run(tmp, "elavult", one_source_fails, mutate_db=age_everything)
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("kilepesi kod = EXIT_ALERT (1)", code, scrape.EXIT_ALERT)
        check("a freshness blokk jelzi az elavulast",
              len(payload["freshness"]["series_stale"]) > 0, True)

        print("\n[5] A frissesseg ki van mondva az exportban")
        payload = json.loads((tmp / "egy_bukik.json").read_text(encoding="utf-8"))
        every_series = [s for c in payload["categories"] for s in c["series"]]
        check("minden sorozatnak van updated_through",
              all("updated_through" in s for s in every_series), True)
        check("minden sorozatnak van days_since_update",
              all("days_since_update" in s for s in every_series), True)
        check("freshness blokk letezik", "freshness" in payload, True)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 62)
    print(f"OSSZESEN {len(results)} allitas, BUKOTT {len(failed)}")
    for name, _, detail in failed:
        print(f"  BUKIK: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
