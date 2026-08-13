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

Maricopa recorder documents carry party **names, not APNs**. They resolve two ways, in order of confidence: (1) **deed-number direct join** — the parcel master carries `DEED_NUMBER`, the recording number of the deed that vests each parcel, so a recorded deed whose number matches is pinned to that APN by exact identifier; (2) **name matching** (`pipeline/match_recorder.py`, strict-first, with a co-party intersection tier) for everything else. Documents that can't be pinned stay in the list as **unresolved** leads — still exported with their name + document number for skip tracing. Pima deed transfers arrive already parcel-resolved from the GIS layer. A `NTS Cancelled` (CQ) or `Trustee's Deed` (TD) is not a standalone lead — it **closes** its matching Notice of Trustee Sale, marking that lead cancelled or completed.

**Deeds resolve retroactively.** The assessor lags ~4 weeks writing a new deed into `DEED_NUMBER`, so a freshly-recorded deed will show **unresolved until that processing clears**, then resolve automatically on a later build — this is expected, not a bug. The dashboard says as much in the detail panel for recent unresolved deeds.

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
`scrapers/maricopa_recorder_api.py` has a `DOC_TYPES` registry (query code → label + category). These are the **2-letter query codes** from the county's own search dropdown, not the display codes returned in results — verify a code's live behavior before adding one (e.g. `SL` is State Tax Lien; `STL` is Substitution of Trustee). The current types span Foreclosure, Legal, Tax & Liens, Estate, Estate Planning, and Transfers. Pima deed transfers are a single `Deed Transfer` type from the GIS layer; the treasurer feed adds `Tax Delinquent`.

### Doc-type recon findings — DO NOT re-add codes off their label (Aug 2026)
A full recon of all 267 Maricopa codes (30-day window, volume + display-code + sampled parties + lifecycle) settled which codes belong. **Labels lie; always sample the parties before adding.** Decisions:

- **`BB` Beneficiary Deed — ADDED** (category *Estate Planning*, on by default). Transfer-on-death deeds, >500/mo, all-family parties. The *pre-death* counterpart to Death Certificate / Probate (aging owner arranging succession). Note it does **not** vest the parcel, so it resolves by name, not the deed# join.
- **`LN` HOA/Assessment Lien — ADDED** (category *Tax & Liens*, **off by default**). Real HOA/assessment-delinquency signal but routine (Sun City rec-center + HOA management liens), so it's a toggle like Judgment / AHCCCS / Mechanics.
- **`NC` "Restitution/Racketeering Lien" — SKIP.** Label looks like a high-value lien, but every record is **STATE OF ARIZONA v. a criminal defendant** — no property nexus. A label-accurate, party-misleading trap; only party-sampling catches it.
- **`EL` "DES Lien" — SKIP.** Accurate label, but it's **unemployment-insurance liens against employer businesses** (LLCs/Inc), not homeowners.
- **170 of 267 codes are dead** (0 volume over 30 days) — including `HD` Sheriff's Deed, `TR` Treasurer's Deed, `XD` Tax Deed, `WA` Writ of Attachment, `EX` Execution, `SF`/`CS` certificates of sale. **This is expected: Arizona is a trustee-sale (non-judicial) foreclosure state.** Foreclosures run through Notice of Trustee Sale → Trustee's Deed (both already captured), so the judicial/sheriff/tax-sale outcome codes other states rely on simply aren't recorded here at volume. Don't add them expecting foreclosure outcomes — use `NS` (with the CQ/TD lifecycle) instead.
- The remaining ~63 non-dead codes are administrative noise (releases, reconveyances, satisfactions, assignments, financing statements, plats/surveys/easements) — resolutions and paperwork, not distress.

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

### Pima recorder — attended scraper (`scrapers/pima_recorder.py`)
The Pima recorder portal (`pimacountyaz-web.tylerhost.net`, Tyler EagleWeb) gates every search behind a once-per-session disclaimer + reCAPTCHA — so it can't run headless in the 4am automation. Instead it's an **attended, on-demand local tool**: run it when you want fresh Pima recorder distress docs (foreclosure, lis pendens, probate, liens the GIS deed feed can't provide).

#### Runbook (the daily routine)

```bash
python scrapers/pima_recorder.py            # last 3 days, default-on types
python scrapers/pima_recorder.py --days 30  # wider backfill window
```

1. **Run the command.** A separate browser window opens ("Google Chrome for Testing") showing the Pima County disclaimer page.
2. **In that window** — not your everyday Chrome — accept the disclaimer and solve the reCAPTCHA. That's the only thing you do. The script watches the window and continues by itself the moment the accept lands; there is nothing to press in the terminal. (If you accidentally accept in the wrong browser, nothing breaks — the script keeps waiting and prints a reminder every 30 seconds until you accept in its window.)
3. **Wait for `✓ done`.** A routine 3-day run takes **under a minute total** — the scrape itself is ~20 seconds once you've accepted; a 30-day backfill is ~2 minutes. The log prints one line per date chunk plus a grantor→grantee sample for each doc type.

New records **merge into** `data/pima_recorder_docs_portal.jsonl` (cumulative, deduped by document number — a short run never erases older data). Each run stamps `data/_pima_recorder_last_run.json`. Small "new" counts are normal: the portal certifies records a few business days behind today and updates nightly Mon–Fri, so a daily 3-day window mostly re-confirms what you already have.

**The freshness pill** in the dashboard header shows how old this data is: **green** = pulled within 3 days, **amber** = 3–4 days, **red ⚠** = 5+ days, "never run" if absent. It reads the last-run stamp, so it updates after the next leads build (the 4am job, or a manual `python -m pipeline.build_docleads`). When the pill is red, run the command above — that's all it's asking for.

**If it fails, it fails loud — just re-run.** Progress is checkpointed per date chunk, so a re-run resumes where it stopped instead of starting over; you'll re-accept the disclaimer and it continues. The two loud failures:
- **`SESSION_EXPIRED`** — plain language: *the portal logged you out mid-run* (it bounced back to the disclaimer page). Nothing is wrong with your machine or the data; sessions just expire. Re-run, accept again, it resumes.
- **`PORTAL_TIMEOUT`** — a page the script expected never appeared (slow portal, or Tyler changed something). The log dumps what the page actually showed. Re-run once; if it fails the same way twice, see the known fragility below.

**Known fragility — hardcoded portal URL.** The script enters the search UI directly at `/web/action/ACTIONGROUP55S1` (the home page's menu tiles are rendered by a `/web/homeActions` XHR that never renders for an automated browser session, even after a genuine disclaimer accept — so the tiles can't be clicked and the direct URL is the reliable way in). If Tyler renumbers that action group in a portal upgrade, the symptom is: **disclaimer accepts fine, then `PORTAL_TIMEOUT` on every attempt**, with the log line `direct action-group route bounced` and/or a page-text dump showing the home page ("You have been redirected to the home page because your options have changed"). Fix: in a normal browser, accept the disclaimer, click *Official Records Search - Web*, and copy the new `/web/action/ACTIONGROUP…` URL from the address bar into `ACTION_GROUP_URL` in `scrapers/pima_recorder.py`.

**It is deliberately NOT in the GitHub Actions automation** — a headless runner can't solve the reCAPTCHA.

**Pima recorder doc types** (Part B recon, party-verified): on by default — Notice of Trustee Sale, NTS Cancelled (closer), Trustee's Deed (closer), Lis Pendens, Death Certificate, Affidavit Terminating JT/CP (a co-owner death), Deed of Distribution, Affidavit of Succession, Federal/State/City Lien, Beneficiary Deed, Beneficiary Deed Revocation, Disclaimer Deed. Off by default — Judgment, AHCCCS Lien, Mechanics Lien.

**Skipped — party-verified traps (do NOT re-add off the label):**
- **RESTITUTION LIEN** (301/mo) — grantor is an individual but grantee is always **ARIZONA STATE**: criminal restitution, no property nexus. (The Maricopa `NC` equivalent.)
- **NOTICE LIEN** (267/mo) and **HOSPITAL LIEN** (125/mo) — grantee is always a **medical center / hospital**: injury-settlement liens, not realty.
- Mining / water-right / land-patent codes exist but are rare Arizona-frontier instruments, not distress. No tribal-land codes (tribal trust land is federal/BIA, not county-recorded).

### Resolving Pima recorder docs
The portal exposes **no parcel/APN/legal reference per document** — only sequence number, date, grantor, grantee (property ID lives only in the purchasable image). So Pima recorder docs resolve two ways: **vesting deeds** (Trustee's Deed, Deed of Distribution) join by sequence# ↔ the parcel layer's `SEQ_NUM_D` (Pima's DEED_NUMBER equivalent, 90% populated, ~4-week assessor lag); **everything else** (Notice of Trustee Sale, Lis Pendens, Death Certificate, liens) resolves by name matching against the Pima parcel owner index — **expect ~40–50%**, the same ceiling as Maricopa recorder docs, because there's no parcel-ID shortcut. (Pima GIS *deed transfers* stay 100% resolved — that feed carries the APN directly.)
- City of Phoenix / Tucson / Mesa code violation portals
- Arizona Judicial Branch probate case search

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no data yet` on dashboard | Run `python -m pipeline.build_docleads` — it creates `data/leads.json`. |
| Pima recorder scraper: `SESSION_EXPIRED` | The portal logged you out mid-run (sessions expire). Re-run the same command, accept the disclaimer again — it resumes from the last completed chunk. |
| Pima recorder scraper: `PORTAL_TIMEOUT` every attempt, log says `direct action-group route bounced` | Tyler renumbered the search entry URL. See "Known fragility" in the Pima recorder section — update `ACTION_GROUP_URL` in `scrapers/pima_recorder.py`. |
| Recorder scrape returns 0 rows | County updated the portal UI. Check `data/_debug/*.png` for the failed screen and update selectors in the scraper. |
| Pima parcel scraper can't find endpoint | Visit https://gisopendata.pima.gov, find the Parcels dataset, click "View Data Source", copy the URL (without `/query`), and pass it: `python scrapers/pima_parcels.py --url <url>`. |
| ArcGIS paginator returns fewer records than count | Rate-limited. The built-in backoff will handle it. Rerun if it stalls. |
| Map shows nothing | Current build uses zip centroid fallback. Add geometry to the parcel scrape (set `returnGeometry=true` and project to WGS84) to get true lat/lon. |

---

## License

See `LICENSE`. This codebase was built as a work product; rights and distribution are per the underlying development agreement.
