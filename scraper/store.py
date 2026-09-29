from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .sources import CATEGORY_CONFIG, EXPORT_UNIT
except ImportError:  # direct script execution
    from sources import CATEGORY_CONFIG, EXPORT_UNIT


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "eggprices.db"
EXPORT_PATH = PROJECT_ROOT / "dashboard" / "data.json"

# Hany nap utan szamit egy sorozat elavultnak.
#
# MERVE 2026-09-14 az elo DB-n (50 sorozat, teljes tortenet): a kozlesek median
# koze 7 nap, de a sorozatonkenti LEGNAGYOBB termeszetes res 14 nap (rungis), es
# a 95. percentilis is 14. Egy 14 napos kuszob tehat PONTOSAN a tortenelmi
# maximumon ulne -> garantalt fals riasztas. 21 nap = egy teljes cikluspnyi
# margo a mert maximum folott, es egy tenylegesen befagyott forrast (60+ nap)
# tovabbra is hamar elkap.
# Tartalek kuszob azoknak a sorozatoknak, amiknek nincs eleg tortenete sajat
# kuszob szamitasahoz (2-nel kevesebb res).
STALE_AFTER_DAYS = 21
# Also korlat a sorozatonkenti kuszobre — egy nagyon suru sorozat se riasszon
# mar par nap utan.
MIN_STALE_AFTER_DAYS = 10
# Felso korlat: barmennyire is ritkul egy forras, 30 nap utan szolunk.
MAX_STALE_AFTER_DAYS = 30
# Ennyi legutobbi resbol szamolunk. Heti kozlesnel ez kb. fel ev — eleg hosszu,
# hogy a valodi ritmust lassa, es eleg rovid, hogy egy regi kieses kioregedjen.
GAP_WINDOW = 26

# ── GAT 2: ADATKOR-PLAFON (2026-09-29) ─────────────────────────────────────
# Fix, NEM tanult konstans. Arra valaszol, amire a szallitasi gat vak: a forras
# pontosan szallit, de egyre regebbi adatot.
#
# Miert 20: a mert legnagyobb NORMAL adatkor 17 (eu_whole_broiler_65, es nem
# egyszeri — 115 napbol 13-on 17, 29-en >=16, tehat hetente visszater). Egy 18-as
# plafon 1 nap margoval ulne a tortenelmi csucson: pontosan az az alakzat, amit
# fent a STALE_AFTER_DAYS kommentje elitel. 20-nal az EU margoja 3, a masik 49
# sorozate >=8. A backtest 18/19/20/21-nel egyarant 0 esemenyt ad — a 20 tehat
# ingyen vesz margot. Ellenorzese: scraper/backtest_frissesseg.py
#
# ELAVULAS-JELZES: ha egy sorozat a plafon 2 napos korzetebe er, az DEGRADED
# (uzemeltetesi hir), nem ALERT — l. a `figyelmeztetes` mezot a series_freshness-ben.
ADATKOR_PLAFON_DAYS = 20
ADATKOR_FIGYELMEZTETES_NAP = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  label TEXT NOT NULL,
  country TEXT,
  category TEXT NOT NULL DEFAULT 'kelteto',
  size TEXT,
  color TEXT,
  unit TEXT DEFAULT 'EUR/100',
  source_url TEXT,
  UNIQUE(key, size, color)
);

CREATE TABLE IF NOT EXISTS observation (
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  week_iso TEXT NOT NULL,
  observed_date TEXT,
  price REAL NOT NULL,
  change REAL,
  native_price REAL,
  native_unit TEXT,
  fx_rate REAL,
  fx_rate_unit TEXT,
  fx_rate_date TEXT,
  fx_source TEXT,
  fetched_at TEXT NOT NULL,
  raw TEXT,
  UNIQUE(series_id, week_iso)
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(series)")}
    if "category" not in columns:
        conn.execute(
            "ALTER TABLE series ADD COLUMN category TEXT NOT NULL DEFAULT 'kelteto'"
        )
    observation_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(observation)")
    }
    optional_columns = {
        "native_price": "REAL",
        "native_unit": "TEXT",
        "fx_rate": "REAL",
        "fx_rate_unit": "TEXT",
        "fx_rate_date": "TEXT",
        "fx_source": "TEXT",
        # 2026-09-29 — a SZALLITASI ORA. Ket kulon mennyiseg kell:
        #   first_seen_at  : mikor lattuk ELOSZOR ezt a hetet (YYYY-MM-DD, Europe/Bucharest)
        #   first_seen_src : 'git' (visszamenoleges rekonstrukcio) | 'scrape' (elo futas)
        # A `fetched_at` erre NEM hasznalhato: az upsert MINDEN futasnal felulirja
        # (merve: az eu_whole_broiler_65 mind a 68 sora azonos fetched_at-et visel).
        # A provenance nem dekoracio: a rekonstrukcio ERTEK-alapon javit, es ebbol
        # tudjuk, hogy egy datum honnan szarmazik.
        "first_seen_at": "TEXT",
        "first_seen_src": "TEXT",
    }
    for name, sql_type in optional_columns.items():
        if name not in observation_columns:
            conn.execute(f"ALTER TABLE observation ADD COLUMN {name} {sql_type}")
    conn.commit()


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def get_or_create_series(
    conn: sqlite3.Connection,
    *,
    key: str,
    label: str,
    country: str,
    category: str,
    size: str | None,
    color: str | None,
    unit: str,
    source_url: str,
) -> int:
    row = conn.execute(
        """
        SELECT id FROM series
        WHERE key = ?
          AND size IS ?
          AND color IS ?
        """,
        (key, size, color),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE series
            SET label = ?, country = ?, category = ?, unit = ?, source_url = ?
            WHERE id = ?
            """,
            (label, country, category, unit, source_url, row["id"]),
        )
        return int(row["id"])

    cur = conn.execute(
        """
        INSERT INTO series (
          key, label, country, category, size, color, unit, source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, label, country, category, size, color, unit, source_url),
    )
    return int(cur.lastrowid)


def upsert_observation(
    conn: sqlite3.Connection,
    *,
    series_id: int,
    week_iso: str,
    observed_date: str | None,
    price: float,
    change: float | None,
    native_price: float | None,
    native_unit: str | None,
    fx_rate: float | None,
    fx_rate_unit: str | None,
    fx_rate_date: str | None,
    fx_source: str | None,
    fetched_at: str,
    raw: Any,
    first_seen_at: str | None = None,
) -> None:
    """A `first_seen_at` a SZALLITAS oraja — es SOHA nem irodik felul.

    Harom eset, mind AC (AC-23):
      1. uj (series_id, week_iso) sor  -> first_seen_at = a futas Europe/Bucharest
         datuma, first_seen_src = 'scrape'
      2. ugyanazon het ujra-upsertje   -> mindket mezo VALTOZATLAN (COALESCE)
      3. kovetkezo uj het              -> sajat, uj first-seen datumot kap

    Miert kritikus a 2. pont: enelkul minden napi futas nullazna az orat, es a
    szallitasi gat sosem latna reseket. A `fetched_at` pont ezt csinalja (mindig
    felulirodik) — ezert nem hasznalhato szallitasi bizonyitekkent.
    """
    if first_seen_at is None:
        first_seen_at = datetime.now(ZoneInfo("Europe/Bucharest")).date().isoformat()
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=_json_default)
    conn.execute(
        """
        INSERT INTO observation (
          series_id, week_iso, observed_date, price, change,
          native_price, native_unit, fx_rate, fx_rate_unit, fx_rate_date, fx_source,
          fetched_at, raw, first_seen_at, first_seen_src
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scrape')
        ON CONFLICT(series_id, week_iso) DO UPDATE SET
          observed_date = excluded.observed_date,
          price = excluded.price,
          change = excluded.change,
          native_price = excluded.native_price,
          native_unit = excluded.native_unit,
          fx_rate = excluded.fx_rate,
          fx_rate_unit = excluded.fx_rate_unit,
          fx_rate_date = excluded.fx_rate_date,
          fx_source = excluded.fx_source,
          fetched_at = excluded.fetched_at,
          raw = excluded.raw,
          -- A SZALLITASI ORA NEM MOZDUL: a COALESCE az elso beirast orzi meg.
          -- (A `fetched_at` folotte a szandekosan frissulo mezo — a ketto kulonbsege
          --  pont az, ami a publikalasi kesest lathatova teszi.)
          first_seen_at = COALESCE(observation.first_seen_at, excluded.first_seen_at),
          first_seen_src = COALESCE(observation.first_seen_src, excluded.first_seen_src)
        """,
        (
            series_id, week_iso, observed_date, price, change,
            native_price, native_unit, fx_rate, fx_rate_unit, fx_rate_date, fx_source,
            fetched_at, raw_json, first_seen_at,
        ),
    )


def _store_one(conn: sqlite3.Connection, obs: dict[str, Any]) -> None:
    """Egyetlen megfigyeles beirasa. Kulon fuggveny, hogy a hivo oldalon a
    hibakezeles ne tordelje szet a behuzast."""
    series_id = get_or_create_series(
        conn,
        key=obs["key"],
        label=obs["label"],
        country=obs.get("country"),
        category=obs["category"],
        size=obs.get("size"),
        color=obs.get("color"),
        unit=obs.get("unit") or EXPORT_UNIT,
        source_url=obs.get("source_url") or "",
    )
    upsert_observation(
        conn,
        series_id=series_id,
        week_iso=obs["week_iso"],
        observed_date=obs.get("observed_date"),
        price=float(obs["price"]),
        change=obs.get("change"),
        native_price=obs.get("native_price"),
        native_unit=obs.get("native_unit"),
        fx_rate=obs.get("fx_rate"),
        fx_rate_unit=obs.get("fx_rate_unit"),
        fx_rate_date=obs.get("fx_rate_date"),
        fx_source=obs.get("fx_source"),
        fetched_at=obs["fetched_at"],
        raw=obs,
    )


def store_observations(
    observations: list[dict[str, Any]],
    db_path: Path = DB_PATH,
) -> tuple[int, list[str]]:
    """Visszaad: (elmentett darabszam, kihagyott sorok leirasa).

    RED1 N-2 (2026-09-14): a kihagyott sorokat VISSZA KELL ADNI, nem csak
    naplozni. Enelkul a hivo nem tudja megkulonboztetni azt, hogy "nem kinaltak
    sort" attol, hogy "2700-at kinaltak es 2700-at elutasitottunk" — az elso
    valtozat ebbol nema sikert csinalt egy teljes napi adatvesztesbol.
    """
    with connect(db_path) as conn:
        init_db(conn)
        count = 0
        skipped: list[str] = []
        for obs in observations:
            try:
                _store_one(conn, obs)
                count += 1
            except Exception as exc:  # noqa: BLE001 - szandekos: egy rossz sor
                # nem dobhatja el a tobbi forras koteget. A hivo a visszaadott
                # listabol tudja meg, hogy tortent-e veszteseg.
                skipped.append(f"{obs.get('key')}/{obs.get('week_iso')}: {exc}")
        if skipped:
            print(
                f"warn: {len(skipped)} observation(s) skipped: "
                + "; ".join(skipped[:5]),
                file=sys.stderr,
            )
        conn.commit()
    return count, skipped


def _gap_days(days: list[date]) -> list[int]:
    return [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]


def _kuszob_resekbol(gaps: list[int]) -> int:
    """A kuszob-formula a SZALLITASI oranak (a kozlesi aganak sajat inline masolata van).

    ⚠️ A docstring elso valtozata azt allitotta, hogy a formula "EGY helyen" el —
    ez HAMIS volt: a kozlesi kuszobot tovabbra is a series_freshness inline
    szamitasa hajtja. A kiemeles tehat DUPLIKALT, nem unifikalt. Szandekosan igy
    marad egyelore: a kozlesi ag leptetese kulon valtozas, es a terv nem kerte.
    Drift-kockazat: ha az egyiket modositod, a masikat is kell.

    Szandekosan ugyanaz a keplet, mint amit a series_freshness 2026-09-14 ota hasznal
    (RED1 N-4 csuszoablak + felso korlat): csak a BEMENET mas (szallitasi resek vs
    kozlesi resek). Igy a szallitasi gat nem uj heurisztika, hanem ugyanaz a mar
    atnezett szamitas egy tisztabb oran.
    """
    if len(gaps) < 2:
        return STALE_AFTER_DAYS
    window = gaps[-GAP_WINDOW:]
    ws = sorted(window)
    median = ws[len(ws) // 2]
    return min(max(max(window) + median, 2 * median, MIN_STALE_AFTER_DAYS),
               MAX_STALE_AFTER_DAYS)


def series_freshness(
    db_path: Path = DB_PATH,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Sorozatonkenti frissesseg, KOZVETLENUL a DB-bol.

    RED1 N-2: ez szandekosan NEM fugg az exporttol. Az elso valtozatban az
    elavultsagot csak az export agon szamoltuk, ezert pont akkor voltunk vakok
    ra, amikor a rendszer a leginkabb romlott (nem mentodott semmi -> nincs
    export -> nincs frissesseg-adat -> "minden rendben").

    A kuszob SOROZATONKENTI (RED1 Q3): a globalis 21 nap a legrosszabbul
    viselkedo sorozat (rungis, 14 napos termeszetes res) margojat adta mind az
    50-nek, holott 46-nak a legnagyobb termeszetes rese 8 nap. Sajat tortenetbol:
    `max_res + median_res`, alsó korlattal.
    """
    as_of = as_of or datetime.now(ZoneInfo("Europe/Bucharest")).date()
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id AS series_id, s.key, s.label, s.size, s.color,
                   o.observed_date, o.first_seen_at, o.first_seen_src
            FROM series s
            JOIN observation o ON o.series_id = s.id
            WHERE o.observed_date IS NOT NULL
            ORDER BY s.id, o.observed_date
            """,
        ).fetchall()

    per: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = per.setdefault(
            row["series_id"],
            {"key": row["key"], "label": row["label"],
             "size": row["size"], "color": row["color"], "days": [],
             "szallitas": set(), "fedetlen": 0, "utolso_keses": None},
        )
        try:
            entry["days"].append(date.fromisoformat(row["observed_date"]))
        except ValueError:
            continue
        # A LEFEDETTSEG nem a mezo letezese, hanem az ERVENYESSEGE: szabalyos datum,
        # ismert provenance, es a ket mezo egyutt van jelen. Egy fel-NULL par vagy egy
        # ismeretlen forras-cimke NEM szamit lefedettnek — kulonben egy hibas sor
        # csendben elesitene a gatat.
        fs, src = row["first_seen_at"], row["first_seen_src"]
        if not fs or src not in ("git", "scrape"):
            entry["fedetlen"] += 1
            continue
        try:
            fs_d = date.fromisoformat(str(fs))
        except (ValueError, TypeError):
            entry["fedetlen"] += 1
            continue
        entry["szallitas"].add(fs_d)
        try:
            obs_d = date.fromisoformat(row["observed_date"])
        except ValueError:
            obs_d = None
        if obs_d is not None:
            elozo = entry["utolso_keses"]
            if elozo is None or fs_d >= elozo[0]:
                entry["utolso_keses"] = (fs_d, (fs_d - obs_d).days)

    out: list[dict[str, Any]] = []
    for entry in per.values():
        days = sorted(set(entry["days"]))
        if not days:
            continue
        gaps = _gap_days(days)
        if len(gaps) >= 2:
            # RED1 N-4 (2026-09-14): a `max` a TELJES tortenetre monoton — egy
            # regi, mar meggyogyult 60 napos kieses orokre 49 napra fujta volna
            # a kuszobot, es soha nem allt volna vissza ("a kuszob megtanulja a
            # sajat romlasat"). Ezert CSUSZOABLAK: csak az utolso GAP_WINDOW res
            # szamit, igy a regi kieses kioregszik. Es FELSO KORLAT, hogy egy
            # lassan ritkulo forras se tolhassa a detektalast a vegtelenbe.
            window = gaps[-GAP_WINDOW:]
            window_sorted = sorted(window)
            median = window_sorted[len(window_sorted) // 2]
            threshold = max(max(window) + median, 2 * median, MIN_STALE_AFTER_DAYS)
            threshold = min(threshold, MAX_STALE_AFTER_DAYS)
        else:
            threshold = STALE_AFTER_DAYS
        parts = [entry["key"]]
        if entry["size"]:
            parts.append(str(entry["size"]))
        if entry["color"]:
            parts.append(str(entry["color"]))
        # ── GAT 1: SZALLITASI ORA — "a forras elhallgatott" ────────────────
        # `as_of - max(first_seen_at)`: hany napja nem erkezett UJ het.
        # A kuszob a SZALLITASI resekbol tanul, ugyanazzal a formulaval.
        # Miert nem az observed_date-bol: az a KOZLESI ritmust meri, a `stale`
        # viszont a mai naptol, amibe a publikalasi keses is beleszamit —
        # ket ora keveredett egy osszehasonlitasban. Ez volt a gyoker.
        szall = sorted(entry["szallitas"])
        lefedett = entry["fedetlen"] == 0 and len(szall) > 0
        if lefedett:
            szall_gaps = _gap_days(szall)
            szall_threshold = _kuszob_resekbol(szall_gaps)
            szall_kor = (as_of - szall[-1]).days
            stale_delivery = szall_kor > szall_threshold
        else:
            # Hianyos lefedettseg -> a gat NEM elesedik erre a sorozatra, DE a
            # gyujtes fut tovabb es a hiany NEVESITVE latszik (nem csendes kihagyas).
            szall_threshold = szall_kor = None
            stale_delivery = False

        # ── GAT 2: ADATKOR-PLAFON — "a forras regi adatot ad" ──────────────
        kor = (as_of - days[-1]).days
        stale_age = kor > ADATKOR_PLAFON_DAYS
        # elavulas-jelzes: a fix konstans nem nemulhat el, ha a valosag elmozdul
        plafon_kozel = (not stale_age) and kor > ADATKOR_PLAFON_DAYS - ADATKOR_FIGYELMEZTETES_NAP

        # A KET JEL KULON CSELEKVEST kiván, ezert kulon is jelenik meg. Az
        # osszevonas (`stale1 or stale2`) pont a cselekves-hataron tuntetne el a
        # kulonbseget: "elhallgatott" vs "regi adatot ad" mas valaszt kér.
        # MINDKET ok latszik, ha mindketto all. A `reason` az ELSODLEGES (a
        # cselekveshez ez kell), a `reasons` a teljes kep. Az elsobbseg a
        # szallitase: "a forras elhallgatott" konkretabb es surgetobb hir, mint
        # "regi az adat" — utobbi gyakran az elobbi KOVETKEZMENYE. A teszt
        # talalta meg, hogy egy sima `reason` mezoben az egyik jel nyom nelkul
        # eltunik, pont azon a hataron, aminek a szetvalasztasaert az egesz
        # atalakitas keszult.
        reasons = ([("delivery")] if stale_delivery else []) + (["age"] if stale_age else [])
        reason = reasons[0] if reasons else None
        keses = entry["utolso_keses"][1] if entry["utolso_keses"] else None
        out.append(
            {
                "key": "__".join(parts),
                "label": entry["label"],
                "updated_through": days[-1].isoformat(),
                "days_since_update": kor,
                "stale_after_days": threshold,
                "stale": stale_delivery or stale_age,
                "reason": reason,
                "reasons": reasons,
                # szallitasi ora
                "delivery_through": szall[-1].isoformat() if szall else None,
                "days_since_delivery": szall_kor,
                "delivery_stale_after_days": szall_threshold,
                # adatkor
                "age_ceiling_days": ADATKOR_PLAFON_DAYS,
                "age_ceiling_near": plafon_kozel,
                # megfigyelhetoseg: a publikalasi keses NEM riaszt, de latszik.
                # Negativ ertek (elore datumozott kozles, merve -13-ig) NEM
                # exportalhato ertekkent: kizarjuk es szamoljuk.
                "publication_lag_days": keses if (keses is not None and keses >= 0) else None,
                "publication_lag_negative": bool(keses is not None and keses < 0),
                "first_seen_coverage_missing": entry["fedetlen"],
            }
        )
    out.sort(key=lambda item: item["days_since_update"], reverse=True)
    return out


def export_data_json(
    db_path: Path = DB_PATH,
    output_path: Path = EXPORT_PATH,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              s.id AS series_id,
              s.key, s.label, s.country, s.category, s.size, s.color, s.unit,
              o.week_iso, o.observed_date, o.price, o.change,
              o.native_price, o.native_unit, o.fx_rate, o.fx_rate_unit,
              o.fx_rate_date, o.fx_source
            FROM series s
            JOIN observation o ON o.series_id = s.id
            ORDER BY s.category, s.key, s.size, s.color, o.week_iso
            """,
        ).fetchall()

    series_map: dict[int, dict[str, Any]] = {}
    series_categories: dict[int, str] = {}
    for row in rows:
        size = row["size"]
        color = row["color"]
        parts = [row["key"]]
        if size:
            parts.append(str(size))
        if color:
            parts.append(str(color))
        export_key = "__".join(parts)

        entry = series_map.setdefault(
            row["series_id"],
            {
                "key": export_key,
                "label": row["label"],
                "country": row["country"],
                "size": size,
                "color": color,
                "unit": row["unit"],
                "points": [],
            },
        )
        series_categories[row["series_id"]] = row["category"]
        point = {
            "week": row["week_iso"],
            "date": row["observed_date"],
            "price": row["price"],
            "change": row["change"],
        }
        for field in (
            "native_price",
            "native_unit",
            "fx_rate",
            "fx_rate_unit",
            "fx_rate_date",
            "fx_source",
        ):
            if row[field] is not None:
                point[field] = row[field]
        entry["points"].append(point)

    categories = []
    for category_key, config in CATEGORY_CONFIG.items():
        category_series = [
            series
            for series_id, series in series_map.items()
            if series_categories[series_id] == category_key
        ]
        categories.append(
            {
                "key": category_key,
                "label": config["label"],
                "default_unit": config["default_unit"],
                "series": category_series,
            }
        )

    # Frissesseg — kimondva, nem elrejtve. Az export a DB-bol dolgozik, ezert
    # szerkezetileg mindig teljes; ettol meg egy-egy sorozat lehet regi. Ha ezt
    # nem irjuk ki, a regi adat frissnek latszik (hamis zold).
    # EGY igazsag-forras: ugyanaz a series_freshness(), amit a scraper is hiv —
    # igy a felulet es a kilepesi kod nem mondhat mast.
    as_of = datetime.now(ZoneInfo("Europe/Bucharest")).date()
    freshness_rows = series_freshness(db_path, as_of=as_of)
    by_key = {row["key"]: row for row in freshness_rows}
    for series in series_map.values():
        row = by_key.get(series["key"])
        series["updated_through"] = row["updated_through"] if row else None
        series["days_since_update"] = row["days_since_update"] if row else None
        series["stale_after_days"] = row["stale_after_days"] if row else None
    # A KET JEL KULON: a `reason` megmondja, MELYIK gat szolalt meg, mert a
    # ket eset mas valaszt kér. "delivery" = a forras elhallgatott (nezd meg a
    # forrast/parsert). "age" = pontosan szallit, de egyre regebbi adatot
    # (a forras publikalasi rendje valtozott).
    stale = [
        {
            "key": row["key"],
            "label": row["label"],
            "updated_through": row["updated_through"],
            "days_since_update": row["days_since_update"],
            "stale_after_days": row["stale_after_days"],
            "reason": row["reason"],
            "reasons": row["reasons"],
            "days_since_delivery": row["days_since_delivery"],
            "delivery_stale_after_days": row["delivery_stale_after_days"],
        }
        for row in freshness_rows
        if row["stale"]
    ]
    # ELAVULAS-JELZES es LEFEDETTSEG — uzemeltetesi hir, NEM riasztas.
    # A fix adatkor-plafon elavulhat (egy forras legitim modon lassulhat); ha egy
    # sorozat a plafon kozeleben jar, azt LATNI kell, mielott fals riasztas lesz belole.
    # A hianyos lefedettsegu sorozatra a szallitasi gat NEM elesedik — ez sem maradhat
    # csendes: nevesitve, nem csak egy szamkent.
    plafon_kozel = sorted(r["key"] for r in freshness_rows if r["age_ceiling_near"])
    fedetlen = sorted(r["key"] for r in freshness_rows if r["first_seen_coverage_missing"])

    payload = {
        "generated_at": datetime.now(ZoneInfo("Europe/Bucharest")).isoformat(timespec="seconds"),
        "schema_version": "0.5",
        "freshness": {
            "as_of": as_of.isoformat(),
            # sorozatonkenti kuszob van; ez csak a tartalek-ertek azoknak,
            # amiknek nincs eleg tortenete
            "fallback_stale_after_days": stale_after_days,
            "series_total": len(series_map),
            "series_stale": stale,
            "age_ceiling_days": ADATKOR_PLAFON_DAYS,
            "series_near_age_ceiling": plafon_kozel,
            "series_without_delivery_history": fedetlen,
        },
        "categories": categories,
    }
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(output_path)
    return payload


def main() -> int:
    with connect(DB_PATH) as conn:
        init_db(conn)
    payload = export_data_json(DB_PATH, EXPORT_PATH)
    count = sum(len(category["series"]) for category in payload["categories"])
    print(f"exported {count} series in {len(payload['categories'])} categories to {EXPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
