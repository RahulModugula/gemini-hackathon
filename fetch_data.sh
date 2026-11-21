#!/usr/bin/env bash
# Download the public Outagamie County YMCA childcare bid set into data/ and verify checksums.
# Uses curl's default user agent (the county CDN rejects spoofed browser UAs) and resumes
# partial downloads, because the server drops long transfers. Exits non-zero on any short file
# or checksum mismatch.
set -euo pipefail
cd "$(dirname "$0")"
BASE="https://www.outagamie.gov/files/assets/county/v/1/finance/bids-rfps/closing-2026/maintenance/childcare-center"
FILES=(
  outagamie-ymca-childcare-bid-set-drawings.pdf
  outagamie-county-ymca-childcare-project-manual.pdf
  addendum-1.pdf
  addendum-2.pdf
  a201-daycare-center.pdf
)
[ -f data/SHA256SUMS ] || { echo "missing data/SHA256SUMS" >&2; exit 1; }
size() { stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0; }
ok_sum() { (cd data && grep " $1\$" SHA256SUMS | shasum -a 256 -c --status - 2>/dev/null); }
for f in "${FILES[@]}"; do
  if [ -f "data/$f" ] && ok_sum "$f"; then echo "$f already verified"; continue; fi
  done_ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    # --fail: HTTP errors (403 page etc.) exit non-zero instead of being saved as the PDF.
    # curl exits 0 only when the transfer completed; 33 = range already satisfied
    rc=0; curl -fsS -C - --max-time 120 -o "data/$f" "$BASE/$f" || rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 33 ]; then done_ok=1; break; fi
  done
  [ "$done_ok" -eq 1 ] || { echo "incomplete after 10 attempts: $f ($(size "data/$f") bytes)" >&2; exit 1; }
  echo "$f $(size "data/$f") bytes"
done
(cd data && shasum -a 256 -c SHA256SUMS)
