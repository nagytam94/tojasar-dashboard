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
) -> None:
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=_json_default)
    conn.execute(
        """
        INSERT INTO observation (
          series_id, week_iso, observed_date, price, change,
          native_price, native_unit, fx_rate, fx_rate_unit, fx_rate_date, fx_source,
          fetched_at, raw
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
          raw = excluded.raw
        """,
        (
            series_id, week_iso, observed_date, price, change,
            native_price, native_unit, fx_rate, fx_rate_unit, fx_rate_date, fx_source,
            fetched_at, raw_json,
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
                   o.observed_date
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
             "size": row["size"], "color": row["color"], "days": []},
        )
        try:
            entry["days"].append(date.fromisoformat(row["observed_date"]))
        except ValueError:
            continue

    out: list[dict[str, Any]] = []
    for entry in per.values():
        days = sorted(set(entry["days"]))
        if not days:
            continue
        gaps = _gap_days(days)
        if len(gaps) >= 2:
            gaps_sorted = sorted(gaps)
            median = gaps_sorted[len(gaps_sorted) // 2]
            threshold = max(max(gaps) + median, 2 * median, MIN_STALE_AFTER_DAYS)
        else:
            threshold = STALE_AFTER_DAYS
        parts = [entry["key"]]
        if entry["size"]:
            parts.append(str(entry["size"]))
        if entry["color"]:
            parts.append(str(entry["color"]))
        out.append(
            {
                "key": "__".join(parts),
                "label": entry["label"],
                "updated_through": days[-1].isoformat(),
                "days_since_update": (as_of - days[-1]).days,
                "stale_after_days": threshold,
                "stale": (as_of - days[-1]).days > threshold,
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
    stale = [
        {
            "key": row["key"],
            "label": row["label"],
            "updated_through": row["updated_through"],
            "days_since_update": row["days_since_update"],
            "stale_after_days": row["stale_after_days"],
        }
        for row in freshness_rows
        if row["stale"]
    ]

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
