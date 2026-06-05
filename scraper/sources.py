"""Source definitions for all Pluimveebeurs egg-price categories."""

from __future__ import annotations

from dataclasses import dataclass


BASE_URL = "https://www.pluimveebeurs.com/prijsinformatie"
EXPORT_UNIT = "EUR/db"

CATEGORY_CONFIG = {
    "kelteto": {"label": "Keltető tojás", "default_unit": "EUR/db"},
    "ipari": {"label": "Ipari tojás", "default_unit": "EUR/kg"},
    "etkezesi": {"label": "Étkezési tojás", "default_unit": "EUR/100"},
    "napos_csibe": {"label": "Napos csibe", "default_unit": "EUR/db"},
    "broiler": {"label": "Broiler", "default_unit": "EUR/100kg"},
}


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    country: str
    slug: str
    kind: str
    category: str
    site_category: str
    default_unit: str
    export_unit: str
    price_scale: float = 1.0
    url_override: str | None = None

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        return f"{BASE_URL}/{self.site_category}/{self.slug}"


SOURCES: tuple[Source, ...] = (
    Source(
        key="broedeiprijs_vrije_markt",
        label="Broedeiprijs vrije markt",
        country="NL",
        slug="broedeiprijs-vrije-markt",
        kind="single",
        category="kelteto",
        site_category="vleeskuikens",
        default_unit="EUR/db",
        export_unit="EUR/db",
    ),
    Source(
        key="broederijnotering_lto_nop_nvp",
        label="Broederijnotering LTO/NOP en NVP",
        country="NL",
        slug="broederijnotering-lto-nop-en-nvp",
        kind="single",
        category="kelteto",
        site_category="vleeskuikens",
        default_unit="EUR/db",
        export_unit="EUR/db",
        price_scale=0.01,
    ),
    Source(
        key="nop_richtprijs_industrie",
        label="NOP richtprijs 2.0 industrienotering",
        country="NL",
        slug="nop-richtprijs-20-industrienotering",
        kind="single",
        category="ipari",
        site_category="eierprijzen",
        default_unit="EUR/kg",
        export_unit="EUR/kg",
    ),
    Source(
        key="rungis_paris_industrie",
        label="Rungis - Paris industrie",
        country="FR",
        slug="rungis-paris-industrie",
        kind="single",
        category="ipari",
        site_category="eierprijzen",
        default_unit="EUR/kg",
        export_unit="EUR/kg",
    ),
    Source(
        key="weser_ems_verarbeitung",
        label="Weser Ems Verarbeitungswaren",
        country="DE",
        slug="weser-ems-verarbeitungswaren",
        kind="single",
        category="ipari",
        site_category="eierprijzen",
        default_unit="EUR/kg",
        export_unit="EUR/kg",
    ),
    Source(
        key="weser_ems_verarbeitung_boden",
        label="Weser Ems Verarbeitungswaren Bodenhaltung",
        country="DE",
        slug="weser-ems-verarbeitungswaren-bodenhaltung",
        kind="single",
        category="ipari",
        site_category="eierprijzen",
        default_unit="EUR/kg",
        export_unit="EUR/kg",
    ),
    Source(
        key="barneveldse",
        label="Barneveldse Eiernotering",
        country="NL",
        slug="barneveldse-eiernotering",
        kind="multi",
        category="etkezesi",
        site_category="eierprijzen",
        default_unit="EUR/100",
        export_unit="EUR/100",
    ),
    Source(
        key="weser_ems_boden",
        label="Weser Ems Bodenhaltung",
        country="DE",
        slug="weser-ems-bodenhaltung",
        kind="multi",
        category="etkezesi",
        site_category="eierprijzen",
        default_unit="EUR/100",
        export_unit="EUR/100",
    ),
    Source(
        key="weser_ems_konv",
        label="Weser Ems (konv.)",
        country="DE",
        slug="weser-ems",
        kind="multi",
        category="etkezesi",
        site_category="eierprijzen",
        default_unit="EUR/100",
        export_unit="EUR/100",
    ),
    Source(
        key="rungis",
        label="Rungis - Paris",
        country="FR",
        slug="rungis-paris",
        kind="multi",
        category="etkezesi",
        site_category="eierprijzen",
        default_unit="EUR/100",
        export_unit="EUR/100",
    ),
    Source(
        key="kruisem",
        label="Kruisem handelsnotering",
        country="BE",
        slug="kruisem-handelsnotering",
        kind="multi",
        category="etkezesi",
        site_category="eierprijzen",
        default_unit="EUR/100",
        export_unit="EUR/100",
    ),
    Source(
        key="napos_csibe_cenyrolnicze",
        label="Napos csibe",
        country="PL",
        slug="",
        kind="cenyrolnicze_chicks",
        category="napos_csibe",
        site_category="",
        default_unit="EUR/db",
        export_unit="EUR/db",
        url_override="https://www.cenyrolnicze.pl/drob/piskleta",
    ),
    Source(
        key="eu_whole_broiler_65",
        label="EU Whole broiler (65%)",
        country="EU",
        slug="",
        kind="eu_broiler",
        category="broiler",
        site_category="",
        default_unit="EUR/100kg",
        export_unit="EUR/100kg",
        url_override="https://api.tech.ec.europa.eu/agrifood/api/poultry/prices",
    ),
)


def get_sources(keys: list[str] | None = None) -> list[Source]:
    if not keys:
        return list(SOURCES)
    wanted = set(keys)
    unknown = sorted(wanted - {source.key for source in SOURCES})
    if unknown:
        raise ValueError(f"unknown source key(s): {', '.join(unknown)}")
    return [source for source in SOURCES if source.key in wanted]
