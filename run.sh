#!/usr/bin/env bash
#
# Maricopa + Pima Intel — full refresh
# ------------------------------------
# Pulls both parcel masters, scrapes recent recorder signals, builds the
# ranked lead list, and exports Skip Trace + GHL CSVs.
#
# Usage:
#   ./run.sh                       # full refresh, default 30-day signal window
#   DAYS=90 ./run.sh               # longer signal window
#   SKIP_PARCELS=1 ./run.sh        # signals + pipeline only (parcel master already fresh)

set -euo pipefail

DAYS="${DAYS:-30}"
SKIP_PARCELS="${SKIP_PARCELS:-0}"
PYTHON="${PYTHON:-python}"

cd "$(dirname "$0")"

echo "======================================"
echo "Maricopa + Pima Intel — full refresh"
echo "  date window: $DAYS days"
echo "  skip parcel master: $SKIP_PARCELS"
echo "======================================"
echo

if [[ "$SKIP_PARCELS" != "1" ]]; then
  echo "[1/4] Maricopa parcel master..."
  $PYTHON scrapers/maricopa_parcels.py

  echo
  echo "[2/4] Pima parcel master..."
  $PYTHON scrapers/pima_parcels.py
else
  echo "[1-2/4] Parcel masters skipped (SKIP_PARCELS=1)"
fi

echo
echo "[3/4] Recorder signals (last $DAYS days)..."
$PYTHON scrapers/maricopa_recorder.py --days "$DAYS"
$PYTHON scrapers/pima_recorder.py    --days "$DAYS"

echo
echo "[4/4] Build leads + export CSVs..."
$PYTHON pipeline/build_leads.py
$PYTHON pipeline/export_csv.py

echo
echo "Done."
echo "  Dashboard: open index.html"
echo "  Exports:   data/export_skiptrace.csv, data/export_ghl.csv"
