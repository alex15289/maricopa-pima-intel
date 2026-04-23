# Pima Recorder Daily Refresh — Setup Guide

Get fresh Pima County Recorder data into the intel dashboard. Run this once
a day or once a week — fresh recorder signals (NOTS, Lis Pendens, Judgments,
AHCCCS liens, City liens, Sheriff's Deeds) flow directly into the dashboard
scoring and filter chips.

---

## How it works (30-second version)

```
  ┌───────────────────────┐   ┌──────────────────────┐   ┌────────────────┐
  │  ./scrape-pima.sh     │ → │  scraper → CSV       │ → │  enrich → JSONL│
  └───────────────────────┘   └──────────────────────┘   └────────────────┘
                                                                  │
                                                                  ▼
                          ┌────────────────┐      ┌──────────────────────┐
                          │  GitHub Pages  │ ←──  │  build_leads.py      │
                          └────────────────┘      └──────────────────────┘
                                  │
                                  ▼
                 https://deftones420x.github.io/maricopa-pima-intel
```

You run ONE command. You solve ONE CAPTCHA. Dashboard updates.

---

## Three workflows — pick the one you like

### Workflow A — Full auto (recommended for daily use)

One command does everything:
```bash
cd ~/Desktop/maricopa-pima-intel
./scrape-pima.sh
```

What happens:
1. Chrome opens to the Pima Recorder search page
2. **You solve the CAPTCHA**, pick a document type, set date range, click Search
3. Return to terminal, press Enter
4. Scraper walks through all results (takes 10-30 minutes depending on count)
5. Script auto-converts to pipeline format
6. Rebuilds `leads.json`
7. Commits + pushes to GitHub
8. Dashboard updates in ~60 seconds

### Workflow B — Scrape only, decide later when to publish

```bash
python3 scrapers/pima_recorder_selenium.py
```

This JUST scrapes. Writes to `data/Pimacounty_Data.csv`. You can inspect
the CSV, re-scrape, do whatever. When ready to publish:

```bash
python3 pipeline/enrich_pima_recorder.py
python3 pipeline/build_leads.py
git add data/*.jsonl data/*.csv data/leads.json
git commit -m "Pima recorder refresh"
git push
```

### Workflow C — Upload an XLSX from somewhere else

If someone hands you an XLSX that matches the same column format
(Document Type, Sequence Number, Recording Date, Grantors, Grantees,
Parcel ID, Address, Legal Description, etc.), drop it in and convert:

```bash
python3 pipeline/enrich_pima_recorder.py --input path/to/file.xlsx
python3 pipeline/build_leads.py
# then git commit + push
```

---

## First-time setup (once per machine)

```bash
cd ~/Desktop/maricopa-pima-intel

# Install Python dependencies
pip install selenium undetected-chromedriver webdriver-manager scrapy openpyxl --break-system-packages

# Make the launcher executable
chmod +x scrape-pima.sh
```

Done. Chrome must be installed on the machine — the scraper drives Chrome,
so no other browser will work.

---

## Scraper tips

### Running by document type (recommended)

The Tyler search form lets you filter by document type. Instead of scraping
everything, pick ONE doc type per run. Smaller result sets = fewer CAPTCHA
prompts, faster to finish, easier to verify.

Suggested rotation (Monday through Saturday):
- Mon: NOTICE OF TRUSTEE'S SALE (last 7 days)
- Tue: LIS PENDENS (last 7 days)
- Wed: JUDGMENT (last 7 days)
- Thu: AHCCCS LIEN (last 30 days — lower volume)
- Fri: CITY LIEN (last 30 days — lower volume)
- Sat: SHERIFF'S DEED (last 7 days)

Or run one big weekly sweep on Sunday — whatever fits your workflow.

### Resume support

The scraper is smart about duplicates. If you stop mid-scrape (Ctrl+C, laptop
dies, CAPTCHA gives up), next time you run it, it skips records already in
`data/Pimacounty_Data.csv`. No re-work.

### CAPTCHA reappearing mid-scrape

If Tyler's anti-bot prompts another CAPTCHA while scraping, the script
detects this (sequence number stops advancing) and waits 40 seconds. Solve
the CAPTCHA in the Chrome window and it continues. Max 3 retries before
it gives up on that record.

### Testing without deploying

Want to verify output before publishing? Run the translator in dry-run mode:

```bash
python3 pipeline/enrich_pima_recorder.py --dry-run
```

Shows you how many records would be emitted and what signal types they'd map to.

---

## Troubleshooting

### "no recorder file found"

The translator couldn't find the scraper output. Expected at:
- `data/pima_recorder.xlsx` (preferred)
- `data/pima_recorder.csv`
- `data/Pimacounty_Data.csv` (default from scraper)

Use `--input PATH` if your file is elsewhere.

### "openpyxl not installed"

```bash
pip install openpyxl --break-system-packages
```

### "chromedriver not found"

The scraper uses `webdriver-manager` to auto-install the right ChromeDriver
for your Chrome version. If it fails, check Chrome is installed and up to date.

### "Dropped (unknown type)" is high

Some Pima document types don't map to our canonical signals — Warranty Deeds,
Releases of Lien, misc recording types. This is normal. Anything that IS a
distress signal (NOTS, Lis Pendens, Judgments, AHCCCS, City Lien, Sheriff's
Deed) will map correctly.

If a doc type you care about isn't mapping, edit `SIGNAL_MAPPINGS` in
`pipeline/enrich_pima_recorder.py` to add a new pattern.

### "Dropped (no APN)" is high

Pima recorder clerks sometimes don't include parcel IDs in the Legal Description
field, especially on older documents or liens against people (not properties).
These get dropped because we can't match them to a parcel without an APN.

Typical loss: 10-20% of records. That's the cost of using county recorder data
— always messier than clean GIS data.

---

## What flows into the dashboard

Every signal the translator emits:
- Appears as a **new filter chip** in the sidebar (auto-built from the data)
- Contributes to the **6-pattern distress system** (Foreclosure / Tax / Legal etc.)
- Bumps leads toward **HOT / STRONG / LIKELY** tiers based on signal stacking
- Shows up in the detail pane with raw doc type, grantor/grantee, record date

Example: a house that already had "Likely Vacant" and "Estate Owner" heuristics
flagged, then gets a new "Notice of Trustee Sale" signal — jumps from LIKELY
to HOT tier. That's exactly what your client wants fresh daily data to enable.
