#!/usr/bin/env bash
# Build Lucky Bite places for one Geofabrik region, e.g.:
#   pipeline/build.sh north-america        -> work/places-north-america.jsonl
#   pipeline/build.sh north-america/us/kentucky   (any Geofabrik path works, handy for testing)
#
# Needs: osmium-tool (apt install osmium-tool / brew install osmium-tool), python3, pip install osmium
set -euo pipefail
REGION="${1:?region, e.g. europe or north-america/us/kentucky}"
WORK="${2:-work}"
SLUG="$(echo "$REGION" | tr '/' '-')"
mkdir -p "$WORK"
RAW="$WORK/$SLUG.osm.pbf"
FILTERED="$WORK/$SLUG.food.osm.pbf"
OUT="$WORK/places-$SLUG.jsonl"

echo "== $REGION: downloading"
# --speed-* abort a transfer that stalls below 100 KB/s for a minute (then --retry resumes it with -C -),
# so a throttled mirror fails fast and retries instead of hanging the job for hours.
curl -fL --retry 8 --retry-delay 30 --retry-all-errors -C - --speed-limit 100000 --speed-time 60 \
     -o "$RAW" "https://download.geofabrik.de/$REGION-latest.osm.pbf"

echo "== $REGION: filtering to eating places"
# Keeps nodes/ways/relations tagged as eating places, plus the nodes those ways need (for coordinates).
osmium tags-filter --overwrite -o "$FILTERED" "$RAW" \
  "n/amenity=restaurant,fast_food,cafe,bar,pub,food_court,biergarten,ice_cream" \
  "w/amenity=restaurant,fast_food,cafe,bar,pub,food_court,biergarten,ice_cream" \
  "r/amenity=restaurant,fast_food,cafe,bar,pub,food_court,biergarten,ice_cream"
rm -f "$RAW"   # the raw extract is the big one; drop it as soon as it's filtered

echo "== $REGION: extracting"
python3 "$(dirname "$0")/extract.py" "$FILTERED" "$OUT"
rm -f "$FILTERED"
echo "== $REGION: done -> $OUT ($(wc -l < "$OUT") places)"
