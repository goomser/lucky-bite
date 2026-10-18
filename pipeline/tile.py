#!/usr/bin/env python3
"""
Step 2 of the Lucky Bite data pipeline: places.jsonl (one or many) -> tiles/ + index.json.

Tiles are 1° x 1° cells that split into quarters whenever they hold more than MAX_PER_TILE
places, down to 1/16° (about 4 miles). So rural Kansas is one file and Manhattan is many small ones.

Tile key: "z<level>_<latIdx>_<lonIdx>" where cell size = 1 / 2**level degrees and
          latIdx = floor(lat / size), lonIdx = floor(lon / size).

index.json: {"built": "YYYY-MM-DD", "max_level": 4, "tiles": {"<key>": count, ...}}
The app walks a 1° cell down the levels until it finds a key present in the index.

Sources are merged here. Records whose id starts with "o" come from Overture (overture.py); an
Overture place is dropped when an OpenStreetMap place with the same name sits within ~250 m, so
OSM's hours, prices and diet tags win whenever both sources know a restaurant.

The world is processed in 10° x 10° blocks so memory stays small on a free CI runner.

Usage: tile.py OUT_DIR places1.jsonl [places2.jsonl ...]
"""
import json, math, os, re, sys, time, tempfile
from collections import defaultdict

MAX_PER_TILE = 2500   # ~350 KB of JSON
MAX_LEVEL = 4         # 1/16 degree, about 4 miles
BLOCK = 10            # degrees per processing block
DUP_MI = 0.16         # ~250 m
NORM = re.compile(r"[^a-z0-9]+")


def key(level, lat, lon):
    size = 1 / 2 ** level
    return f"z{level}_{math.floor(lat / size)}_{math.floor(lon / size)}"


def norm_name(s):
    s = NORM.sub("", s.lower())
    return s[3:] if s.startswith("the") else s


def dist_mi(a, b):
    r = 3958.8
    dlat = math.radians(b[0] - a[0]); dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def dedupe(recs):
    """Drop Overture records that duplicate an OSM record (same normalized name within DUP_MI)."""
    osm_by_name = defaultdict(list)
    for r in recs:
        if not r[0].startswith("o"):
            osm_by_name[norm_name(r[1])].append((r[2], r[3]))
    out, dropped = [], 0
    for r in recs:
        if r[0].startswith("o"):
            n = norm_name(r[1])
            cands = osm_by_name.get(n, [])
            if not cands and len(n) >= 6:  # "skylinechili" vs "skylinechiliflorence"
                for k, v in osm_by_name.items():
                    if len(k) >= 6 and (k.startswith(n) or n.startswith(k)):
                        cands = v; break
            if any(dist_mi((r[2], r[3]), c) <= DUP_MI for c in cands):
                dropped += 1
                continue
        out.append(r)
    return out, dropped


def split(level, recs, out):
    if len(recs) <= MAX_PER_TILE or level >= MAX_LEVEL:
        out[key(level, recs[0][2], recs[0][3])] = recs
        return
    buckets = defaultdict(list)
    for r in recs:
        buckets[key(level + 1, r[2], r[3])].append(r)
    for sub in buckets.values():
        split(level + 1, sub, out)


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    out_dir, inputs = sys.argv[1], sys.argv[2:]
    t0 = time.time()

    # Pass 1: shard every input line into 10° blocks on disk (cheap on memory, at most 648 files).
    tmp = tempfile.mkdtemp(prefix="tiles-")
    handles, total = {}, 0
    for path in inputs:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    lat, lon = float(r[2]), float(r[3])
                except Exception:
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                b = f"{math.floor(lat / BLOCK)}_{math.floor(lon / BLOCK)}"
                if b not in handles:
                    handles[b] = open(os.path.join(tmp, b + ".jsonl"), "w", encoding="utf-8")
                handles[b].write(line if line.endswith("\n") else line + "\n")
                total += 1
    for h in handles.values():
        h.close()
    print(f"{total:,} input records in {len(handles)} blocks", file=sys.stderr)

    # Pass 2: per block, dedupe by id and across sources, then tile.
    tdir = os.path.join(out_dir, "tiles")
    os.makedirs(tdir, exist_ok=True)
    index = {"built": time.strftime("%Y-%m-%d"), "max_level": MAX_LEVEL, "tiles": {}}
    unique, dropped_total, biggest = 0, 0, 0
    for b in sorted(handles):
        seen, cells = set(), defaultdict(list)
        with open(os.path.join(tmp, b + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r[0] in seen:
                    continue
                seen.add(r[0])
                cells[key(0, r[2], r[3])].append(r)
        os.remove(os.path.join(tmp, b + ".jsonl"))
        for recs in cells.values():
            recs, dropped = dedupe(recs)
            dropped_total += dropped
            unique += len(recs)
            tiles = {}
            split(0, recs, tiles)
            for k, trecs in tiles.items():
                trecs.sort(key=lambda r: r[1].lower())
                with open(os.path.join(tdir, k + ".json"), "w", encoding="utf-8") as f:
                    json.dump(trecs, f, ensure_ascii=False, separators=(",", ":"))
                index["tiles"][k] = len(trecs)
                biggest = max(biggest, len(trecs))
    os.rmdir(tmp)

    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    print(f"{unique:,} places ({dropped_total:,} cross-source duplicates dropped) in {len(index['tiles']):,} tiles, "
          f"largest {biggest:,}, {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
