#!/usr/bin/env python3
"""A `first_seen_at` visszamenoleges rekonstrukcioja a git-tortenetbol.

MIT CSINAL
----------
A szallitasi ora ahhoz kell, hogy meg tudjuk kulonboztetni a "forras elhallgatott"
esetet attol, hogy "a forras rendben szallit, csak az adat termeszetenel fogva
regebbi". Az elobbi hiba, az utobbi normalis — ma a ketto ugyanugy nez ki.

A DB maga nem tudja, mikor erkezett egy het: a `fetched_at`-et minden napi upsert
felulirja. De a `dashboard/data.json` napi commitjai megorziztek: egy het akkor
"erkezett meg", amikor eloszor megjelent egy commitban.

NEGY DOLOG, AMIT EZ A SCRIPT SZANDEKOSAN MASKEPP CSINAL, MINT AZ ELSO VALTOZATOK
-------------------------------------------------------------------------------
1. **A git-terkep a DB-tranzakcio ELOTT, memoriaban keszul.** Merve: 125 `git show`
   = 2,8 s, a `journal_mode=delete` es a Python sqlite lock-timeoutja 5 s — egy
   `BEGIN IMMEDIATE` alatt futtatott 125 subprocess minden mas irot kizarna, es a
   timeout osszeadodhatna. Subprocess-hiba eseten igy **0 DB-modositas** tortenik.
2. **A javitas ERTEK-alapu, nem provenance-alapu.** A `WHERE first_seen_src <> 'git'`
   alak egy hibas datumot `'git'` markerrel VEGLEGESEN beragasztana. Itt a feltetel
   az elvart ertektol valo elteres (`IS NOT` — SQLite-ban NULL-biztos).
3. **Mind a NEGY data.json-sema kezelve, 0 nema atlepes.** Merve: MISSING=3, 0.3=3,
   0.4=100, 0.5=19. A legelso harom commitban NINCS `categories` kulcs; egy
   `.get("categories", [])` nemán atlepne oket, es pont a legkorabbi datumokat rontana.
4. **A kulcs-bijekcio ellenorzott, elteresnel ABORT.** A `data.json` `key` mezoje MAR
   a kompozit export-kulcs (`barneveldse__48`); aki ujra hozzafuzi a size/color-t,
   annak a JOIN-ja 42/50 sorozatra elbukik (megmerve, elkovetve).

Futtatas:  python3 scraper/migrate_first_seen.py [--apply] [--db PATH]
Alapbol SZARAZ futas. Kilepokod: 0 = kesz/nincs teendo · 1 = elteres · 2 = nem merheto.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAJL = "dashboard/data.json"
DB_ALAP = REPO / "data" / "eggprices.db"
GIT_BUDGET_S = 180.0          # a TELJES git-fazis budgetje, egyben (AC-19)


class NemMerheto(RuntimeError):
    """A migracio nem futhat le megbizhatoan. Sosem 'OK'."""


def _git(args: list[str], hatarido: float) -> str:
    maradek = hatarido - time.monotonic()
    if maradek <= 0:
        raise NemMerheto(f"a git-fazis tullepte a {GIT_BUDGET_S:.0f} s budgetet")
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                       text=True, timeout=maradek, shell=False)
    if r.returncode != 0:
        raise NemMerheto(f"git {' '.join(args)} -> rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def _sorozatok(doc: dict, sha: str) -> list[dict]:
    if "categories" in doc:
        return [s for kat in doc["categories"] for s in kat.get("series", [])]
    if "series" in doc:
        return doc["series"]
    raise NemMerheto(f"ismeretlen data.json alak {sha[:8]}: kulcsok={sorted(doc)[:6]}")


def _hetek(s: dict) -> list[str]:
    ki = []
    for p in s.get("points") or s.get("observations") or s.get("data") or []:
        if isinstance(p, dict):
            w = p.get("week_iso") or p.get("week")
            if w:
                ki.append(str(w))
    return ki


def git_terkep() -> tuple[dict[tuple[str, str], str], dict]:
    """(export_kulcs, week_iso) -> elso megjelenes napja. MINDEN DB-muvelet ELOTT."""
    hatarido = time.monotonic() + GIT_BUDGET_S
    sorok = _git(["log", "--follow", "--format=%H %ad", "--date=short", "--reverse",
                  "--", FAJL], hatarido).strip().splitlines()
    if not sorok:
        raise NemMerheto("nincs data.json-commit — a git-tortenet nem olvashato")

    elso_latas: dict[tuple[str, str], str] = {}
    ertelmezve = 0
    for sor in sorok:
        sha, nap = sor.split(None, 1)
        nap = nap.strip()
        doc = json.loads(_git(["show", f"{sha}:{FAJL}"], hatarido))
        for s in _sorozatok(doc, sha):          # ismeretlen sema -> KIVETEL
            k = s.get("key") or s.get("id")
            if not k:
                raise NemMerheto(f"sorozat kulcs nelkul {sha[:8]}: {sorted(s)[:5]}")
            for w in _hetek(s):
                elso_latas.setdefault((str(k), w), nap)
        ertelmezve += 1

    if ertelmezve != len(sorok):
        raise NemMerheto(f"{ertelmezve}/{len(sorok)} commit ertelmezve — nema atlepes tortent")
    return elso_latas, {"commitok": len(sorok), "ertelmezve": ertelmezve,
                        "parok": len(elso_latas)}


def db_kulcsok(conn: sqlite3.Connection) -> dict[tuple[int, str], str]:
    """(series_id, week_iso) -> export_kulcs. A kulcs-kepzes a store.py-t tukrozi."""
    ki = {}
    for r in conn.execute("""
            SELECT o.series_id, o.week_iso, s.key, s.size, s.color
            FROM observation o JOIN series s ON s.id = o.series_id"""):
        p = [r["key"]]
        if r["size"]:
            p.append(str(r["size"]))
        if r["color"]:
            p.append(str(r["color"]))
        ki[(r["series_id"], r["week_iso"])] = "__".join(p)
    return ki


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="eles iras (alapbol szaraz futas)")
    ap.add_argument("--db", type=Path, default=DB_ALAP)
    args = ap.parse_args()

    # ── 1. FAZIS: git, memoriaban, MINDEN DB-iras elott ─────────────────────
    try:
        terkep, stat = git_terkep()
    except (NemMerheto, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        print(f"NEM MERHETO (git-fazis, 0 DB-modositas tortent): {e}", file=sys.stderr)
        return 2
    print(f"git-terkep: {stat['ertelmezve']}/{stat['commitok']} commit ertelmezve · "
          f"{stat['parok']} (kulcs, het) par")

    # ── 2. FAZIS: SEMA, majd bijekcio-ellenorzes ───────────────────────────
    # A sema-bovites (idempotens ALTER TABLE) CSAK a git-fazis sikere UTAN fut —
    # igy egy git-hiba tenylegesen 0 DB-modositast jelent (AC-19). Az init_db a
    # store.py `optional_columns` mintajat hasznalja, nem kulon ALTER-scriptet (K6).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import store  # noqa: E402  (a git-fazis utan, szandekosan)

    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        store.init_db(conn)
        oszlopok = {r["name"] for r in conn.execute("PRAGMA table_info(observation)")}
        hiany = {"first_seen_at", "first_seen_src"} - oszlopok
        if hiany:
            print(f"ABORT: a sema-bovites utan is hianyzik: {sorted(hiany)}", file=sys.stderr)
            return 2
        sorok = db_kulcsok(conn)
        db_kulcs_keszlet = {k for k in sorok.values()}
        git_kulcs_keszlet = {k for k, _ in terkep}
        csak_git = sorted(git_kulcs_keszlet - db_kulcs_keszlet)
        csak_db = sorted(db_kulcs_keszlet - git_kulcs_keszlet)
        print(f"kulcsok: DB {len(db_kulcs_keszlet)} · git {len(git_kulcs_keszlet)}")
        if csak_git:
            # NEM hiba: pl. atnevezett sorozat regi kulcsa. De NEVESITVE kell latszania.
            print(f"  csak a gitben (atnevezett/megszunt, nem migralunk): {', '.join(csak_git)}")
        if csak_db:
            print(f"ABORT: {len(csak_db)} DB-kulcshoz nincs git-elozmeny: "
                  f"{', '.join(csak_db[:5])}", file=sys.stderr)
            return 1

        hianyzo = [(sid, w) for (sid, w) in sorok if (sorok[(sid, w)], w) not in terkep]
        if hianyzo:
            print(f"ABORT: {len(hianyzo)} (sor, het) parhoz nincs git-elso-latas, "
                  f"pl. {hianyzo[:3]}", file=sys.stderr)
            return 1

        # ── 3. FAZIS: ROVID iro tranzakcio, ERTEK-alapu feltetellel ─────────
        teendo = [(terkep[(sorok[(sid, w)], w)], sid, w) for (sid, w) in sorok]
        if not args.apply:
            # szaraz: megszamoljuk, mi valtozna
            n = 0
            for nap, sid, w in teendo:
                r = conn.execute("SELECT first_seen_at, first_seen_src FROM observation "
                                 "WHERE series_id=? AND week_iso=?", (sid, w)).fetchone()
                if r["first_seen_at"] != nap or r["first_seen_src"] != "git":
                    n += 1
            print(f"SZARAZ FUTAS: {n} sor valtozna ({len(teendo)} vizsgalva). "
                  f"Eles irashoz: --apply")
            return 0

        conn.execute("BEGIN IMMEDIATE")
        cur = conn.executemany(
            """UPDATE observation SET first_seen_at = ?, first_seen_src = 'git'
               WHERE series_id = ? AND week_iso = ?
                 AND (first_seen_at IS NOT ? OR first_seen_src IS NOT 'git')""",
            [(nap, sid, w, nap) for nap, sid, w in teendo])
        valtozott = cur.rowcount
        conn.commit()
        print(f"KESZ: {valtozott} sor modositva ({len(teendo)} vizsgalva)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
