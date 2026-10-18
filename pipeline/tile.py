#!/usr/bin/env python3
"""
Step 2 of the Lucky Bite data pipeline: places.jsonl (one or many) -> tiles/*.json.gz + index.json.

Tiles are gzipped (the app inflates them with DecompressionStream) so the whole world fits a 1 GB site.

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
import gzip, json, math, os, re, sys, time, tempfile
from collections import defaultdict

MAX_PER_TILE = 2500   # ~350 KB of JSON
MAX_LEVEL = 4         # 1/16 degree, about 4 miles
BLOCK = 10            # degrees per processing block
DUP_MI = 0.16         # ~250 m
CHAIN_MIN = 25        # a name this common worldwide is a chain...
# ...unless it's made only of generic words ("Pizzeria", "Bar Central", "Café de la Gare").
GENERIC = set("""the a la le les el los las de del della di da du der die das al an and y et und bar cafe café caffe caffè
restaurant restaurante ristorante restoran pizzeria pizza bistro bistrot grill grille diner cantina taverna tavern trattoria
osteria pub inn kitchen cuisine food foods eat eatery cafeteria kantine kiosk imbiss bakery panaderia boulangerie taqueria
kebab kebap doner döner sushi ramen pho buffet snack snackbar club lounge house casa maison central centro centre center
coffee tea chai express fast burger burgers chicken fish bbq barbecue steak steakhouse wings taco tacos noodle noodles
dumpling bakery cakes cake bistro brasserie bodega cantine comedor fonda loncheria merendero parrilla asador churrasqueria
marisqueria cevicheria cocina gastro gastropub bierhaus brauhaus gasthaus gasthof wirtshaus stube stuben hof garten
hotel motel station stop corner square place plaza park beach road street new old little big royal golden silver
north south east west city town village mr mrs ms mister mama papa mom pop uncle aunt king queen chef gare port mar sol luna sport sports
golden garden gardens wok china chinese dragon panda palace shanghai peking beijing hong kong jade lotus bamboo great wall hunan szechuan
sichuan canton cantonese mandarin oriental asia asian tokyo sakura fuji osaka kyoto samurai ninja wasabi taj mahal india indian bombay
mumbai delhi curry tandoor tandoori masala spice spices saffron bombay thai siam bangkok pho saigon hanoi seoul korea korean
el la mi casa hacienda cantina tequila azteca fiesta mexico mexican burrito burritos taqueria mariscos pollo loco amigos
italia italian italiano roma napoli venezia milano toscana sicilia pasta trattoria pizzeria bella bello buon buona nonna
greek gyro gyros athens olympia mediterranean kebab house grill cafe bistro
star stars sun moon ocean sea harbor harbour bay river lake hill hills mountain valley garden village family
deli delicatessen bakery donuts donut bagel bagels coffee cafe waffle pancake pancakes breakfast brunch lunch dinner
smokehouse pit roadhouse saloon brewing brewery taproom ale alehouse""".split())
def generic(name):
    return all(w in GENERIC or w.isdigit() for w in re.sub(r"[^a-zà-ÿ0-9 ]+", " ", name.lower()).split())
NORM = re.compile(r"[^a-z0-9]+")
# Overture-only hygiene: business-registry names and non-restaurant service categories.
JUNK_NAME = re.compile(r"\b(inc|llc|l\.l\.c|dba|d/b/a|corp|corporation|holdings|enterprises)\b\.?", re.I)
JUNK_CUISINE = re.compile(r"delivery|courier|catering|supplier|wholesale|equipment", re.I)


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


GRID = 0.004  # ~0.25 mi cells for the duplicate check


def dedupe(recs):
    """Drop Overture records that duplicate an OSM record: same normalized name (or one a prefix
    of the other) within DUP_MI. OSM places are binned into a fine grid so each Overture place
    only compares against the handful of OSM places in its own and neighboring cells."""
    grid = defaultdict(list)
    for r in recs:
        if not r[0].startswith("o"):
            grid[(int(r[2] // GRID), int(r[3] // GRID))].append((norm_name(r[1]), r[2], r[3]))
    out, dropped = [], 0
    for r in recs:
        if r[0].startswith("o"):
            n = norm_name(r[1])
            gi, gj = int(r[2] // GRID), int(r[3] // GRID)
            dup = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for k, la, lo in grid.get((gi + di, gj + dj), ()):
                        if (k == n or (len(n) >= 6 and len(k) >= 6 and (k.startswith(n) or n.startswith(k)))) \
                                and dist_mi((r[2], r[3]), (la, lo)) <= DUP_MI:
                            dup = True
                            break
                    if dup:
                        break
                if dup:
                    break
            if dup:
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

    # Pass 1: shard every input line into 10° blocks on disk (cheap on memory, at most 648 files),
    # and count how often each name occurs worldwide: a name seen CHAIN_MIN+ times is a chain even
    # when the source forgot to say so.
    tmp = tempfile.mkdtemp(prefix="tiles-")
    handles, total = {}, 0
    name_count = defaultdict(int)
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
                name_count[norm_name(r[1])] += 1
    for h in handles.values():
        h.close()
    chains = {n for n, c in name_count.items() if c >= CHAIN_MIN and len(n) >= 4}
    del name_count
    seen_generic = set()
    def is_chain(name):
        n = norm_name(name)
        if n not in chains:
            return False
        if n in seen_generic:
            return False
        if generic(name):
            seen_generic.add(n)
            return False
        return True
    print(f"{total:,} input records in {len(handles)} blocks; {len(chains):,} chain names", file=sys.stderr)

    # Pass 2: per block, dedupe by id and across sources, then tile.
    tdir = os.path.join(out_dir, "tiles")
    os.makedirs(tdir, exist_ok=True)
    index = {"built": time.strftime("%Y-%m-%d"), "max_level": MAX_LEVEL, "tiles": {}}
    unique, dropped_total, biggest, junk = 0, 0, 0, 0
    for b in sorted(handles):
        seen, cells = set(), defaultdict(list)
        with open(os.path.join(tmp, b + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r[0] in seen:
                    continue
                seen.add(r[0])
                if r[0].startswith("o") and (JUNK_NAME.search(r[1]) or JUNK_CUISINE.search(r[8])):
                    junk += 1
                    continue
                if not r[5] and is_chain(r[1]):
                    r[5] = 1
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
                with gzip.open(os.path.join(tdir, k + ".json.gz"), "wt", encoding="utf-8", compresslevel=6) as f:
                    json.dump(trecs, f, ensure_ascii=False, separators=(",", ":"))
                index["tiles"][k] = len(trecs)
                biggest = max(biggest, len(trecs))
    os.rmdir(tmp)

    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    print(f"{unique:,} places ({dropped_total:,} cross-source duplicates and {junk:,} junk entries dropped) in {len(index['tiles']):,} tiles, "
          f"largest {biggest:,}, {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
