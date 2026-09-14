#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from .sources import EXPORT_UNIT, Source, get_sources
    from .store import DB_PATH, EXPORT_PATH, export_data_json, series_freshness, store_observations
except ImportError:  # direct script execution
    from sources import EXPORT_UNIT, Source, get_sources
    from store import DB_PATH, EXPORT_PATH, export_data_json, series_freshness, store_observations


USER_AGENT = "Cloudus-Tojasar-Scraper/0.1 (+local dashboard; contact: owner)"
REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.35
WEEK_RE = re.compile(r"\b(?:wk|week)\s*(\d{1,2})\b", re.IGNORECASE)
DATE_DMY_RE = re.compile(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{4}))?")
PRICE_RE = re.compile(r"€\s*([+-]?\d+(?:[.,]\d+)?)")
CHANGE_RE = re.compile(r"€\s*[+-]?\d+(?:[.,]\d+)?\s+([+-]\d+(?:[.,]\d+)?)")
SIZE_TOKENS = {"XL", "L", "M", "S"}
COLOR_TOKENS = {"wit", "bruin"}
EU_BROILER_PRODUCT_NAMES = {"Whole broiler (65%)", "0207 11 30"}
ECB_PLN_SERIES_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.PLN.EUR.SP00.A"
CHICK_LINES = (
    ("ross_308", "Ross 308"),
    ("hubbard_flex", "Hubbard Flex"),
    ("cobb_500", "Cobb 500"),
    ("lohmann_brown", "Lohmann (L. Brown)"),
)


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


def parse_polish_period_start(text: str) -> date:
    matches = list(DATE_DMY_RE.finditer(text))
    if len(matches) < 2 or not matches[-1].group(3):
        raise ValueError(f"cannot parse Polish weekly period: {text!r}")
    year = int(matches[-1].group(3))
    start = matches[0]
    return date(year, int(start.group(2)), int(start.group(1)))


def fetch_ecb_pln_rate(session: requests.Session, target_date: date) -> dict[str, Any]:
    start = target_date - timedelta(days=10)
    response = session.get(
        ECB_PLN_SERIES_URL,
        params={"startPeriod": start.isoformat(), "endPeriod": target_date.isoformat(), "format": "csvdata"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    eligible = [row for row in rows if row.get("TIME_PERIOD") and row["TIME_PERIOD"] <= target_date.isoformat()]
    if not eligible:
        raise ValueError(f"no ECB PLN/EUR rate on or before {target_date}")
    row = max(eligible, key=lambda item: item["TIME_PERIOD"])
    return {
        "rate": float(row["OBS_VALUE"]),
        "date": row["TIME_PERIOD"],
        "source": "ECB EXR.D.PLN.EUR.SP00.A",
    }


def scrape_cenyrolnicze_chicks(session: requests.Session, source: Source) -> list[dict[str, Any]]:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    soup = BeautifulSoup(fetch_html(session, source), "html.parser")
    period_node = soup.select_one(".notowania-date")
    table = soup.find(id="table-notowania")
    if not period_node or not table:
        raise ValueError("weekly period or price table missing")
    period_start = parse_polish_period_start(period_node.get_text(" ", strip=True))
    summary = next(
        (row for row in table.find_all("tr") if "PODSUMOWANIE" in row.get_text(" ", strip=True)),
        None,
    )
    if not summary:
        raise ValueError("summary row missing")
    average_cells = [
        cell.get_text(" ", strip=True)
        for cell in summary.find_all("td")
        if "Średnia cena za sztukę" in cell.get_text(" ", strip=True)
    ]
    if len(average_cells) < 5:
        raise ValueError(f"expected 5 chick summary cells, got {len(average_cells)}")

    fx = fetch_ecb_pln_rate(session, period_start)
    observations = []
    source_indexes = (0, 1, 2, 4)
    for (key, label), source_index in zip(CHICK_LINES, source_indexes):
        match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*$", average_cells[source_index])
        native_price = parse_number(match.group(1) if match else None)
        if native_price is None:
            continue
        observations.append(
            {
                "key": key,
                "label": label,
                "country": source.country,
                "category": source.category,
                "size": None,
                "color": None,
                "unit": "EUR/db",
                "source_url": source.url,
                "week_iso": iso_week_from_date(period_start),
                "observed_date": period_start.isoformat(),
                "price": round(native_price / fx["rate"], 6),
                "change": None,
                "native_price": native_price,
                "native_unit": "PLN/db",
                "fx_rate": fx["rate"],
                "fx_rate_unit": "PLN/EUR",
                "fx_rate_date": fx["date"],
                "fx_source": fx["source"],
                "fetched_at": fetched_at,
                "raw": {
                    "kind": source.kind,
                    "line": label,
                    "period_start": period_start.isoformat(),
                    "summary_text": average_cells[source_index],
                },
            }
        )
    return observations


def scrape_eu_broiler(session: requests.Session, source: Source) -> list[dict[str, Any]]:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    end = date.today()
    start = end - timedelta(days=370)
    response = session.get(
        source.url,
        params={
            "memberStateCodes": "EU",
            "beginDate": start.strftime("%d/%m/%Y"),
            "endDate": end.strftime("%d/%m/%Y"),
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    observations = []
    previous: float | None = None
    rows = sorted(response.json(), key=lambda row: datetime.strptime(row["beginDate"], "%d/%m/%Y"))
    for row in rows:
        if row.get("productName") not in EU_BROILER_PRODUCT_NAMES or row.get("priceType") != "Selling price":
            continue
        point_date = datetime.strptime(row["beginDate"], "%d/%m/%Y").date()
        price = parse_number(str(row.get("price", "")).replace("€", ""))
        if price is None:
            continue
        change = None if previous is None else round_change(price - previous)
        previous = price
        observations.append(
            {
                "key": source.key,
                "label": source.label,
                "country": source.country,
                "category": source.category,
                "size": None,
                "color": None,
                "unit": source.export_unit,
                "source_url": source.url,
                "week_iso": iso_week_from_date(point_date),
                "observed_date": point_date.isoformat(),
                "price": price,
                "change": change,
                "fetched_at": fetched_at,
                "raw": {"kind": source.kind, "api_row": row},
            }
        )
    return observations


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
    if unit != source.default_unit:
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
                    "category": source.category,
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
                "category": source.category,
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
    if source.kind == "cenyrolnicze_chicks":
        return scrape_cenyrolnicze_chicks(session, source)
    if source.kind == "eu_broiler":
        return scrape_eu_broiler(session, source)

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


RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1.0
RETRY_STATUS = (500, 502, 503, 504)


def build_session() -> requests.Session:
    """A scraper HTTP-sessionje, retry-rel.

    Kulon fuggveny, mert kulonben TESZTELHETETLEN (RED1 mutacios meres: a
    retry-reteg torlese nyomtalanul atment a teszten). Igy egy lokalis
    http.server-rel merhető, hogy tenyleg ujraprobal-e.

    Miert kell: a naploban rogzitett OSSZES eddigi bukas tranziens kulso hiba
    volt (500 / 504 / read timeout), es forrasonkent EGYETLEN probalkozas ment.
    A riasztas-zaj gyokere itt van, nem a kilepesi kod szemantikajaban.
    """
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    )
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


ALERT_STATE_NAME = "alert-state.json"


def _alert_state_path(db_path: Path) -> Path:
    return db_path.parent / ALERT_STATE_NAME


def newly_stale_series(db_path: Path, stale: list[dict[str, Any]]) -> list[str]:
    """Melyik sorozat valt MOST elavultta — esemeny, nem allapot.

    RED1 N-1 (2026-09-14): az elso valtozat az ALLAPOTRA riasztott, ezert egy
    tartosan lemarado forras miatt Tomi MINDEN REGGEL kapott volna riasztast.
    A sajat szabalyunk (reference-esemenyt-naplozz-ne-allapotot) szerint a
    jelzes a kuszob ATLEPESEHEZ kotodik. A folyamatos allapotot a dashboard
    figyelmezteto savja mutatja, nem a riasztas.

    A visszaallo sorozat kikerul a nyilvantartasbol, igy ha kesobb ujra
    elavul, ujra szol egyszer.
    """
    path = _alert_state_path(db_path)
    current = sorted(item["key"] for item in stale)
    previous: list[str] = []
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("reported_stale", [])
    except FileNotFoundError:
        pass
    except Exception as exc:  # serult allapotfajl ne allitsa meg a futast
        warn(f"alert state unreadable ({exc}); treating every stale series as new")
    fresh = [key for key in current if key not in set(previous)]
    try:
        path.write_text(
            json.dumps({"reported_stale": current}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        warn(f"alert state not written ({exc})")
    return fresh


# Kilepesi kodok — a run_daily.sh ezekre tamaszkodik.
#   0 = minden forras rendben
#   1 = valodi baj, Tomit ertesiteni kell
#   2 = degradalt: volt forraskieses, de az adat mentve es exportalva; NEM riaszt
EXIT_OK = 0
EXIT_ALERT = 1
EXIT_DEGRADED = 2


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Pluimveebeurs egg prices into SQLite and dashboard/data.json.")
    parser.add_argument("--source", action="append", dest="sources", help="Source key to scrape; repeatable. Defaults to all.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite DB path.")
    parser.add_argument("--out", type=Path, default=EXPORT_PATH, help="dashboard/data.json output path.")
    parser.add_argument("--no-export", action="store_true", help="Only upsert DB, do not write data.json.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    session = build_session()

    sources = list(get_sources(args.sources))
    all_observations: list[dict[str, Any]] = []
    failures: list[str] = []
    for source in sources:
        try:
            observations = scrape_source(session, source)
            if not observations:
                failures.append(f"{source.key}: no observations parsed")
            all_observations.extend(observations)
            print(f"{source.key}: parsed {len(observations)} observations")
        except Exception as exc:
            warn(f"{source.key}: scrape failed: {exc}")
            failures.append(f"{source.key}: {exc}")
        time.sleep(REQUEST_DELAY_SECONDS)

    # --- GYOKER-JAVITAS 2026-09-14 ---
    # Korabban itt allt egy "if failures: return 1" kapu a MENTES ELOTT. Emiatt
    # egyetlen forras kiesese eldobta az OSSZES tobbi forras aznapi adatat is
    # (merve 2026-09-14: 13-bol 12 forras sikeres, 2755 megfigyeles a kukaba,
    # a DB-ben aznap 0 sor). Az export amugy is a DB-bol dolgozik, tehat
    # szerkezetileg soha nem lehetett "reszleges" — a regi "refusing partial
    # export" uzenet felrevezetett: nem exportot tagadott meg, hanem mentest.
    # Ezert: eloszor MENTUNK es EXPORTALUNK, a hiba-dontes utana jon.
    stored, skipped = 0, []
    if all_observations:
        stored, skipped = store_observations(all_observations, args.db)
        print(f"stored/upserted {stored} observations in {args.db}")
    else:
        warn("no observations from any source - nothing to store")

    # RED1 N-2 (2026-09-14): a frissesseg MINDIG a DB-bol jon, az exporttol
    # FUGGETLENUL. Az elso valtozat az export agan szamolta, ezert pont akkor
    # volt vak ra, amikor a rendszer a leginkabb romlott.
    stale = [row for row in series_freshness(args.db) if row["stale"]]
    if stale:
        print(f"stale series: {len(stale)}")

    if args.no_export:
        pass
    elif stored == 0:
        # RED1 F-3: ha semmi nem kerult a DB-be, az export csak egy FRISS
        # generated_at-et irna a regi adat foleé — a dashboard fejlece "ma
        # frissult"-et mutatna egy teljes kieses napjan. A DB nem valtozott,
        # tehat nincs mit exportalni. Inkabb legyen a fajl lathatoan regi.
        warn("nothing stored - data.json left untouched (no fake generated_at)")
    else:
        payload = export_data_json(args.db, args.out)
        count = sum(len(category["series"]) for category in payload["categories"])
        print(f"exported {count} series in {len(payload['categories'])} categories to {args.out}")

    for failure in failures:
        warn(f"source failure: {failure}")

    # --- A DONTES. Sorrend: eloszor a rendszerszintu baj, aztan az ESEMENY,
    # vegul a degradalt allapotok. Minden ag a mentes es az export UTAN van —
    # ez a javitas lenyege: a hiba-dontes soha ne elozze meg a hatast.

    # 1) Egyetlen forras sem adott adatot.
    if not all_observations:
        warn(f"all {len(sources)} sources failed - no data stored")
        return EXIT_ALERT

    # 2) Kinaltunk sorokat, de EGY SEM ment be (RED1 N-2: ez korabban nema
    #    siker volt — 2600-bol 2599 eldobva is exit 0-t adott).
    if stored == 0:
        warn(
            f"all {len(all_observations)} offered observations were rejected - "
            f"nothing stored"
        )
        return EXIT_ALERT

    # 3) UJONNAN elavult sorozat -> riasztas EGYSZER (esemeny, nem allapot).
    newly = newly_stale_series(args.db, stale)
    if newly:
        by_key = {item["key"]: item for item in stale}
        details = ", ".join(
            f"{key} ({by_key[key]['days_since_update']}d > {by_key[key]['stale_after_days']}d)"
            for key in newly[:5]
        )
        warn(f"{len(newly)} series newly stale: {details}")
        return EXIT_ALERT

    # 4) Degradalt allapotok: latszanak a naploban es a dashboard savjan,
    #    de NEM ebresztik fel Tomit.
    if skipped:
        warn(f"degraded: {len(skipped)} observation(s) rejected, {stored} stored")
        return EXIT_DEGRADED

    if stale:
        warn(
            f"degraded: {len(stale)} series stale (already reported) - "
            f"see dashboard freshness banner"
        )
        return EXIT_DEGRADED

    if failures:
        warn(
            f"degraded: {len(failures)} of {len(sources)} sources failed; "
            f"{stored} observations stored and exported anyway"
        )
        return EXIT_DEGRADED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
