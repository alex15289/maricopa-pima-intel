# Maricopa + Pima Intel

**Motivated-seller lead generation dashboard for Maricopa and Pima counties, Arizona.**

Pulls recorded documents (Notice of Trustee Sale, Lis Pendens, Affidavit of Death, tax liens, mechanics liens, HOA liens), joins them to the Assessor parcel master to resolve situs addresses, stacks multi-signal properties, scores, and exports ranked leads for skip tracing and CRM ingestion.

Live dashboard template: `index.html` — static, GitHub Pages ready, reads `data/leads.json`.

---

## How it works

```
┌───────────────────────┐     ┌──────────────────────────┐
│  ArcGIS parcel        │     │  Recorder portal         │
│  FeatureServer        │     │  (Playwright scrape)     │
│  (Maricopa + Pima)    │     │  NTS, LP, AFDT, FTL...   │
└──────────┬────────────┘     └────────────┬─────────────┘
           │  parcel master                 │  raw signals
           ▼                                ▼
    maricopa_parcels.jsonl           maricopa_recorder_raw.jsonl
    pima_parcels.jsonl               pima_recorder_raw.jsonl
           │                                │
           └───────────┬────────────────────┘
                       ▼
             pipeline/build_leads.py
               │ resolve APN → site address
               │ stack by APN
               │ score (weighted)
               ▼
                  data/leads.json
                       │
            ┌──────────┼──────────────┐
            ▼                         ▼
     index.html dashboard    pipeline/export_csv.py
                                  │
                       ┌──────────┼──────────┐
                       ▼                     ▼
                  skiptrace.csv           ghl.csv
```

The core insight: recorder documents identify properties by **APN, not street address**. The parcel master from each county's Assessor (bulk ArcGIS download) is the join table that gives every signal a real property address.

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

# 3. Playwright browser (first run only)
python -m playwright install chromium
```

---

## Run a full refresh

```bash
./run.sh                      # or: make refresh
```

That single command:

1. Pulls both parcel masters (~1.8M parcels total — takes ~15–25 min on first run, quicker afterwards since only deltas change day-to-day)
2. Scrapes recent recorder signals (default: last 30 days)
3. Builds `data/leads.json`
4. Exports `data/export_skiptrace.csv` and `data/export_ghl.csv`

After that, open `index.html` in a browser (or push to GitHub Pages).

---

## Run individual steps

```bash
# Parcel masters
python scrapers/maricopa_parcels.py
python scrapers/pima_parcels.py

# For a quick test, cap at 5000 parcels:
python scrapers/maricopa_parcels.py --limit 5000

# Signal scrapers (recorder portal — Playwright)
python scrapers/maricopa_recorder.py --days 30
python scrapers/pima_recorder.py --days 30

# Build ranked lead list
python pipeline/build_leads.py

# Export CSVs
python pipeline/export_csv.py
python pipeline/export_csv.py --min-score 80 --county Maricopa
```

---

## GitHub Pages deployment

1. Push the repo to GitHub.
2. Settings → Pages → Build from branch → `main` / `/ (root)`.
3. The dashboard goes live at `https://<user>.github.io/<repo>/`.
4. Refresh the dataset: run `./run.sh` locally, then commit `data/leads.json`. Next push updates the live dashboard.

**Automated refresh (optional):** `.github/workflows/refresh.yml` is included but disabled by default. Enable it in GitHub Actions to have the pipeline run on schedule (weekly parcel master, daily recorder signals). It commits the updated `data/` folder back to `main`.

---

## Configuration

### Scoring weights
`pipeline/build_leads.py` has a `WEIGHTS` dict at the top. Tune it per campaign (e.g., boost `"Notice of Trustee Sale"` if you want pre-foreclosure to dominate, increase `"flag_out_of_state"` for tired-landlord campaigns).

### Doc codes
`scrapers/maricopa_recorder.py` and `scrapers/pima_recorder.py` each have a `DOC_CODES` dict. Add or remove codes as needed. Codes vary slightly between counties — verify against the portal's advanced search.

### Date window
Pass `--days N` to either recorder scraper. Default 30. Maricopa's online recorder only exposes the last ~2 years. Pima exposes back to 1982.

---

## Repo structure

```
.
├── index.html                 # Dashboard (GitHub Pages entrypoint)
├── data/                      # All JSONL / JSON / CSV output
│   └── leads.json             # Dashboard input (committed)
├── scrapers/
│   ├── maricopa_parcels.py    # ArcGIS parcel master (Maricopa)
│   ├── pima_parcels.py        # ArcGIS parcel master (Pima)
│   ├── maricopa_recorder.py   # Recorder signal scraper (Playwright)
│   └── pima_recorder.py       # Recorder signal scraper (Playwright)
├── pipeline/
│   ├── build_leads.py         # Normalize + stack + score
│   └── export_csv.py          # Skip Trace + GHL formats
├── .github/workflows/
│   └── refresh.yml            # Scheduled automation (disabled by default)
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
| `gis.mcassessor.maricopa.gov/arcgis/rest/services/MaricopaDynamicQueryService/MapServer/0` | Maricopa full parcel roll — APN, owner, site address, mailing address, characteristics, valuations |
| `services1.arcgis.com/Ezk9fcjSUkeadg6u` (Pima ArcGIS org) | Pima parcel roll |
| `recorder.maricopa.gov/recording/document-search.html` | Maricopa recorded documents (2-yr window) |
| `www.recorder.pima.gov/PublicSearch` | Pima recorded documents (back to 1982) |

Future signal sources to add (scrapers follow the same unified schema):
- Maricopa County Treasurer tax-delinquent list
- Pima County Treasurer tax-delinquent list
- City of Phoenix / Tucson / Mesa code violation portals
- Arizona Judicial Branch probate case search

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no data yet` on dashboard | Run `python pipeline/build_leads.py` — it creates `data/leads.json`. |
| Recorder scrape returns 0 rows | County updated the portal UI. Check `data/_debug/*.png` for the failed screen and update selectors in the scraper. |
| Pima parcel scraper can't find endpoint | Visit https://gisopendata.pima.gov, find the Parcels dataset, click "View Data Source", copy the URL (without `/query`), and pass it: `python scrapers/pima_parcels.py --url <url>`. |
| ArcGIS paginator returns fewer records than count | Rate-limited. The built-in backoff will handle it. Rerun if it stalls. |
| Map shows nothing | Current build uses zip centroid fallback. Add geometry to the parcel scrape (set `returnGeometry=true` and project to WGS84) to get true lat/lon. |

---

## License

See `LICENSE`. This codebase was built as a work product; rights and distribution are per the underlying development agreement.
