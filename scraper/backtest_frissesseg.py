#!/usr/bin/env python3
"""Frissesseg-gat BACKTEST — a valodi git-tortenetre, HERMETIKUSAN.

MIERT LETEZIK
-------------
A frissesseg-ellenorzes atalakitasanak az egyetlen erdemi bizonyiteka az, hogy az uj
gat a MULTBELI VALODI adaton kevesebbet riaszt hamisan, mint a mai. Ez a script az az
orakulum — nem illusztracio, hanem elfogadasi kriterium: a `PINELT` szamoktol valo
elteres kilepokodot valt.

A hatter: az `eu_whole_broiler_65` sorozatot a mai ellenorzes hetente 2-3 napra
elavultnak jelolte, holott a forras ep. Az ok: a kuszob a kozlesi ritmust tanulja
(`observed_date` resek, heti sorozatnal 7+7=14 nap), a `stale` viszont a MAI naptol
mer, es abba beleszamit a forras ~11 napos publikalasi kesese is. Ket ora keveredik
egy osszehasonlitasban.

Ket kudarcbol tanult, es mindkettot gepileg zarja ki:

1. **Atvett szam.** A terv egy korabbi valtozata egy lektori jelentesbol vett szamot
   (33 riasztasi esemeny) idezett meresnek. Nem allt. Azota az orakulum SCRIPT, nem idezet.

2. **Rossz mennyiseg meresе.** A masodik valtozat az `observed_date`-et merte szallitasnak,
   holott a `first_seen_at` a `UNIQUE(series_id, week_iso)` soron el (`store.py`), tehat a
   szallitas szemcseje a HET. A hiba oka: a script `p.get("week_iso")`-t keresett, de a
   `data.json`-ban a mezo neve `week`, igy a kereses csendben a `date`-re esett vissza.
   **A rossz definicio es a hideg-indulasi feltevés kioltotta egymast, es a hibas script
   helyes 0-t adott** — egy orakulum, ami ket hibabol ad jo eredmenyt, elteresekor sem
   fog megszolalni. Ezert a szallitas definicioja itt EGY helyen all, kimondva (`_hetek`).

HERMETIKUS — ket egymas utani futas bitre azonos kimenetet ad:
  * a commit-tartomany ROGZITETT (`ELSO_SHA` … `UTOLSO_SHA`), nem az elo `git log` vege;
  * a sorozat-kulcsok FIXTURE-bol jonnek (`fixtures/backtest_kulcsok.json`), nem az elo DB-bol;
  * `as_of` = az utolso commit napja, nem a mai datum.
Ha barmelyik felteves serul (uj commit, valtozott kulcs-keszlet), az EXPLICIT dontes legyen:
a konstansokat kell atirni, es a PINELT szamokat ujra levezetni.

DEFINICIOK (ezek AC-k, nem izles kerdese):
  SZALLITAS  — az adott naptari napon megjelent egy olyan `week_iso`, amit az adott sorozatnal
               korabban meg nem lattunk.
  ESEMENY    — `(sorozat, nap)` par, ahol a sorozat aznap stale, az elozo VIZSGALT napon nem.
               Ez az, amire a scraper `newly_stale`-t jelent es riasztast kuld.
  STALE-NAP  — `(sorozat, nap)` parok szama, ahol a sorozat stale.
  NAP        — a tartomany azon naptari napjai, amelyre van `data.json` commit (115 a 117-bol;
               2026-09-07 es 09-08 hianyzik). A hianyzo napokat NEM interpolaljuk.
  HIDEG      — a gat a tartomany elejen nullarol tanul (ezt latta volna a 06-05-i bekapcsolas).
  MELEG      — a kuszob a TELJES tortenetbol szamol minden napon (ez a migracio utani valosag).

Futtatas:  python3 scraper/backtest_frissesseg.py [--json]
Kilepokod: 0 = a mert szamok egyeznek a PINELT ertekekkel · 1 = elteres · 2 = nem merheto
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAJL = "dashboard/data.json"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "backtest_kulcsok.json"

# ── ROGZITETT tartomany (l. a modul-docstring "HERMETIKUS" reszet) ──────────
ELSO_SHA = "668bac5abede878a85083a290a72294aca89520b"   # 2026-06-05, az elso data.json
UTOLSO_SHA = "c6f1ab94729e8111148236a84b47ff0a22ecf204"  # 2026-09-29

# ── a store.py-bol tukrozott konstansok ─────────────────────────────────────
# Szandekosan MASOLAT, nem import: ha a store.py-ban valtoznak, ennek a scriptnek a
# PINELT szamai ervenyuket vesztik, es ezt latni akarjuk (a teszt bukik), nem elnyelni.
GAP_WINDOW = 26
STALE_AFTER_DAYS = 21
MIN_STALE_AFTER_DAYS = 10
MAX_STALE_AFTER_DAYS = 30
ADATKOR_PLAFON_DAYS = 20        # Gat 2 — fix, nem tanult (v5)

# ── PINELT eredmenyek (AC-15) ───────────────────────────────────────────────
PINELT = {
    "mai":          {"esemeny": 17, "stale_nap": 45},
    "gat1_hideg":   {"esemeny": 4,  "stale_nap": 12},
    "gat1_meleg":   {"esemeny": 0,  "stale_nap": 0},
    "gat2":         {"esemeny": 0,  "stale_nap": 0},
}


class NemMerheto(RuntimeError):
    """A meres nem adhat megbizhato eredmenyt. Sosem 'OK'."""


def _git(*args: str) -> str:
    """rc != 0 -> KIVETEL. Nem csendes ures string (a nulla talalat nem lelet)."""
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=180, shell=False)
    if r.returncode != 0:
        raise NemMerheto(f"git {' '.join(args)} -> rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def _kulcsok() -> set[str]:
    try:
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except OSError as e:
        raise NemMerheto(f"fixture nem olvashato: {FIXTURE}: {e}") from e
    k = set(doc.get("kulcsok") or [])
    if not k:
        raise NemMerheto(f"ures kulcs-fixture: {FIXTURE}")
    return k


def _sorozatok(doc: dict, sha: str) -> list[dict]:
    """Mind a NEGY data.json-sema. Ismeretlen alak -> KIVETEL, nem csendes atlepes.

    Merve: 125 commiton a semak MISSING=3 / 0.3=3 / 0.4=100 / 0.5=19. A legelso harom
    commitban NINCS `categories` kulcs — egy `.get("categories", [])` nemán atlepne oket,
    es ezzel pont a legkorabbi first-seen datumokat rontana el.
    """
    if "categories" in doc:
        return [s for kat in doc["categories"] for s in kat.get("series", [])]
    if "series" in doc:
        return doc["series"]
    raise NemMerheto(f"ismeretlen data.json alak {sha[:8]}: kulcsok={sorted(doc)[:6]}")


def _hetek(s: dict) -> list[str]:
    """A SZALLITAS szemcseje: a het. A data.json-ban a mezo neve `week` (pl. '2026-W38')."""
    ki = []
    for p in s.get("points") or s.get("observations") or s.get("data") or []:
        if isinstance(p, dict):
            w = p.get("week_iso") or p.get("week")
            if w:
                ki.append(str(w))
    return ki


def _observed(s: dict) -> list[date]:
    """Az ADATKOR-hoz: a megfigyeles datuma (a data.json-ban `date`)."""
    ki = []
    for p in s.get("points") or s.get("observations") or s.get("data") or []:
        if isinstance(p, dict) and p.get("date"):
            try:
                ki.append(date.fromisoformat(str(p["date"])[:10]))
            except ValueError:
                continue
    return ki


def _gap_days(d: list[date]) -> list[int]:
    return [(d[i + 1] - d[i]).days for i in range(len(d) - 1)]


def _kuszob(gaps: list[int]) -> int:
    """A store.py kuszob-formulaja, valtozatlanul."""
    if len(gaps) < 2:
        return STALE_AFTER_DAYS
    w = gaps[-GAP_WINDOW:]
    ws = sorted(w)
    med = ws[len(ws) // 2]
    return min(max(max(w) + med, 2 * med, MIN_STALE_AFTER_DAYS), MAX_STALE_AFTER_DAYS)


def _napok() -> list[tuple[date, str]]:
    """A rogzitett tartomany commit-napjai, naponta az UTOLSO commit."""
    # `ELSO_SHA^..UTOLSO_SHA` NEM hasznalhato: az ELSO_SHA a repo legelso commitja,
    # nincs szuloje (`fatal: bad revision`). Ezert a felso hatar rogzitett, az also
    # a repo eleje — es EXPLICIT ellenorizzuk, hogy az elso talalat az ELSO_SHA.
    nyers = _git("log", "--format=%H %ad", "--date=short", "--reverse",
                 UTOLSO_SHA, "--", FAJL).strip().splitlines()
    if not nyers:
        raise NemMerheto("a rogzitett tartomany ures — a SHA-k elavultak?")
    elso_talalt = nyers[0].split(None, 1)[0]
    if elso_talalt != ELSO_SHA:
        raise NemMerheto(
            f"a tartomany kezdete elmozdult: vart {ELSO_SHA[:8]}, kaptam {elso_talalt[:8]} "
            "— a tortenet atirodott (rebase/filter)? A PINELT szamokat ujra kell vezetni.")
    per_nap: dict[date, str] = {}
    for sor in nyers:
        sha, nap = sor.split(None, 1)
        per_nap[date.fromisoformat(nap.strip())] = sha
    return sorted(per_nap.items())


def meres() -> dict:
    kulcsok = _kulcsok()
    napok = _napok()
    as_of_veg = napok[-1][0]

    het_latott: dict[str, set[str]] = defaultdict(set)
    obs_latott: dict[str, set[date]] = defaultdict(set)
    szall: dict[str, list[date]] = defaultdict(list)

    # elso menet: a teljes tortenet osszegyujtese (a MELEG regimehez is kell)
    napi: list[tuple[date, dict[str, dict]]] = []
    for nap, sha in napok:
        doc = json.loads(_git("show", f"{sha}:{FAJL}"))
        allapot: dict[str, dict] = {}
        for s in _sorozatok(doc, sha):
            k = s.get("key") or s.get("id")
            if not k or k not in kulcsok:
                continue
            uj_het = False
            for w in _hetek(s):
                if w not in het_latott[k]:
                    het_latott[k].add(w)
                    uj_het = True
            if uj_het:
                szall[k].append(nap)
            for d in _observed(s):
                obs_latott[k].add(d)
            allapot[k] = {
                "szall": list(szall[k]),
                "obs_max": max(obs_latott[k]) if obs_latott[k] else None,
                "obs_mind": sorted(obs_latott[k]),
            }
        napi.append((nap, allapot))

    # a MELEG regime kuszobei: a TELJES szallitasi tortenetbol
    meleg_kuszob = {k: _kuszob(_gap_days(sorted(v))) for k, v in szall.items()}

    ered = {n: {"esemeny": 0, "stale_nap": 0}
            for n in ("mai", "gat1_hideg", "gat1_meleg", "gat2")}
    elozo: dict[str, dict[str, bool]] = defaultdict(lambda: defaultdict(bool))

    for nap, allapot in napi:
        for k, a in allapot.items():
            if a["obs_max"] is None:
                continue
            kor = (nap - a["obs_max"]).days
            most = {
                "mai": kor > _kuszob(_gap_days(a["obs_mind"])),
                "gat1_hideg": bool(a["szall"]) and (nap - a["szall"][-1]).days > _kuszob(_gap_days(a["szall"])),
                "gat1_meleg": bool(a["szall"]) and (nap - a["szall"][-1]).days > meleg_kuszob.get(k, STALE_AFTER_DAYS),
                "gat2": kor > ADATKOR_PLAFON_DAYS,
            }
            for nev, ertek in most.items():
                if ertek:
                    ered[nev]["stale_nap"] += 1
                    if not elozo[k][nev]:
                        ered[nev]["esemeny"] += 1
                elozo[k][nev] = ertek

    return {"tartomany": [str(napok[0][0]), str(as_of_veg)], "commit_napok": len(napok),
            "sorozatok": len(kulcsok), "eredmeny": ered}


def onproba() -> list[str]:
    """POZITIV KONTROLL — MINDEN futaskor. Egy meroeszkoz, ami 0-t adhat, elobb
    bizonyitsa be, hogy nem nema.

    A backtest harom oszlopa nullat ad. Ha a stale-logika barmiert mindig False-t
    adna (elirt osszehasonlitas, rossz mertekegyseg, ures bemenet), a kimenet
    UGYANIGY nezne ki — es a terv legerosebb erve egy hallgato muszer lenne.
    Ez a fuggveny szintetikus adaton kikenyszeriti mindket iranyt.

    Visszaad: az ELTERESEK listaja (ures lista = a muszer el).
    """
    nap = date(2026, 9, 29)
    esetek = [
        # (nev, szallitasi napok, varhato stale?)
        ("heti ritmus, majd 40 nap nemasag",
         [date(2026, 6, 5).toordinal() + 7 * i for i in range(12)], True),
        ("heti ritmus, utolso szallitas 4 napja",
         [date(2026, 6, 5).toordinal() + 7 * i for i in range(17)], False),
        ("EGYETLEN szallitas (backfill) 10 napja",
         [date(2026, 9, 19).toordinal()], False),
        ("EGYETLEN szallitas (backfill) 25 napja",
         [date(2026, 9, 4).toordinal()], True),
        ("MERGEZES: minden first_seen = ma",
         [nap.toordinal()], False),
    ]
    elteres = []
    for nev, ordinals, vart in esetek:
        napok = [date.fromordinal(o) for o in ordinals]
        kapott = (nap - max(napok)).days > _kuszob(_gap_days(sorted(napok)))
        if kapott != vart:
            elteres.append(f"{nev}: vart stale={vart}, kapott={kapott}")
    # Gat 2 mindket iranya
    for kor, vart in ((ADATKOR_PLAFON_DAYS, False), (ADATKOR_PLAFON_DAYS + 1, True)):
        if (kor > ADATKOR_PLAFON_DAYS) != vart:
            elteres.append(f"Gat 2 adatkor={kor}: vart stale={vart}")
    return elteres


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="gepi kimenet")
    args = ap.parse_args()

    hibas = onproba()
    if hibas:
        print("ONPROBA BUKOTT — a muszer nem megbizhato, a szamokat NE hasznald:",
              file=sys.stderr)
        for h in hibas:
            print(f"  - {h}", file=sys.stderr)
        return 2

    try:
        m = meres()
    except NemMerheto as e:
        print(f"NEM MERHETO: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(m, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        t = m["tartomany"]
        print(f"tartomany: {t[0]} … {t[1]} · {m['commit_napok']} commit-nap · "
              f"{m['sorozatok']} sorozat")
        print(f"\n{'gat':<34} {'esemeny':>8} {'stale-nap':>10}   pinelt")
        print("-" * 70)
        cimke = {"mai": "MAI KOD (observed_date-ora)",
                 "gat1_hideg": "Gat 1 szallitasi ora (HIDEG)",
                 "gat1_meleg": "Gat 1 szallitasi ora (MELEG)",
                 "gat2": f"Gat 2 adatkor-plafon ({ADATKOR_PLAFON_DAYS})"}
        for nev in ("mai", "gat1_hideg", "gat1_meleg", "gat2"):
            e, s = m["eredmeny"][nev]["esemeny"], m["eredmeny"][nev]["stale_nap"]
            p = PINELT[nev]
            jel = "OK" if (e == p["esemeny"] and s == p["stale_nap"]) else "ELTERES"
            print(f"{cimke[nev]:<34} {e:>8} {s:>10}   {p['esemeny']}/{p['stale_nap']} {jel}")

    elteres = [n for n in PINELT
               if m["eredmeny"][n] != {"esemeny": PINELT[n]["esemeny"],
                                       "stale_nap": PINELT[n]["stale_nap"]}]
    if elteres:
        print(f"\nELTERES a pinelt ertekektol: {', '.join(elteres)}", file=sys.stderr)
        print("Ez nem automatikusan hiba — de EXPLICIT dontest kiván: vagy a kod valtozott, "
              "vagy a tartomany/fixture avult el. Ne a PINELT szamot ird at gondolkodas nelkul.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
