#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import warnings
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests
from bs4 import BeautifulSoup

try:
    from .sources import EXPORT_UNIT, Source, get_sources
    from .store import DB_PATH, EXPORT_PATH, export_data_json, store_observations
except ImportError:  # direct script execution
    from sources import EXPORT_UNIT, Source, get_sources
    from store import DB_PATH, EXPORT_PATH, export_data_json, store_observations


USER_AGENT = "Cloudus-Tojasar-Scraper/0.1 (+local dashboard; contact: owner)"
REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.35
WEEK_RE = re.compile(r"\b(?:wk|week)\s*(\d{1,2})\b", re.IGNORECASE)
DATE_DMY_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})(?:[-/](\d{4}))?")
PRICE_RE = re.compile(r"€\s*([+-]?\d+(?:[.,]\d+)?)")
CHANGE_RE = re.compile(r"€\s*[+-]?\d+(?:[.,]\d+)?\s+([+-]\d+(?:[.,]\d+)?)")
SIZE_TOKENS = {"XL", "L", "M", "S"}
COLOR_TOKENS = {"wit", "bruin"}


def warn(message: str) -> None:
    print(f"warn: {message}", file=sys.stderr)


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def round_change(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)


def scale_price(source: Source, value: float) -> float:
    return round(value * source.price_scale, 6)


def scale_change(source: Source, value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * source.price_scale, 6)


def iso_week_from_date(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def date_from_dutch_date(text: str, default_year: int | None = None) -> date | None:
    match = DATE_DMY_RE.search(text)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3) or default_year or date.today().year)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_info_box(soup: BeautifulSoup) -> dict[str, Any]:
    values = [node.get_text(" ", strip=True) for node in soup.select(".info-box .info-box-value")]
    unit_text = values[0] if len(values) > 0 else ""
    week_text = values[1] if len(values) > 1 else ""
    modified_text = values[2] if len(values) > 2 else ""

    observed = date_from_dutch_date(modified_text)
    week = None
    week_match = WEEK_RE.search(week_text)
    if week_match and observed:
        week = f"{observed.isocalendar().year}-W{int(week_match.group(1)):02d}"
    elif observed:
        week = iso_week_from_date(observed)

    return {
        "unit_text": unit_text,
        "unit": normalize_unit(unit_text),
        "week_text": week_text,
        "week_iso": week,
        "observed_date": observed.isoformat() if observed else None,
        "modified_text": modified_text,
    }


def normalize_unit(unit_text: str) -> str:
    lower = unit_text.lower()
    if "stuk" in lower:
        return "EUR/stuk"
    if "tojás" in lower or "tojas" in lower:
        return "EUR/tojás"
    if "kg" in lower:
        return "EUR/kg"
    if "100" in lower or "stuks" in lower:
        return "EUR/100"
    return EXPORT_UNIT


def fetch_html(session: requests.Session, source: Source) -> str:
    response = session.get(source.url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def fetch_chart(
    session: requests.Session,
    *,
    page_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any] | None:
    response = session.post(
        "https://www.pluimveebeurs.com/?marketplace",
        data={"startDate": start_date, "endDate": end_date, "pageId": page_id},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    try:
        return response.json()
    except requests.JSONDecodeError:
        return json.loads(response.text)


def chart_range(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    one_year = None
    for option in soup.select(".chart-option"):
        text = option.get_text(" ", strip=True).lower()
        if "1 jaar" in text:
            one_year = option
            break
    if one_year:
        return one_year.get("data-start"), one_year.get("data-end")

    print_button = soup.select_one(".print-button")
    if not print_button:
        return None, None
    href = print_button.get("href", "")
    start = re.search(r"startDate=(\d{4}-\d{2}-\d{2})", href)
    end = re.search(r"endDate=(\d{4}-\d{2}-\d{2})", href)
    return (start.group(1) if start else None, end.group(1) if end else None)


def infer_label_dates(labels: list[str], start_text: str, end_text: str) -> list[date | None]:
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    cursor = start - timedelta(days=14)
    candidates: list[date] = []
    while cursor <= end + timedelta(days=14):
        monday = cursor - timedelta(days=cursor.weekday())
        if not candidates or candidates[-1] != monday:
            candidates.append(monday)
        cursor += timedelta(days=1)

    result: list[date | None] = []
    search_from = 0
    previous_date: date | None = None
    for label in labels:
        explicit = date_from_dutch_date(label, default_year=start.year)
        if explicit and "-" not in label:
            candidates_for_label = []
            for year in sorted({start.year - 1, start.year, end.year, end.year + 1}):
                maybe = date_from_dutch_date(label, default_year=year)
                if maybe and start - timedelta(days=14) <= maybe <= end + timedelta(days=14):
                    candidates_for_label.append(maybe)
            if previous_date:
                candidates_for_label = [d for d in candidates_for_label if d > previous_date]
            if candidates_for_label:
                chosen = min(candidates_for_label)
                result.append(chosen)
                previous_date = chosen
                continue

        match = WEEK_RE.search(label)
        if not match:
            result.append(None)
            continue
        week_num = int(match.group(1))
        found_index = None
        for idx in range(search_from, len(candidates)):
            if candidates[idx].isocalendar().week == week_num:
                found_index = idx
                break
        if found_index is None:
            result.append(None)
        else:
            chosen = candidates[found_index] + timedelta(days=4)
            result.append(chosen)
            previous_date = chosen
            search_from = found_index + 1
    return result


def parse_dataset_identity(source: Source, dataset_label: str) -> tuple[str | None, str | None, str]:
    tokens = dataset_label.strip().split()
    lowered = [token.lower() for token in tokens]

    unit = source.default_unit
    if lowered[:2] == ["per", "kg"]:
        unit = "EUR/kg"
        tokens = tokens[2:]
        lowered = lowered[2:]
    elif tokens and tokens[0] == "100":
        unit = "EUR/100"
        tokens = tokens[2:] if len(tokens) > 1 and tokens[1].lower().startswith("st") else tokens[1:]
        lowered = [token.lower() for token in tokens]
    elif source.default_unit in {"EUR/stuk", "EUR/tojás"}:
        unit = source.default_unit
    elif "kg" in source.default_unit.lower():
        unit = source.default_unit

    color = None
    size = None
    if tokens and lowered[0] in COLOR_TOKENS:
        color = lowered[0]
        tokens = tokens[1:]
    elif tokens and lowered[0] == "prijs":
        tokens = tokens[1:]

    for token in tokens:
        cleaned = token.strip()
        upper = cleaned.upper()
        if upper in SIZE_TOKENS or cleaned.isdigit():
            size = upper if upper in SIZE_TOKENS else cleaned
            break

    if source.key == "barneveldse" and unit == "EUR/kg" and size:
        size = f"kg_{size}"

    return size, color, unit


def series_label(source: Source, size: str | None, color: str | None, unit: str) -> str:
    label_size = size
    if source.key == "barneveldse" and unit == "EUR/kg" and size and size.startswith("kg_"):
        label_size = f"per kg {size.removeprefix('kg_')}"
    suffix = " ".join(part for part in [label_size, color] if part)
    label = source.label if not suffix else f"{source.label} — {suffix}"
    if unit != EXPORT_UNIT:
        label = f"{label} [{unit}]"
    return label


def should_keep_dataset(source: Source, dataset_label: str, unit: str) -> bool:
    return True


def parse_current_changes(soup: BeautifulSoup, source: Source, info: dict[str, Any]) -> dict[tuple[str | None, str | None, str], dict[str, Any]]:
    changes: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    for selection in soup.select(".chart-selection"):
        title_node = selection.select_one(".chart-selection-title")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        for item in selection.select(".chart-selection-item"):
            text = item.get_text(" ", strip=True)
            if "€" not in text:
                continue
            checkbox = item.select_one('input[type="checkbox"]')
            dataset_label = checkbox.get("name") if checkbox and checkbox.get("name") else f"{title} {text.split()[0]}"
            size, color, unit = parse_dataset_identity(source, dataset_label)
            if not should_keep_dataset(source, dataset_label, unit):
                continue
            price_match = PRICE_RE.search(text)
            change_match = CHANGE_RE.search(text)
            price = parse_number(price_match.group(1) if price_match else None)
            change = parse_number(change_match.group(1) if change_match else None)
            if price is None:
                continue
            changes[(size, color, unit)] = {
                "price": price,
                "change": change,
                "label": dataset_label,
                "week_iso": info.get("week_iso"),
                "observed_date": info.get("observed_date"),
            }
    return changes


def observations_from_chart(
    source: Source,
    chart: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    labels = chart.get("labels") or []
    label_dates = infer_label_dates(labels, start_date, end_date)
    observations: list[dict[str, Any]] = []
    for dataset in chart.get("prices") or chart.get("datasets") or []:
        dataset_label = str(dataset.get("label", "")).strip()
        size, color, unit = parse_dataset_identity(source, dataset_label)
        if not should_keep_dataset(source, dataset_label, unit):
            continue
        values = dataset.get("data") or []
        previous: float | None = None
        for idx, value in enumerate(values):
            price = parse_number(str(value)) if value is not None else None
            point_date = label_dates[idx] if idx < len(label_dates) else None
            if price is None or point_date is None:
                continue
            change = None if previous is None else round_change(price - previous)
            previous = price
            observations.append(
                {
                    "key": source.key,
                    "label": series_label(source, size, color, unit),
                    "country": source.country,
                    "size": size,
                    "color": color,
                    "unit": unit,
                    "source_url": source.url,
                    "week_iso": iso_week_from_date(point_date),
                    "observed_date": point_date.isoformat(),
                    "price": scale_price(source, price),
                    "change": scale_change(source, change),
                    "fetched_at": fetched_at,
                    "raw": {
                        "source": asdict(source),
                        "kind": "chart",
                        "dataset_label": dataset_label,
                        "chart_label": labels[idx] if idx < len(labels) else None,
                        "source_price": price,
                        "source_change": change,
                        "range": {"start": start_date, "end": end_date},
                    },
                }
            )
    return observations


def observations_from_current(
    source: Source,
    current: dict[tuple[str | None, str | None, str], dict[str, Any]],
    *,
    fetched_at: str,
) -> list[dict[str, Any]]:
    observations = []
    for (size, color, unit), item in current.items():
        if not item.get("week_iso"):
            continue
        observations.append(
            {
                "key": source.key,
                "label": series_label(source, size, color, unit),
                "country": source.country,
                "size": size,
                "color": color,
                "unit": unit,
                "source_url": source.url,
                "week_iso": item["week_iso"],
                "observed_date": item.get("observed_date"),
                "price": scale_price(source, item["price"]),
                "change": scale_change(source, item.get("change")),
                "fetched_at": fetched_at,
                "raw": {
                    "source": asdict(source),
                    "kind": "current",
                    "dataset_label": item.get("label"),
                    "source_price": item["price"],
                    "source_change": item.get("change"),
                },
            }
        )
    return observations


def scrape_source(session: requests.Session, source: Source) -> list[dict[str, Any]]:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    html = fetch_html(session, source)
    soup = BeautifulSoup(html, "html.parser")
    info = parse_info_box(soup)
    page = soup.find(id="marketDetail")
    page_id = page.get("data-page-id") if page else None
    start_date, end_date = chart_range(soup)
    current = parse_current_changes(soup, source, info)

    observations: list[dict[str, Any]] = []
    if page_id and start_date and end_date:
        try:
            chart = fetch_chart(session, page_id=page_id, start_date=start_date, end_date=end_date)
            if chart:
                observations.extend(
                    observations_from_chart(
                        source,
                        chart,
                        start_date=start_date,
                        end_date=end_date,
                        fetched_at=fetched_at,
                    )
                )
        except Exception as exc:
            warn(f"{source.key}: chart backfill failed: {exc}")

    observations.extend(observations_from_current(source, current, fetched_at=fetched_at))
    return observations


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Pluimveebeurs egg prices into SQLite and dashboard/data.json.")
    parser.add_argument("--source", action="append", dest="sources", help="Source key to scrape; repeatable. Defaults to all.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite DB path.")
    parser.add_argument("--out", type=Path, default=EXPORT_PATH, help="dashboard/data.json output path.")
    parser.add_argument("--no-export", action="store_true", help="Only upsert DB, do not write data.json.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

    all_observations: list[dict[str, Any]] = []
    for source in get_sources(args.sources):
        try:
            observations = scrape_source(session, source)
            all_observations.extend(observations)
            print(f"{source.key}: parsed {len(observations)} observations")
        except Exception as exc:
            warn(f"{source.key}: scrape failed: {exc}")
        time.sleep(REQUEST_DELAY_SECONDS)

    if not all_observations:
        warn("no observations parsed")
        return 1

    stored = store_observations(all_observations, args.db)
    print(f"stored/upserted {stored} observations in {args.db}")
    if not args.no_export:
        payload = export_data_json(args.db, args.out)
        print(f"exported {len(payload['series'])} series with per-series units to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
