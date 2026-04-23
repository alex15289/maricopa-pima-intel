#!/bin/bash
# scrape-pima.sh
# ==============
# One-command Pima Recorder refresh for the intel dashboard.
#
# WHAT THIS DOES:
#   1. Opens Chrome with the Tyler Recorder portal
#   2. Waits for YOU to solve CAPTCHA + set search criteria
#      (date range, document type, etc.)
#   3. Scrapes all matching records
#   4. Converts output → pipeline format
#   5. Rebuilds leads.json
#   6. Commits + pushes to GitHub (dashboard auto-updates)
#
# RECOMMENDED WORKFLOW (daily or weekly):
#   1. cd ~/Desktop/maricopa-pima-intel
#   2. ./scrape-pima.sh
#   3. When Chrome opens: solve CAPTCHA → pick a doc type (e.g.
#      "NOTICE OF TRUSTEE'S SALE") → set date range → click Search
#   4. Return to terminal, press Enter
#   5. Walk away while it scrapes (takes 10-30 min depending on count)
#   6. Script auto-commits and pushes
#
# FIRST-TIME SETUP:
#   chmod +x scrape-pima.sh
#   pip install selenium undetected-chromedriver webdriver-manager scrapy openpyxl --break-system-packages

set -e  # exit on any error

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Pima Recorder Scraper — Intel Dashboard Refresh         ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Activate venv if present ──────────────────────────────────────
if [ -d ".venv" ]; then
    echo "→ Activating virtual environment..."
    source .venv/bin/activate
fi

# ── Step 2: Verify dependencies ───────────────────────────────────────────
echo "→ Checking dependencies..."
python3 -c "import selenium" 2>/dev/null || {
    echo "  Installing selenium + friends..."
    pip install selenium undetected-chromedriver webdriver-manager scrapy openpyxl --break-system-packages
}

# ── Step 3: Run the scraper ───────────────────────────────────────────────
echo ""
echo "→ Launching scraper. Chrome will open shortly..."
echo "  WHEN CHROME OPENS:"
echo "    1. Solve the CAPTCHA"
echo "    2. Pick a document type from the search form"
echo "    3. Set a date range (recommend: last 90 days)"
echo "    4. Click the SEARCH button"
echo "    5. Return here and press ENTER"
echo ""

python3 scrapers/pima_recorder_selenium.py

# ── Step 4: Verify output exists ──────────────────────────────────────────
if [ ! -f "data/Pimacounty_Data.csv" ]; then
    echo ""
    echo "✗ Scraper did not produce data/Pimacounty_Data.csv"
    echo "  Exiting without updating pipeline."
    exit 1
fi

# ── Step 5: Translate raw scrape → JSONL signals ──────────────────────────
echo ""
echo "→ Translating scraper output → pipeline signals..."
python3 pipeline/enrich_pima_recorder.py --input data/Pimacounty_Data.csv

# ── Step 6: Rebuild dashboard leads.json ──────────────────────────────────
echo ""
echo "→ Rebuilding leads..."
PYTHONPATH=. python3 pipeline/build_leads.py

# ── Step 7: Commit and push ───────────────────────────────────────────────
echo ""
echo "→ Staging changes for GitHub..."
git add data/Pimacounty_Data.csv data/pima_recorder_raw.jsonl data/leads.json 2>/dev/null || true

if git diff --cached --quiet; then
    echo "  (no changes to commit)"
else
    DATE=$(date +%Y-%m-%d)
    COUNT=$(wc -l < data/pima_recorder_raw.jsonl 2>/dev/null || echo "?")
    git commit -m "Pima Recorder refresh $DATE ($COUNT signals)"
    echo ""
    echo "→ Pushing to GitHub..."
    git push
    echo ""
    echo "✓ Dashboard will update in ~60 seconds at:"
    echo "  https://deftones420x.github.io/maricopa-pima-intel/"
fi

echo ""
echo "✓ Done."
