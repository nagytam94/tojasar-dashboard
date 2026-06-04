# Tojásár-dashboard — Megosztott kontraktus (Cloudus tulajdona)

> Ez a SZERZŐDÉS a scraper (Turing) és a dashboard (Kepes) között.
> Mindkét oldal EHHEZ épít. Ha módosítani kell → Cloudusszal egyeztetni a buszon.
> Verzió: 0.2.0 · 2026-06-04 (per-series unit, Turing #295 feloldva)

## Projekt-felállás
- **Turing** (/cx): `scraper/` — heti fetch + parse + SQLite tárolás + `dashboard/data.json` export.
- **Kepes**: `dashboard/` — statikus interaktív idősor-dashboard, a `data.json`-t olvassa.
- **Cloudus**: kontraktus, glue, review, integráció, source-vizsgálat.

## Mappa-layout
```
~/projektek/tojasar-dashboard/
├── CONTRACT.md          # ez a fájl
├── scraper/             # Turing: Python scraper + pipeline daemon
│   ├── scrape.py        # fetch + parse egy run
│   ├── store.py         # SQLite upsert + data.json export
│   ├── sources.py       # a 6 notering definíciója (lent)
│   └── requirements.txt
├── data/
│   └── eggprices.db     # SQLite (lent a séma)
└── dashboard/           # Kepes: statikus UI
    ├── index.html
    ├── app.js
    ├── style.css
    └── data.json        # a scraper írja, a dashboard olvassa
```

## Források (6 notering — Humboldt kutatás #288)
Mind: `https://www.pluimveebeurs.com/prijsinformatie/eierprijzen/<slug>`

| key | label | ország | slug (✅ Cloudus által verifikálva) | jelleg |
|-----|-------|--------|------|--------|
| `barneveldse` | Barneveldse Eiernotering | NL | `barneveldse-eiernotering` | méret/szín bontás |
| `weser_ems_boden` | Weser Ems Bodenhaltung | DE | `weser-ems-bodenhaltung` | padlós, XL/L/M/S × wit/bruin |
| `weser_ems_konv` | Weser Ems (konv.) | DE | `weser-ems` | konvencionális |
| `nop_richtprijs` | NOP richtprijs 2.0 industrienotering | NL | `nop-richtprijs-20-industrienotering` | ipari |
| `rungis` | Rungis - Paris | FR | `rungis-paris` | egyetlen érték |
| `kruisem` | Kruisem handelsnotering | BE | `kruisem-handelsnotering` | egyetlen érték |

Mind: `https://www.pluimveebeurs.com/prijsinformatie/eierprijzen/<slug>`

> **FONTOS**: az ABC Notering = broiler csirke (`/prijsinformatie/vleeskuikens/abc-notering`), NEM tojás → kihagyni.
> **Kruisem variánsok** (Turing/Tomi döntés ha kell): `kruisem-handelsnotering` (alapértelmezett — kereskedelmi), `-handelsnotering-scharrel` (padlós), `-producentnotering`, `-producentnotering-scharrel`. Most a handelsnotering-et vesszük.
> **További elérhető noteringok** (későbbi bővítés, MOST NEM kell): Amsterdamse Index, Weser Ems Verarbeitungswaren (+ Bodenhaltung), `*-scharrel` (padlós) variánsok minden noteringnél. A `-scharrel` = free-range/padlós, érdekes lehet később mint külön series.

## Forrás-oldal struktúra (Cloudus WebFetch, weser-ems-bodenhaltung)
- Aktuális heti táblázat: méret (XL/L/M/S) × szín (wit/bruin), ár **/100 db EUR**, + heti változás (pl. -0.08).
- Fejléc: "wk 22", utolsó frissítés dátum+idő (pl. "29-05-2026 06:44").
- **"Prijsverloop"** (árfolyam) grafikon: 1 hónap / 3 hónap / 1 év nézet — valószínűleg AJAX/JSON endpoint tölti.
  → **Turing TASK**: nézd meg a network/page source-t, keresd meg ezt az endpointot. Ha megvan → ~52 hét backfill AZONNAL. Ha nincs → heti akkumuláció előre.
- Régebbi historikus adat (1 évnél régebbi) fizetős report (€37.50) → NEM kell, az 1-éves chart elég a starthoz.
- Egyszerűbb noteringok (NOP/Rungis/Kruisem) valószínűleg 1 érték/hét, nincs méret/szín bontás.

## SQLite séma (`data/eggprices.db`)
```sql
CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,          -- 'weser_ems_boden'
  label TEXT NOT NULL,        -- 'Weser Ems Bodenhaltung'
  country TEXT,               -- 'DE'
  size TEXT,                  -- 'XL'/'L'/'M'/'S' vagy NULL ha nincs bontás
  color TEXT,                 -- 'wit'/'bruin' vagy NULL
  unit TEXT DEFAULT 'EUR/100', -- ár-egység
  source_url TEXT,
  UNIQUE(key, size, color)
);

CREATE TABLE IF NOT EXISTS observation (
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  week_iso TEXT NOT NULL,     -- ISO 'YYYY-Www' pl. '2026-W22'
  observed_date TEXT,         -- 'YYYY-MM-DD' a forrás dátuma
  price REAL NOT NULL,        -- EUR/100
  change REAL,                -- heti változás, lehet NULL
  fetched_at TEXT NOT NULL,   -- ISO8601 mikor scrape-eltük
  raw TEXT,                   -- nyers parsed JSON (debug)
  UNIQUE(series_id, week_iso) -- idempotens upsert
);
```
**Upsert szabály**: `INSERT ... ON CONFLICT(series_id, week_iso) DO UPDATE` — heti újrafutás nem duplikál.

## `dashboard/data.json` séma (a SZERZŐDÉS lényege)
A scraper minden run végén ezt írja. A dashboard EZT olvassa. Forma:
```json
{
  "generated_at": "2026-06-04T20:10:00+02:00",
  "default_unit": "EUR/100",
  "series": [
    {
      "key": "weser_ems_boden__L__wit",
      "label": "Weser Ems Bodenhaltung — L wit",
      "country": "DE",
      "size": "L",
      "color": "wit",
      "unit": "EUR/100",
      "points": [
        { "week": "2026-W20", "date": "2026-05-15", "price": 18.50, "change": -0.20 },
        { "week": "2026-W21", "date": "2026-05-22", "price": 18.30, "change": -0.20 },
        { "week": "2026-W22", "date": "2026-05-29", "price": 18.00, "change": -0.30 }
      ]
    },
    {
      "key": "nop_richtprijs",
      "label": "NOP richtprijs 2.0 industrienotering",
      "country": "NL", "size": null, "color": null,
      "unit": "EUR/kg",
      "points": [
        { "week": "2026-W22", "date": "2026-05-29", "price": 1.18, "change": -0.02 }
      ]
    }
  ]
}
```
- `series[].key`: stabil egyedi azonosító (`<notering>__<size>__<color>`, a nem-létező részek elhagyva, pl. `nop_richtprijs`).
- `points[]`: **idő szerint növekvő** (régi → új), heti felbontás.
- `price`: szám. **`unit` PER-SERIES kötelező** (lent a unit-szabály). `change`/`date` lehet null.
- `default_unit`: top-level hint a leggyakoribb egységhez; a series-szintű `unit` az autoritatív.
- A dashboard ne feltételezzen fix series-listát — dinamikusan a `series[]`-ből építkezzen.

## Unit-szabály (v0.2.0 — Turing #295 ütközés feloldása)
A noteringok **nem mind EUR/100 db**: pl. **NOP richtprijs 2.0 = EUR/kg**, Barneveldse **vegyes** (100 st ÉS per kg dataset is van).
**DÖNTÉS (Cloudus):**
1. **Per-series `unit` mező kötelező** — minden series a SAJÁT egységét hordozza (`"EUR/100"` vagy `"EUR/kg"`). NINCS hasraütős konverzió (EUR/kg → EUR/100 átlag-tojássúlyt feltételezne → torzít, tilos).
2. **NOP marad az exportban** `unit: "EUR/kg"`-mal. Nem hagyjuk ki — Tomi 6 kulcs-noteringja közül egy.
3. **Vegyes-egységű notering (Barneveldse)**: KÜLÖN series-ekre bontva, mindegyik egy-egységű. Pl. `barneveldse__L` (EUR/100) és `barneveldse__kg` (EUR/kg). A scraper amelyik datasetet eléri, azt külön series-ként exportálja.
4. **Dashboard szabály**: TILOS különböző egységű series-t ugyanarra az Y-tengelyre tenni (félrevezető). Kepes vagy (a) egységenként külön panel/tengely, vagy (b) a notering-választó + legendben egység-címke, és csak azonos-egységűek kombinálhatók egy chartba. Egység mindig látszik a legendben/tooltipben.

## Dashboard követelmények (Kepes)
- Interaktív idősor-grafikon(ok), heti felbontás.
- Több notering összehasonlító nézet (multi-line chart) + notering-választó (checkbox/legend).
- Méret/szín szűrő ahol van bontás (Weser/Barneveld).
- Idő-range: 1 hó / 3 hó / 1 év / all.
- Hetente frissíthető: re-load data.json (nincs hardcode adat).
- Design: Clodus design system + Kepes magenta (#c724b1) akcentus. Sötét, letisztult.
- Statikus (HTML+JS), nincs backend kötelező — `data.json` fetch elég. Könnyű chart lib OK (pl. uPlot/Chart.js) vagy vanilla SVG.

## Frissítési ciklus
- Heti cron/launchd (pl. hétfő 08:00): `scrape.py` → DB upsert → `data.json` export.
- A dashboard a friss `data.json`-t mutatja automatikusan.
