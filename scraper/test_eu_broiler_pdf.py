"""EU broiler: DG AGRI dashboard PDF-tartalek (az EC agrifood API SUSPENDED)."""
from datetime import date
from pathlib import Path

import pytest

import scrape

FIXTURE = Path(__file__).parent / "fixtures" / "eu_broiler_dashboard_2026-11-20.txt"


def test_parse_dashboard_text():
    parsed = scrape.parse_eu_broiler_dashboard_text(FIXTURE.read_text())
    assert parsed["price"] == 287.45
    assert parsed["week_start"] == date(2026, 11, 9)  # 2026-W46 hetfoje
    assert parsed["week_pct"] == 1.4
    assert parsed["updated"] == date(2026, 11, 20)


def test_parse_rejects_unknown_layout():
    with pytest.raises(ValueError):
        scrape.parse_eu_broiler_dashboard_text("nothing useful here")


def test_january_update_uses_previous_year():
    text = FIXTURE.read_text().replace("20.11.2026", "05.01.2027")
    assert scrape.parse_eu_broiler_dashboard_text(text)["week_start"].year == 2026


def test_api_failure_falls_back_to_pdf(monkeypatch):
    source = scrape.get_sources(["eu_whole_broiler_65"])[0]
    monkeypatch.setattr(scrape, "scrape_eu_broiler_api", lambda s, src: (_ for _ in ()).throw(RuntimeError("500")))
    sentinel = [{"key": "eu_whole_broiler_65"}]
    monkeypatch.setattr(scrape, "scrape_eu_broiler_pdf", lambda s, src: sentinel)
    assert scrape.scrape_eu_broiler(None, source) is sentinel


def test_api_ok_does_not_touch_pdf(monkeypatch):
    source = scrape.get_sources(["eu_whole_broiler_65"])[0]
    api = [{"key": "x"}]
    monkeypatch.setattr(scrape, "scrape_eu_broiler_api", lambda s, src: api)
    monkeypatch.setattr(scrape, "scrape_eu_broiler_pdf", lambda s, src: pytest.fail("pdf used"))
    assert scrape.scrape_eu_broiler(None, source) is api
