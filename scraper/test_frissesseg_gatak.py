#!/usr/bin/env python3
"""A ket frissesseg-gat tesztjei — PIROS agakkal.

A projekt meglevo stilusat koveti (`test_partial_failure.py`): nincs pytest-fuggoseg,
`main()` szamol es nem-nulla kilepokoddal ter vissza, ha barmi bukik.

MIT MERNEK EZEK A TESZTEK
-------------------------
Nem azt, hogy a gat "mukodik" — azt, hogy **BUKIK-E, AMIKOR KELL**. Egy gat, ami
mindig False-t ad, ugyanugy nez ki egy egeszseges rendszeren, mint egy helyes gat.
Ezert minden allitasnak van piros parja: a szintetikus romlasra a gatnak meg KELL
szolalnia, kulonben a teszt bukik.

Futtatas: python3 scraper/test_frissesseg_gatak.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(nev: str, kapott, vart) -> None:
    ok = kapott == vart
    results.append((nev, ok, f"kapott={kapott!r} vart={vart!r}"))
    print(f"  {'OK   ' if ok else 'BUKIK'} {nev}: kapott={kapott!r} vart={vart!r}")


def _db(td: Path) -> Path:
    return td / "t.db"


def _sorozat(conn, key: str) -> int:
    return store.get_or_create_series(conn, key=key, label=key, country="XX",
                                      category="kelteto", size=None, color=None,
                                      unit="EUR", source_url="")


def _het(conn, sid: int, w: str, observed: date, first_seen: date, src: str = "scrape") -> None:
    store.upsert_observation(
        conn, series_id=sid, week_iso=w, observed_date=observed.isoformat(),
        price=1.0, change=None, native_price=None, native_unit=None, fx_rate=None,
        fx_rate_unit=None, fx_rate_date=None, fx_source=None,
        fetched_at="2026-09-29T06:00:00+00:00", raw={}, first_seen_at=first_seen.isoformat())
    if src != "scrape":
        conn.execute("UPDATE observation SET first_seen_src=? WHERE series_id=? AND week_iso=?",
                     (src, sid, w))


def _heti_sorozat(conn, key: str, hetek: int, keses: int, elso: date) -> int:
    """`hetek` darab heti adat, mindegyik `keses` nappal a hete utan szallitva."""
    sid = _sorozat(conn, key)
    for i in range(hetek):
        obs = elso + timedelta(days=7 * i)
        _het(conn, sid, f"2026-W{10 + i:02d}", obs, obs + timedelta(days=keses))
    return sid


def _sor(db: Path, key: str, as_of: date) -> dict:
    return next(r for r in store.series_freshness(db, as_of=as_of) if r["key"] == key)


def main() -> int:
    elso = date(2026, 3, 2)          # hetfo

    # ─────────────────────────────────────────────────────────────────────
    # EZ A LEGFONTOSABB TESZT A FAJLBAN. A `COALESCE` az egesz atalakitas
    # tartooszlopa: ha az upsert felulirna a first_seen_at-et, minden napi futas
    # nullazna a szallitasi orat (0 res -> fallback, szallitasi kor 0), es a
    # Gat 1 OROKRE nema lenne — miközben minden zoldnek latszana.
    # A RED1 #6 megmerte: a COALESCE kicserelese `excluded`-re 118/118 zold
    # tesztet hagyott, mert egyetlen teszt sem upsertalt UJRA ugyanarra a hetre.
    # A terv AC-23 piros agat kimondta; a teszt hianyzott.
    print("\n# AC-23 — az UJRA-UPSERT nem mozdithatja a szallitasi orat")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            sid = _sorozat(c, "ismetelt")
            # 1. nap: uj het erkezik
            _het(c, sid, "2026-W40", date(2026, 9, 28), date(2026, 9, 29))
            r1 = c.execute("SELECT first_seen_at, first_seen_src, fetched_at, price "
                           "FROM observation WHERE week_iso='2026-W40'").fetchone()
            # 8. nap: UGYANAZ a het jon ujra, frissebb arral es fetched_at-tel
            store.upsert_observation(
                c, series_id=sid, week_iso="2026-W40", observed_date="2026-09-28",
                price=9.99, change=None, native_price=None, native_unit=None,
                fx_rate=None, fx_rate_unit=None, fx_rate_date=None, fx_source=None,
                fetched_at="2026-10-06T06:00:00+00:00", raw={},
                first_seen_at="2026-10-06")
            r2 = c.execute("SELECT first_seen_at, first_seen_src, fetched_at, price "
                           "FROM observation WHERE week_iso='2026-W40'").fetchone()
            # majd egy KOVETKEZO het
            _het(c, sid, "2026-W41", date(2026, 10, 5), date(2026, 10, 6))
            r3 = c.execute("SELECT first_seen_at FROM observation "
                           "WHERE week_iso='2026-W41'").fetchone()
        check("1. beiras: a szallitasi ora indul", r1["first_seen_at"], "2026-09-29")
        check("  ... es a provenance 'scrape'", r1["first_seen_src"], "scrape")
        check("UJRA-UPSERT: a first_seen_at NEM MOZDUL", r2["first_seen_at"], "2026-09-29")
        check("  ... a provenance sem", r2["first_seen_src"], "scrape")
        check("  ... de a fetched_at FRISSUL (a ketto kulonbsege a keses)",
              r2["fetched_at"][:10], "2026-10-06")
        check("  ... es az ar is frissul (nem fagyasztottuk be az egesz sort)",
              r2["price"], 9.99)
        check("KOVETKEZO het: sajat, uj first_seen-t kap", r3["first_seen_at"], "2026-10-06")

    # ─────────────────────────────────────────────────────────────────────
    print("\n# GAT 1 — szallitasi ora")
    with tempfile.TemporaryDirectory() as td:        # cleanup-fegyelem (trap/finally)
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            # 17 heti szallitas, 11 napos publikalasi kesessel (az EU mintaja)
            _heti_sorozat(c, "eu_szeru", 17, 11, elso)
        utolso_szall = elso + timedelta(days=7 * 16 + 11)

        # ZOLD: 4 nappal az utolso szallitas utan
        r = _sor(db, "eu_szeru", utolso_szall + timedelta(days=4))
        check("normal mukodes: nem stale", r["stale"], False)
        check("  ... a szallitasi kuszob tanult", r["delivery_stale_after_days"], 14)
        check("  ... a publikalasi keses latszik", r["publication_lag_days"], 11)

        # PIROS: 40 nap nemasag -> a szallitasi gatnak szolnia KELL
        r = _sor(db, "eu_szeru", utolso_szall + timedelta(days=40))
        check("40 nap nemasag: STALE", r["stale"], True)
        check("  ... es az OK a szallitas", r["reason"], "delivery")

    # HATARERTEK — IZOLALVA: a Gat 2-nek nem szabad kozbeszolnia, ezert 0 napos
    # publikalasi keses (az adatkor igy egyutt mozog a szallitasi korral, es a
    # 14/15-os hataron meg boven a 20-as plafon alatt van).
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            _heti_sorozat(c, "azonnali", 17, 0, elso)
        utolso = elso + timedelta(days=7 * 16)
        r = _sor(db, "azonnali", utolso + timedelta(days=14))
        check("pontosan a kuszobon (14): meg nem stale", r["stale"], False)
        r = _sor(db, "azonnali", utolso + timedelta(days=15))
        check("egy nappal folotte (15): stale", r["stale"], True)
        check("  ... es CSAK a szallitas az ok", r["reasons"], ["delivery"])

    # ─────────────────────────────────────────────────────────────────────
    print("\n# GAT 2 — adatkor-plafon (fix 20)")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            # a forras PONTOSAN szallit, de egyre regebbi adatot: 30 napos kesessel
            sid = _sorozat(c, "csuszo")
            for i in range(12):
                obs = elso + timedelta(days=7 * i)
                _het(c, sid, f"2026-W{10+i:02d}", obs, obs + timedelta(days=30))
        utolso_szall = elso + timedelta(days=7 * 11 + 30)

        # a szallitas friss (1 napja), de az adat 31 napos -> a Gat 2 fogja meg
        r = _sor(db, "csuszo", utolso_szall + timedelta(days=1))
        check("pontos szallitas, regi adat: STALE", r["stale"], True)
        check("  ... es az OK az adatkor", r["reason"], "age")
        check("  ... a szallitasi ora NEM jelez", r["days_since_delivery"], 1)

    # A PLAFON KET OLDALA — IZOLALVA: a szallitas legyen FRISS (a vizsgalat
    # napjan erkezett), hogy csak az adatkor donthessen.
    for plusz, vart_stale, cimke in ((0, False, "adatkor == plafon (20): meg nem stale"),
                                     (1, True, "adatkor == plafon+1 (21): STALE")):
        with tempfile.TemporaryDirectory() as td:
            db = _db(Path(td))
            kor = store.ADATKOR_PLAFON_DAYS + plusz
            vizsgalat = date(2026, 9, 29)
            utolso_obs = vizsgalat - timedelta(days=kor)
            with store.connect(db) as c:
                store.init_db(c)
                sid2 = _sorozat(c, "hatar")
                for i in range(12):
                    obs = utolso_obs - timedelta(days=7 * (11 - i))
                    # minden het a VIZSGALAT napjan erkezett -> 0 res, friss szallitas
                    _het(c, sid2, f"2026-W{10+i:02d}", obs, vizsgalat)
            r = _sor(db, "hatar", vizsgalat)
            check(cimke, r["stale"], vart_stale)
            if vart_stale:
                check("  ... es CSAK az adatkor az ok", r["reasons"], ["age"])
            else:
                check("  ... de a plafon-kozelseg jelzi", r["age_ceiling_near"], True)

    # ─────────────────────────────────────────────────────────────────────
    print("\n# LEFEDETTSEG — hianyos first_seen -> a gat NEM elesedik, de LATSZIK")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            sid = _heti_sorozat(c, "hianyos", 17, 11, elso)
            # egyetlen sor provenance-a serul (fel-NULL par)
            c.execute("UPDATE observation SET first_seen_src=NULL "
                      "WHERE series_id=? AND week_iso='2026-W10'", (sid,))
        utolso_szall = elso + timedelta(days=7 * 16 + 11)
        r = _sor(db, "hianyos", utolso_szall + timedelta(days=40))
        check("hianyos lefedettseg: a szallitasi gat NEM elesedik", r["reason"] != "delivery", True)
        check("  ... es a hiany NEVESITVE latszik", r["first_seen_coverage_missing"], 1)
        check("  ... a szallitasi kuszob nincs kiszamolva", r["delivery_stale_after_days"], None)

        # ismeretlen provenance-cimke ugyanigy nem szamit lefedettnek
        with store.connect(db) as c:
            c.execute("UPDATE observation SET first_seen_src='valami' "
                      "WHERE series_id=(SELECT id FROM series WHERE key='hianyos') "
                      "AND week_iso='2026-W11'")
        r = _sor(db, "hianyos", utolso_szall + timedelta(days=40))
        check("ismeretlen provenance sem szamit lefedettnek",
              r["first_seen_coverage_missing"], 2)

    # ─────────────────────────────────────────────────────────────────────
    print("\n# MERGEZES — minden first_seen ugyanaz a nap (deploy-baleset)")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            sid = _sorozat(c, "mergezett")
            ma = date(2026, 9, 29)
            for i in range(17):
                _het(c, sid, f"2026-W{10+i:02d}", elso + timedelta(days=7 * i), ma)
        # 0 szallitasi res -> fallback 21, NEM a 30-as plafon, es nem is vakul meg
        r = _sor(db, "mergezett", date(2026, 9, 29))
        check("mergezes: a fallback kuszob lep be", r["delivery_stale_after_days"],
              store.STALE_AFTER_DAYS)
        check("  ... es nem stale a mergezes napjan", r["reason"] != "delivery", True)
        r = _sor(db, "mergezett", date(2026, 9, 29) + timedelta(days=22))
        check("mergezes utan 22 nappal: a gat MEGIS megszolal",
              r["reason"], "delivery")

    # ─────────────────────────────────────────────────────────────────────
    print("\n# NEGATIV KESES — elore datumozott kozles nem exportalhato ertekkent")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        with store.connect(db) as c:
            store.init_db(c)
            sid = _sorozat(c, "elore")
            for i in range(12):
                obs = elso + timedelta(days=7 * i + 10)      # a jovoben
                _het(c, sid, f"2026-W{10+i:02d}", obs, obs - timedelta(days=10))
        r = _sor(db, "elore", date(2026, 9, 29))
        check("negativ keses: nem exportalunk erteket", r["publication_lag_days"], None)
        check("  ... de a teny NEVESITVE van", r["publication_lag_negative"], True)

    # ─────────────────────────────────────────────────────────────────────
    print("\n# ALERT-SPECIFIKUS NYUGTA — mas riasztas sikere NEM nyugtaz stale-t")
    import json as _json
    import scrape
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td))
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()
        allapot = scrape._alert_state_path(db)
        marker = scrape._delivered_marker_path(db)
        stale_lista = [{"key": "valami", "reason": "delivery", "days_since_update": 30,
                        "stale_after_days": 14}]

        # 1. kor: uj stale -> FUGGOBEN
        scrape.newly_stale_series(db, stale_lista)
        raw = _json.loads(allapot.read_text())
        check("1. kor: a jelzes fuggoben", raw["pending_stale"], ["valami:delivery"])

        # 2/a: URES marker -> FAIL-CLOSED, NEM nyugta.
        # Ez az elso valtozat lyuka volt: az ures markert elfogadta "visszafele
        # kompatibilitasbol", kozben a shell ALLANDOAN ures markert gyartott.
        marker.write_text("")
        scrape.newly_stale_series(db, stale_lista)
        raw = _json.loads(allapot.read_text())
        check("URES marker: NEM nyugta (fail-closed)", raw["pending_stale"],
              ["valami:delivery"])

        # 2/b: egy MASIK riasztas ment ki sikeresen. A shell ilyenkor "other"-t ir
        # — ez VALODI ertek, nem kitalalt: l. a szerzodes-tesztet lent.
        marker.write_text("other\n")
        scrape.newly_stale_series(db, stale_lista)
        raw = _json.loads(allapot.read_text())
        check("2. kor: MAS riasztas nyugtaja NEM szamit", raw["pending_stale"],
              ["valami:delivery"])
        check("  ... es nem kerult 'jelentett'-be", raw["reported_stale"], [])

        # 3. kor: a STALE riasztas kezbesitese igazolt
        marker.write_text("stale\n")
        scrape.newly_stale_series(db, stale_lista)
        raw = _json.loads(allapot.read_text())
        check("3. kor: a sajat nyugta ELFOGADVA", raw["reported_stale"], ["valami:delivery"])
        check("  ... es a fuggo lista kiurult", raw["pending_stale"], [])

    # ─────────────────────────────────────────────────────────────────────
    print("\n# SEMA-MIGRACIO — a regi, csupasz kulcsu allapot nem gyart riasztas-aradatot")
    with tempfile.TemporaryDirectory() as td:
        db = _db(Path(td)); db.touch()
        allapot = scrape._alert_state_path(db)
        # REGI formatum: csupasz kulcs, reason nelkul
        allapot.write_text(_json.dumps({"reported_stale": ["valami"], "pending_stale": []}))
        friss = scrape.newly_stale_series(db, [{"key": "valami", "reason": "delivery",
                                                "days_since_update": 30,
                                                "stale_after_days": 14}])
        check("a deploy napjan NEM riasztunk ujra a formatumvaltas miatt", friss, [])

    # ─────────────────────────────────────────────────────────────────────
    # SZERZODES a shell es a Python kozott. A RED1 #6 leletje: a teszt olyan
    # marker-erteket hasznalt ("other"), amit a shell SOHA nem tudott eloallitani
    # — zold teszt egy halott uton. Ezert most a shell FORRASABOL ellenorizzuk,
    # hogy a ket oldal ugyanarrol beszel-e.
    print("\n# SZERZODES — a run_daily.sh tenylegesen eloallitja-e a vart ertekeket")
    sh = (Path(__file__).resolve().parent / "run_daily.sh").read_text(encoding="utf-8")
    check("a shell MINDIG ir tipust a markerbe (sosem ures)",
          "printf '%s\\n' \"$alert_kind\"" in sh, True)
    check("  ... es az alapertelmezes 'other', ha nincs .alert-kind",
          'alert_kind="other"' in sh, True)
    check("  ... a .alert-kind a kuldes ELEJEN elfogy (nem ragadhat benn)",
          sh.index('rm -f "$alert_kind_file"') < sh.index("api.telegram.org"), True)
    check("a Python CSAK a 'stale'-t fogadja el nyugtanak",
          'nyugta_ervenyes = marker_tipus == "stale"' in
          (Path(__file__).resolve().parent / "scrape.py").read_text(encoding="utf-8"), True)

    print("\n" + "=" * 66)
    bukott = [r for r in results if not r[1]]
    print(f"OSSZESEN {len(results)} allitas, BUKOTT {len(bukott)}")
    for nev, _, reszlet in bukott:
        print(f"  BUKIK: {nev} — {reszlet}")
    return 1 if bukott else 0


if __name__ == "__main__":
    raise SystemExit(main())
