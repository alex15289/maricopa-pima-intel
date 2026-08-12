# Maricopa + Pima Document Intel

**Recorded-document intelligence for Maricopa and Pima counties, Arizona.** (Universal County Intelligence Framework v5.5.0)

**Doctrine: every lead is a real recorded document.** The lead *type* is the document *type* — Notice of Trustee Sale, Lis Pendens, Judgment, tax/mechanics lien, death certificate, probate, deed transfer. There are no scores, weights, combo bonuses, distress patterns, or tiers. The Assessor parcel master is demoted to an **enrichment lookup** (APN → address / owner / mailing); it never generates leads. Heuristic flags (absentee, out-of-state, long-hold) are optional **display annotations**, not lead generators.

Live dashboard: `index.html` — static, GitHub Pages ready, reads `data/leads.json`, organized by document type.

---

## How it works

```
  MARICOPA                          PIMA
  ┌────────────────────────┐        ┌─────────────────────────────┐
  │ Recorder REST API      │        │ GIS LandRecords layer 12    │
  │ publicapi.recorder…    │        │ (deed transfers: SEQ_NUM_D, │
  │ 19 doc-type codes      │        │  RECORDDATE, APN, owner)    │
  │ (names, no APN)        │        │ + Treasurer monthly file    │
  └───────────┬────────────┘        └──────────────┬──────────────┘
              │ maricopa_recorder_docs        pima_recorder_docs
              │ (name→parcel match)           pima_tax_docs (resolved)
              ▼                                       ▼
         ArcGIS parcel master  ───────────────►  pipeline/build_docleads.py
         (enrichment only)                        │ APN → address/owner/mail
                                                  │ CQ/TD close matching NS
                                                  │ annotations, freshness sort
                                                  ▼
                                            data/leads.json
                                        (documents, newest first,
                                         per-doc-type counts)
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                          index.html dashboard         Skip Trace / GHL CSV
```

Maricopa recorder documents carry party **names, not APNs**, so they are joined to the parcel master by owner name (`pipeline/match_recorder.py`, strict-first). Documents that can't be pinned to a parcel stay in the list as **unresolved** leads — still exported with their name + document number for skip tracing. Pima deed transfers arrive already parcel-resolved from the GIS layer. A `NTS Cancelled` (CQ) or `Trustee's Deed` (TD) is not a standalone lead — it **closes** its matching Notice of Trustee Sale, marking that lead cancelled or completed.

---

## Setup (one-time)

```bash
# 1. Clone
git clone <repo-url> maricopa-pima-intel
cd maricopa-pima-intel

# 2. Python environment
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

All document sources are open HTTP (Maricopa recorder REST API, Pima GIS ArcGIS) — no browser automation required.

---

## Run a full refresh

```bash
./run.sh                      # or: make refresh
```

That single command:

1. Pulls both parcel masters (~2.2M parcels total — for enrichment only)
2. Scrapes recorded documents: Maricopa recorder (last 30 days), Pima deed transfers (last 90 days), Pima treasurer file if present
3. Builds `data/leads.json` (doc-type leads, newest first)

After that, open `index.html` in a browser (or push to GitHub Pages).

---

## Run individual steps

```bash
# Parcel masters (enrichment lookup)
python scrapers/maricopa_parcels.py
python scrapers/pima_parcels.py

# Recorded-document sources
python scrapers/maricopa_recorder_api.py --days 30    # REST API, 19 doc-type codes
python scrapers/pima_deeds.py --days 90               # GIS layer 12 deed transfers
python pipeline/enrich_treasurer.py                   # Pima delinquency file (if present)

# Build doc-type lead list
python -m pipeline.build_docleads

# Export CSVs (unresolved leads included, with name + doc number)
python pipeline/export_csv.py
python pipeline/export_csv.py --county Maricopa --doc-type "Notice of Trustee Sale"
```

---

## GitHub Pages deployment

1. Push the repo to GitHub.
2. Settings → Pages → Build from branch → `main` / `/ (root)`.
3. The dashboard goes live at `https://<user>.github.io/<repo>/`.
4. Refresh the dataset: run `./run.sh` locally, then commit `data/leads.json`. Next push updates the live dashboard.

**Automated refresh:** `.github/workflows/refresh.yml` runs daily at 11:00 UTC (4am Phoenix — MST year-round, no DST). Parcel masters (enrichment) come from a weekly `actions/cache` entry; the cumulative document stores come from a rolling daily cache so `CQ`/`TD` can close an `NS` recorded weeks earlier. Each run pulls Maricopa recorder documents via the REST API and Pima deed transfers from the GIS layer (both fail-soft), rebuilds `data/leads.json`, and commits it back to `main` (skipped when nothing changed). A sanity guard refuses to commit a gutted document pool. `weekly-refresh.yml` and `monthly-refresh.yml` are manual-dispatch only (superseded).

---

## Configuration

### Document types
`scrapers/maricopa_recorder_api.py` has a `DOC_TYPES` registry (query code → label + category). These are the **2-letter query codes** from the county's own search dropdown, not the display codes returned in results — verify a code's live behavior before adding one (e.g. `SL` is State Tax Lien; `STL` is Substitution of Trustee). The 19 approved types span Foreclosure, Legal, Tax & Liens, Estate, and Transfers. Pima deed transfers are a single `Deed Transfer` type from the GIS layer; the treasurer feed adds `Tax Delinquent`.

### Foreclosure lifecycle
`pipeline/build_docleads.py` treats `NTS Cancelled` (CQ) and `Trustee's Deed` (TD) as closers: they mark their matching `Notice of Trustee Sale` cancelled/completed rather than appearing as standalone leads. Matching is by resolved APN or shared party name, nearest prior NS within `KILL_LOOKBACK_DAYS`.

**Provisional status while the store warms up.** The cumulative document store starts empty and fills over successive daily runs (retention-capped). Until its closer history spans `CONFIRM_COVERAGE_DAYS` (180), an "active" NS cannot be confirmed — a cancellation could exist in the not-yet-scanned past — so it is flagged `status_provisional` and the dashboard shows it as a dashed **`active?`** badge (with an explanation in the detail panel) rather than a definitive `active`. This self-heals automatically: once the automation has been running continuously for ~180 days, `coverage_days` reaches the threshold and active NS become confirmed (`history_confident: true` in `leads.json`'s `foreclosure_lifecycle`). No manual step required.

### Default-off document types
Three high-volume, low-resolution people-lien / civil types ship **off by default** in the dashboard — `Judgment`, `AHCCCS Lien`, `Mechanics Lien` (set in `DEFAULT_OFF` in `index.html`). They are mostly unresolved (business/agency parties) and would crowd the view; they remain in `leads.json` and can be toggled on per campaign from the sidebar (marked `off`). All other types are on by default.

### Date window
Pass `--days N` to `maricopa_recorder_api.py` (default 30; API coverage goes back to 1871) and to `pima_deeds.py` (default 90; the GIS deed layer lags recording by ~2 weeks). `--retention N` controls how long documents persist in the cumulative store (default 180 days) — this is what lets a `TD`/`CQ` match an `NS` scraped in an earlier run.

---

## Repo structure

```
.
├── index.html                 # Dashboard (GitHub Pages entrypoint)
├── data/                      # All JSONL / JSON / CSV output
│   └── leads.json             # Dashboard input (committed)
├── scrapers/
│   ├── maricopa_parcels.py       # ArcGIS parcel master (enrichment)
│   ├── pima_parcels.py           # ArcGIS parcel master (enrichment)
│   ├── maricopa_recorder_api.py  # Recorder REST API — doc-type documents
│   └── pima_deeds.py             # GIS layer 12 deed transfers
├── pipeline/
│   ├── build_docleads.py         # Doc-type lead builder (no scores/tiers)
│   ├── match_recorder.py         # Name → parcel matching (Maricopa docs)
│   ├── enrich_treasurer.py       # Pima delinquency file → Tax Delinquent docs
│   └── export_csv.py             # Skip Trace + GHL formats
├── .github/workflows/
│   └── refresh.yml            # Daily automated refresh (4am Phoenix)
├── requirements.txt
├── run.sh                     # One-shot full refresh
├── .gitignore
└── LICENSE
```

---

## Data sources

All data is pulled from public, published government endpoints. No authentication, no paid data.

| Source | What it provides |
|---|---|
| `gis.mcassessor.maricopa.gov/.../Parcels/MapServer/0` | Maricopa parcel roll — APN, owner, site + mailing address, characteristics, valuations (enrichment) |
| `services1.arcgis.com/Ezk9fcjSUkeadg6u` / `gisdata.pima.gov/.../LandRecords/MapServer/12` | Pima parcel roll + deed transfers (SEQ_NUM_D doc #, RECORDDATE) |
| `publicapi.recorder.maricopa.gov/documents/search` | Maricopa recorded documents — open REST API, coverage back to 1871 |
| Pima Treasurer monthly delinquency file | Dropped into `data/` when subscribed; `enrich_treasurer.py` translates it |

Note: the Pima recorder portal (`pimacountyaz-web.tylerhost.net`) is a session-gated Tyler EagleWeb app (disclaimer + reCAPTCHA per session, stateful search) — not automatable unattended, so Pima document leads come from the GIS deed layer instead.
- City of Phoenix / Tucson / Mesa code violation portals
- Arizona Judicial Branch probate case search

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no data yet` on dashboard | Run `python -m pipeline.build_docleads` — it creates `data/leads.json`. |
| Recorder scrape returns 0 rows | County updated the portal UI. Check `data/_debug/*.png` for the failed screen and update selectors in the scraper. |
| Pima parcel scraper can't find endpoint | Visit https://gisopendata.pima.gov, find the Parcels dataset, click "View Data Source", copy the URL (without `/query`), and pass it: `python scrapers/pima_parcels.py --url <url>`. |
| ArcGIS paginator returns fewer records than count | Rate-limited. The built-in backoff will handle it. Rerun if it stalls. |
| Map shows nothing | Current build uses zip centroid fallback. Add geometry to the parcel scrape (set `returnGeometry=true` and project to WGS84) to get true lat/lon. |

---

## License

See `LICENSE`. This codebase was built as a work product; rights and distribution are per the underlying development agreement.
