"""Source definitions for Pluimveebeurs hatching egg price noteringen."""

from __future__ import annotations

from dataclasses import dataclass


BASE_URL = "https://www.pluimveebeurs.com/prijsinformatie"


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    country: str
    slug: str
    kind: str
    category: str = "vleeskuikens"
    default_unit: str = "EUR/db"
    export_unit: str = "EUR/db"
    price_scale: float = 1.0

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.category}/{self.slug}"


SOURCES: tuple[Source, ...] = (
    Source(
        key="broedeiprijs_vrije_markt",
        label="Broedeiprijs vrije markt",
        country="NL",
        slug="broedeiprijs-vrije-markt",
        kind="single",
        default_unit="EUR/db",
        export_unit="EUR/db",
    ),
    Source(
        key="broederijnotering_lto_nop_nvp",
        label="Broederijnotering LTO/NOP en NVP",
        country="NL",
        slug="broederijnotering-lto-nop-en-nvp",
        kind="single",
        default_unit="EUR/db",
        export_unit="EUR/db",
        price_scale=0.01,
    ),
)


EXPORT_UNIT = "EUR/db"


def get_sources(keys: list[str] | None = None) -> list[Source]:
    if not keys:
        return list(SOURCES)
    wanted = set(keys)
    unknown = sorted(wanted - {source.key for source in SOURCES})
    if unknown:
        raise ValueError(f"unknown source key(s): {', '.join(unknown)}")
    return [source for source in SOURCES if source.key in wanted]
