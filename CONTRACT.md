# Tojásár-dashboard — Megosztott kontraktus (Cloudus tulajdona)

> Ez a SZERZŐDÉS a scraper (Turing) és a dashboard (Cloudus) között.
> Mindkét oldal EHHEZ épít. Ha módosítani kell → Cloudusszal egyeztetni a buszon.
> **Verzió: 0.4.0 · 2026-06-05** — ÖT kategória, auditálható PLN→EUR konverzióval.

## Mi változott v0.3 → v0.4
4. **Napos csibe** — publikus aktuális heti lengyel aggregátum, eredeti PLN/db és ECB-rátával konvertált EUR/db.
5. **Broiler** — EU heti `Whole broiler (65%)` eladási ár, EUR/100kg.

A meglévő három kategória és azok series-formája változatlan.

## Mi változott v0.2 → v0.3 (Tomi #2027)
A dashboard mostantól **3 szekciós** (tab/oldal):
1. **🥚 Keltető tojás** — a FŐ/alapnézet (hatching eggs). A jelenlegi 2 broedei series.
2. **🏭 Ipari tojás** — feldolgozóipari/processing tojás (4 notering, EUR/kg).
3. **🛒 Étkezési tojás** — fogyasztói/bolti tojás (a KORÁBBI 43-series consumer dataset, vissza-integrálva).
- A `data.json` mostantól **kategóriákba csoportosít** (lent az új séma). A series-szintű forma VÁLTOZATLAN.
- Minden kategóriának SAJÁT `default_unit`-ja van (kelteto=EUR/db, ipari=EUR/kg, etkezesi=EUR/100).

## Projekt-felállás
- **Turing** (/cx): `scraper/` — heti/napi fetch + parse + SQLite + `dashboard/data.json` export (v0.4 séma, 5 kategória).
- **Cloudus**: `dashboard/index.html` (kategória-tabos UI) + kontraktus + glue + review + integráció.
- **Humboldt**: forrás-kutatás (napos csibe mély pass folyamatban).

## Mappa-layout
```
~/projektek/tojasar-dashboard/
├── CONTRACT.md          # ez a fájl
├── scraper/             # Turing
│   ├── scrape.py        # fetch + parse
│   ├── store.py         # SQLite upsert + data.json export (v0.4)
│   ├── sources.py       # mind az 5 kategória forrásdefiníciói (category mezővel)
│   ├── run_daily.sh     # napi wrapper + failure-watchdog (Telegram-ping)
│   └── requirements.txt
├── data/eggprices.db    # SQLite (category oszloppal)
└── dashboard/
    ├── index.html       # Cloudus: 3-kategóriás interaktív UI
    └── data.json        # a scraper írja, a dashboard olvassa (v0.4 séma)
```

## FORRÁS-TÉRKÉP kategóriánként
Mind: `https://www.pluimveebeurs.com/prijsinformatie/<site_category>/<slug>`

### 🥚 kelteto (category="kelteto", default_unit="EUR/db") — site: vleeskuikens
| key | label | ország | slug (✅ verifikálva) | unit | megjegyzés |
|-----|-------|--------|------|------|-----------|
| `broedeiprijs_vrije_markt` | Broedeiprijs vrije markt | NL | `broedeiprijs-vrije-markt` | EUR/db | szabad piaci, volatilis |
| `broederijnotering_lto_nop_nvp` | Broederijnotering LTO/NOP en NVP | NL | `broederijnotering-lto-nop-en-nvp` | EUR/db | szövetségi referencia, stabil (price_scale 0.01) |

### 🏭 ipari (category="ipari", default_unit="EUR/kg") — site: eieren  [ÚJ — Humboldt #601]
| key | label | ország | slug (⚠️ Turing VERIFIKÁLJA élőben) | unit | megjegyzés |
|-----|-------|--------|------|------|-----------|
| `nop_richtprijs_industrie` | NOP richtprijs 2.0 industrienotering | NL | `nop-richtprijs-20-industrienotering` | EUR/kg | **AJÁNLOTT alap** — EU-szintű módszertan (Weser Ems + Rungis + Anevei súlyozva), ~€1.737/kg (hét 22) |
| `rungis_paris_industrie` | Rungis-Paris industrie | FR | `rungis-paris-industrie` *(verifikáld)* | EUR/kg | FR ipari |
| `weser_ems_verarbeitung` | Weser Ems Verarbeitungswaren | DE | `weser-ems-verarbeitungswaren` *(verifikáld)* | EUR/kg | DE feldolgozóipari |
| `weser_ems_verarbeitung_boden` | Weser Ems Verarbeitungswaren Bodenhaltung | DE | `weser-ems-verarbeitungswaren-bodenhaltung` *(verifikáld)* | EUR/kg | DE padlós feldolgozóipari |

> A slugokat Turing erősítse meg a live site `/prijsinformatie/eierprijzen` (vagy `/eieren`) oldal-térképéből — a fenti slug-tippek a label-ből vannak képezve. Backfill 1 év (1/3/12 hó "Prijsverloop" chart, mint a többinél).

### 🛒 etkezesi (category="etkezesi", default_unit="EUR/100") — site: eieren  [VISSZA-INTEGRÁLÁS]
A KORÁBBI consumer dataset (43 series). A source-definíciók a **git history-ban** vannak (a keltető-pivot ELŐTTI `scraper/sources.py`). Turing állítsa vissza, `category="etkezesi"` taggel. Backup adat referencia: `dashboard/data.consumer-backup-20260605-013428.json` (43 series, EUR/100 + EUR/kg) és `data/eggprices.consumer-backup-20260605-013428.db`.

Noteringok (size/color bontással → 43 series): Barneveldse Eiernotering (NL), Weser Ems Bodenhaltung (DE, XL/L/M/S × wit/bruin), Weser Ems konv. (DE), Rungis-Paris (FR), Kruisem handelsnotering (BE). **NOP-ot NE ide** — az ipari kategóriába megy. ABC Notering = broiler csirke, KIHAGYNI.

### 🐣 napos_csibe (category="napos_csibe", default_unit="EUR/db")
- Aktuális heti forrás: `https://www.cenyrolnicze.pl/drob/piskleta`
- Vonalak: Ross 308, Hubbard Flex, Cobb 500, Lohmann (L. Brown).
- A publikus `PODSUMOWANIE` heti átlagából csak numerikus érték exportálható; üres/`nan` nem nulla.
- Natív egység: PLN/db. Exportár: EUR/db.
- Historikus backfill nincs: az archív heti adatok előfizetés mögött vannak.
- ECB historikus FX: `EXR/D.PLN.EUR.SP00.A`; `EUR/db = PLN/db / OBS_VALUE`.
- FX-dátum: heti periódus kezdőnapja, vagy az azt megelőző legutóbbi ECB munkanap.

### 🍗 broiler (category="broiler", default_unit="EUR/100kg")
- API: `https://api.tech.ec.europa.eu/agrifood/api/poultry/prices`
- Lekérés: `memberStateCodes=EU` + dátumtartomány; a termék kliensoldalon szűrendő.
- Szűrés: `productName="Whole broiler (65%)"` és `priceType="Selling price"`.
- Egység: az API `national currency/100kg` mezője EU sornál ténylegesen EUR/100kg.

## data.json v0.4 séma (a SZERZŐDÉS lényege)
```json
{
  "generated_at": "2026-06-05T08:00:00+02:00",
  "schema_version": "0.4",
  "categories": [
    {
      "key": "kelteto",
      "label": "Keltető tojás",
      "default_unit": "EUR/db",
      "series": [
        {
          "key": "broedeiprijs_vrije_markt",
          "label": "Broedeiprijs vrije markt",
          "country": "NL", "size": null, "color": null,
          "unit": "EUR/db",
          "points": [ {"week":"2026-W22","date":"2026-05-27","price":0.335,"change":0.0} ]
        }
      ]
    },
    { "key": "ipari",    "label": "Ipari tojás",    "default_unit": "EUR/kg",  "series": [ ... ] },
    { "key": "etkezesi", "label": "Étkezési tojás", "default_unit": "EUR/100", "series": [ ... ] },
    { "key": "napos_csibe", "label": "Napos csibe", "default_unit": "EUR/db", "series": [ ... ] },
    { "key": "broiler", "label": "Broiler", "default_unit": "EUR/100kg", "series": [ ... ] }
  ]
}
```
**Szabályok (változatlan a v0.2-ből, csak kategóriába csomagolva):**
- A `series[]` objektum FORMÁJA azonos a v0.2-vel: `key, label, country, size, color, unit, points[]`.
- `points[]`: idő szerint NÖVEKVŐ (régi → új), heti felbontás, `{week, date, price, change}`. `change`/`date` lehet null.
- A naposcsibe-pontok ezen felül kötelező auditmezőket tartalmaznak:
  `native_price`, `native_unit`, `fx_rate`, `fx_rate_unit`, `fx_rate_date`, `fx_source`.
- `key`: stabil egyedi azonosító kategórián belül (`<notering>__<size>__<color>`, nem-létező részek elhagyva).
- **Per-series `unit` kötelező.** TILOS különböző egységű series-t egy Y-tengelyre tenni — a dashboard egységenként külön chartot ad (a meglévő unit-szegmens kezeli).
- A kategória `default_unit`-ja a UI kezdő egysége az adott tabon.
- A dashboard NE feltételezzen fix listát — dinamikusan a `categories[].series[]`-ből építkezik. Üres kategória (pl. ipari amíg nincs scrape) → "nincs adat" állapot, nem hiba.
- Backward-compat: ha `categories` hiányzik de `series` van (régi v0.2 fájl) → a UI egyetlen pszeudo-kategóriába csomagolja (ne törjön).

## SQLite séma (`data/eggprices.db`) — v0.4
A v0.3 séma + opcionális FX-audit oszlopok az `observation` táblán:
```sql
CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  label TEXT NOT NULL,
  country TEXT,
  category TEXT NOT NULL DEFAULT 'kelteto',
  size TEXT, color TEXT,
  unit TEXT DEFAULT 'EUR/100',
  source_url TEXT,
  UNIQUE(key, size, color)
);
CREATE TABLE IF NOT EXISTS observation (
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  week_iso TEXT NOT NULL, observed_date TEXT,
  price REAL NOT NULL, change REAL,
  native_price REAL, native_unit TEXT,
  fx_rate REAL, fx_rate_unit TEXT, fx_rate_date TEXT, fx_source TEXT,
  fetched_at TEXT NOT NULL, raw TEXT,
  UNIQUE(series_id, week_iso)
);
```
**Upsert**: `INSERT ... ON CONFLICT DO UPDATE` — idempotens, napi újrafutás nem duplikál.

## Frissítési ciklus
- Napi launchd 08:00: `run_daily.sh` → `scrape.py` (mind az 5 kategória) → DB upsert → `data.json` export → git push (csak ha változott) → hiba esetén Telegram-ping (failure-watchdog).
- GitHub Pages: https://nagytam94.github.io/tojasar-dashboard/ (auto-deploy push-ra).
- A dashboard a friss `data.json`-t mutatja automatikusan.
