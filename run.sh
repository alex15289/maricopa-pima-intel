#!/usr/bin/env bash
#
# Maricopa + Pima Document Intel — full refresh (UCIF v5.5.0, doc-type model)
# ---------------------------------------------------------------------------
# Pulls both parcel masters (enrichment), scrapes recorded documents from each
# county source, and builds the doc-type lead list. Every lead is a real
# recorded document; the lead type IS the document type.
#
# Usage:
#   ./run.sh                       # full refresh, 30-day recorder window
#   DAYS=90 ./run.sh               # wider Maricopa recorder window
#   SKIP_PARCELS=1 ./run.sh        # docs + build only (parcel master fresh)

set -euo pipefail

DAYS="${DAYS:-30}"
SKIP_PARCELS="${SKIP_PARCELS:-0}"
PYTHON="${PYTHON:-python}"

cd "$(dirname "$0")"
export PYTHONPATH=.

echo "======================================"
echo "Maricopa + Pima Document Intel — refresh"
echo "  recorder window: $DAYS days"
echo "  skip parcel master: $SKIP_PARCELS"
echo "======================================"

if [[ "$SKIP_PARCELS" != "1" ]]; then
  echo "[1/5] Maricopa parcel master (enrichment)..."
  $PYTHON scrapers/maricopa_parcels.py
  echo "[2/5] Pima parcel master (enrichment)..."
  $PYTHON scrapers/pima_parcels.py
else
  echo "[1-2/5] Parcel masters skipped (SKIP_PARCELS=1)"
fi

echo "[3/5] Maricopa recorder documents (last $DAYS days)..."
$PYTHON scrapers/maricopa_recorder_api.py --days "$DAYS" || echo "  (recorder failed — fail-soft)"

echo "[4/5] Pima deed transfers (GIS layer 12) + treasurer feed..."
$PYTHON scrapers/pima_deeds.py --days 90 || echo "  (pima deeds failed — fail-soft)"
$PYTHON pipeline/enrich_treasurer.py || echo "  (treasurer translator failed — fail-soft)"

echo "[5/5] Build doc-type leads..."
$PYTHON -m pipeline.build_docleads

echo
echo "Done. Dashboard: open index.html"
