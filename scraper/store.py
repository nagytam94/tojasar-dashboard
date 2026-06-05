from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime
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
    fetched_at: str,
    raw: Any,
) -> None:
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=_json_default)
    conn.execute(
        """
        INSERT INTO observation (
          series_id, week_iso, observed_date, price, change, fetched_at, raw
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(series_id, week_iso) DO UPDATE SET
          observed_date = excluded.observed_date,
          price = excluded.price,
          change = excluded.change,
          fetched_at = excluded.fetched_at,
          raw = excluded.raw
        """,
        (series_id, week_iso, observed_date, price, change, fetched_at, raw_json),
    )


def store_observations(
    observations: list[dict[str, Any]],
    db_path: Path = DB_PATH,
) -> int:
    with connect(db_path) as conn:
        init_db(conn)
        count = 0
        for obs in observations:
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
                fetched_at=obs["fetched_at"],
                raw=obs,
            )
            count += 1
        conn.commit()
    return count


def export_data_json(
    db_path: Path = DB_PATH,
    output_path: Path = EXPORT_PATH,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              s.id AS series_id,
              s.key, s.label, s.country, s.category, s.size, s.color, s.unit,
              o.week_iso, o.observed_date, o.price, o.change
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
        entry["points"].append(
            {
                "week": row["week_iso"],
                "date": row["observed_date"],
                "price": row["price"],
                "change": row["change"],
            }
        )

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

    payload = {
        "generated_at": datetime.now(ZoneInfo("Europe/Bucharest")).isoformat(timespec="seconds"),
        "schema_version": "0.3",
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
